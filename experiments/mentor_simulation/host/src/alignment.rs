//! Drive production policies from the complete upstream state in one persistent simulator world.
//! Keep original-wrapper reference and audit arms separate from that production loop.
use crate::public::PublicPosition;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::Path;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};
use sts2_core::full_run::RunPolicy;
use sts2_core::potions::CharacterId;
use sts2_core::run_manager::{ActionResult, Phase, RunAction, RunManager};
use sts2_runai::policy::{CombatBudget, DecisionTelemetry, MctsCombatPolicy, MetaEvalRecord};
use sts2_runai::ppo::{
    net,
    obs::ActionDesc,
    policy::{build_obs, classify_surface, phase_str, PpoPolicy},
};
use sts2_wire::emit;

// Match arena's ordinary evaluation budget unless the experiment explicitly declares overrides.
const DEFAULT_SIMS: u32 = 1_000;
// Boss search is four times the ordinary deployed evaluation budget.
const DEFAULT_BOSS_SIMS: u32 = 4_000;
// Keep the arena's original action cap rather than defining a different validation limit.
const DEFAULT_MAX_STEPS: usize = sts2_core::full_run::DEFAULT_MAX_STEPS;

/// Collect independent public checks while the original policy owns every driver action.
#[derive(Debug, Default)]
struct BatchDiagnostics {
    step: usize,
    checks: usize,
    failed_checks: usize,
    checks_after_first_failure: usize,
    first_failure_step: Option<usize>,
    errors: Vec<Value>,
    groups: BTreeMap<String, Value>,
    stage_milliseconds: BTreeMap<String, f64>,
}

impl BatchDiagnostics {
    /// Retain one complete public fixture per error and encounter, with every occurrence indexed.
    fn observe(&mut self, record: &Value) -> Value {
        if record["kind"] == "forced_meta" {
            return record.clone();
        }
        self.checks += 1;
        if self.first_failure_step.is_some() { self.checks_after_first_failure += 1; }
        let mut reasons = BTreeSet::<String>::new();
        for value in [&record["import_errors"], &record["support"]["blocking_reasons"],
            &record["production"]["errors"]] {
            if let Some(rows) = value.as_array() {
                reasons.extend(rows.iter().map(|row| row.as_str().map(str::to_owned)
                    .unwrap_or_else(|| row.to_string())));
            }
        }
        for key in ["mapping_error", "projection_error"] {
            if let Some(reason) = record[key].as_str() { reasons.insert(reason.to_owned()); }
        }
        if record["production"]["ok"] == false && reasons.is_empty() {
            reasons.insert("public_policy_unavailable_without_reason".into());
        }
        if !reasons.is_empty() {
            self.failed_checks += 1;
            self.first_failure_step.get_or_insert(self.step);
        }
        for reason in &reasons {
            let group_key = format!("{}|{}|{}", record["kind"], record["floor"], reason);
            let repeated = self.groups.contains_key(&group_key);
            let group = self.groups.entry(group_key.clone()).or_insert_with(|| json!({
                "group_key":group_key,"signature":reason,"count":0,
                "first_step":self.step,"last_step":self.step,"first_record":record}));
            group["count"] = json!(group["count"].as_u64().unwrap_or(0) + 1);
            group["last_step"] = json!(self.step);
            self.errors.push(json!({"step":self.step,"floor":record["floor"],
                "kind":record["kind"],"signature":reason,"group_key":group_key,
                "possible_same_encounter_dependency":repeated}));
        }
        // Repeated history snapshots can grow quadratically; first fixtures remain complete above.
        json!({"kind":record["kind"],"floor":record["floor"],"phase":record["phase"],
            "step":self.step,"diagnostic_errors":reasons,"fixture_storage":"batch_diagnostics.groups"})
    }

    /// Separate driver completion from public coverage and preserve first-failure comparison counts.
    fn report(&self) -> Value {
        json!({"scope":"diagnostic_driver_outcomes_are_not_public_policy_results",
            "checks":self.checks,"failed_checks":self.failed_checks,
            "first_failure_step":self.first_failure_step,"checks_after_first_failure":self.checks_after_first_failure,
            "errors":self.errors,"groups":self.groups.values().collect::<Vec<_>>(),
            "stage_milliseconds":self.stage_milliseconds,
            "timing_scope":"decision_total includes mirror_clone and meta_public_policy; do not sum these fields",
            "limitations":["checks_mirror_support_and_meta_mapping_not_every_public_search",
                "driver_policy_visits_only_its_own_paths","repeated_encounter_errors_may_share_a_cause"]})
    }
}

/// Store full records normally, and bounded repeated records in diagnostic batches.
fn record_alignment(trace: &Arc<Mutex<Vec<Value>>>, batch: &Option<Arc<Mutex<BatchDiagnostics>>>, record: Value) {
    let record = if let Some(batch) = batch { batch.lock().unwrap().observe(&record) } else { record };
    trace.lock().unwrap().push(record);
}

/// Accumulate diagnostic wall time, leaving ordinary production runs unchanged.
fn record_elapsed(batch: &Option<Arc<Mutex<BatchDiagnostics>>>, stage: &str, started: Instant) {
    if let Some(batch) = batch {
        *batch.lock().unwrap().stage_milliseconds.entry(stage.into()).or_default() +=
            started.elapsed().as_secs_f64() * 1000.0;
    }
}

/// Replay meta observations or an explicit complete combat wire without rebuilding public snapshots.
fn replay_public_fixture(request: &Value) -> Result<Value, String> {
    let projection = &request["projection"];
    let visible = &projection["public_state"];
    if !visible.is_object() {
        return Err("alignment_fixture_public_state_missing".into());
    }
    // A combat-shaped meta fixture must retain its original PPO endpoint; infer only legacy requests.
    let combat_import = match request["kind"].as_str() {
        Some(kind) => kind == "combat_import",
        None => visible["combat"].is_object(),
    };
    if combat_import {
        let wire = request.get("wire_state").or_else(|| projection.get("wire_state"))
            .or_else(|| visible.get("wire_state")).ok_or("alignment_fixture_complete_wire_required")?;
        let state = sts2_wire::state_from_json(wire).map_err(|error| error.to_string())?;
        let root = PublicPosition::from_core(state, visible, false);
        let support = root.search_support();
        return Ok(json!({"ok":support["blocking_reasons"].as_array().is_some_and(Vec::is_empty),
            "support":support,"snapshot":root.snapshot(),
            "scope":"complete_wire_support_replay_not_full_search_or_run"}));
    }
    let mut response = crate::ppo::policy_evaluate(projection)?;
    response["scope"] = json!("public_meta_availability_replay_without_private_driver_action_mapping");
    Ok(response)
}
// Render only already-published option variables through the pinned public English catalogue.
static EVENT_TEXT: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../../vendor/spire-mentor/sts2-pycli/localization_eng/events.json"
    ))
    .expect("pinned event localization")
});
// Ancient choices publish these relic identities and titles; private pickup outcomes are never rendered.
static RELIC_TEXT: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../../vendor/spire-mentor/sts2-pycli/localization_eng/relics.json"
    ))
    .expect("pinned relic localization")
});

/// Render simple visible LocString variables without inventing values for missing or formatted slots.
fn render_public_text(template: &str, vars: &Value) -> Option<String> {
    let mut remaining = template;
    let mut output = String::new();
    while let Some(start) = remaining.find('{') {
        output.push_str(&remaining[..start]);
        let after = &remaining[start + 1..];
        let end = after.find('}')?;
        let key = &after[..end];
        if key.contains([':', '{']) {
            return None;
        }
        let value = vars.get(key)?;
        if let Some(value) = value.as_str() {
            output.push_str(value);
        } else if value.is_number() || value.is_boolean() {
            output.push_str(&value.to_string());
        } else {
            return None;
        }
        remaining = &after[end + 1..];
    }
    output.push_str(remaining);
    Some(output)
}

/// Use actual public localization keys and variables instead of approximate engine fallback prose.
fn public_option_text(source: &Value, option: &mut Value) {
    let prefix = source["loc_key"].as_str().unwrap_or("");
    if prefix.is_empty() {
        return;
    }
    for (field, suffix, vars) in [
        ("label", "title", "title_vars"),
        ("description", "description", "vars"),
    ] {
        let key = format!("{prefix}.{suffix}");
        let Some(template) = EVENT_TEXT[&key].as_str() else {
            continue;
        };
        if let Some(visible) = render_public_text(template, &source[vars]) {
            option[field] = json!(visible);
            option[format!("{field}_template")] = json!(template);
            option[format!("{field}_loc_key")] = json!(key);
            option[format!("{field}_loc_table")] = json!("events");
        }
    }
}

/// Keep a compact, reproducible list of structural differences without hiding their total.
fn differences(left: &Value, right: &Value, path: &str, out: &mut Vec<String>) {
    if left == right {
        return;
    }
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            let mut keys = a.keys().chain(b.keys()).collect::<Vec<_>>();
            keys.sort();
            keys.dedup();
            for key in keys {
                differences(&left[key], &right[key], &format!("{path}/{key}"), out);
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (i, (a, b)) in a.iter().zip(b).enumerate() {
                differences(a, b, &format!("{path}/{i}"), out);
            }
        }
        _ => out.push(path.into()),
    }
}

/// Whitelist visible power amounts and display counters, excluding engine-private accumulators.
fn public_powers(
    state: &sts2_core::CombatState,
    uids: &[sts2_core::PowerUid],
) -> Result<Value, String> {
    let values = emit::combat::powers_to_value(uids, &state.powers, "alignment.public.powers")
        .map_err(|e| e.to_string())?;
    Ok(json!(values
        .as_array()
        .into_iter()
        .flatten()
        .map(|p| {
            let mut row = json!({"power_id":format!("{}_POWER",p["name"].as_str().unwrap_or("")),
                "amount":p["amount"],"display_amount":p["display_amount"]});
            if p["name"] == sts2_core::PowerKind::Surrounded.python_name() {
                row["facing"] = match p["counter"].as_i64() {
                    Some(value) if value == i64::from(sts2_core::hooks::SURROUNDED_FACING_LEFT) => {
                        json!("Left")
                    }
                    Some(value)
                        if value == i64::from(sts2_core::hooks::SURROUNDED_FACING_RIGHT) =>
                    {
                        json!("Right")
                    }
                    _ => Value::Null,
                };
            }
            row
        })
        .collect::<Vec<_>>()))
}

/// Keep the compact bridge's formatted power lane alongside its structured public details.
fn power_lines(details: &Value) -> Vec<String> {
    details
        .as_array()
        .into_iter()
        .flatten()
        .map(|power| {
            format!(
                "{} {}",
                power["power_id"].as_str().unwrap_or(""),
                power["amount"].as_i64().unwrap_or(0)
            )
        })
        .collect()
}

/// Emit card-face quantities; never copy the wire's combat_vars or pile-order knowledge.
fn combat_card(
    state: &sts2_core::CombatState,
    uid: sts2_core::CardUid,
    index: usize,
) -> Result<Value, String> {
    let card = state
        .cards
        .iter()
        .find(|c| c.uid == uid)
        .ok_or("alignment_missing_card")?;
    let wire = emit::combat::card_to_value(state, card);
    let mut row = deck_card(card.card_id.to_python_name(), card.upgraded, None);
    row["i"] = json!(index);
    row["instance_id"] = json!(format!("visible-{}", uid.get()));
    row["energy_cost"] = wire["cost_override"].clone();
    row["star_cost"] = json!(sts2_core::effective_star_cost_in_combat(state, card));
    row["costs_x"] = json!(card.is_x_cost());
    row["star_costs_x"] = json!(card.card_id == sts2_core::cards::CardId::Stardust);
    row["mods"] = json!([]);
    if card.card_id == sts2_core::cards::CardId::Fetch {
        // Match the native public finished-history reference, never the private Fetch scratch set.
        row["fetch_played_this_turn"] = json!(state
            .combat_log
            .event_log
            .public_history()
            .filter(|history| history.complete)
            .map(|history| history.plays.iter().any(|play| {
                play.card_uid == uid && play.round == state.round && play.side == state.side
            })));
    }
    let keywords = [
        ("Exhaust", sts2_core::card_effects::card_exhausts_inst(card)),
        (
            "Ethereal",
            // Ghost Seed, Music Box and Hex also change the currently visible keyword.
            sts2_core::combat_loop::card_is_ethereal_at_play(state, card),
        ),
        ("Retain", sts2_core::card_effects::card_retain_inst(card)),
        ("Innate", sts2_core::card_effects::card_innate_inst(card)),
        (
            "Unplayable",
            sts2_core::card_effects::card_unplayable_inst(card),
        ),
        (
            "Eternal",
            card.keyword_override
                .unwrap_or_else(|| sts2_core::card_effects::registry_keyword_override(card.card_id))
                .contains(sts2_core::cards::KeywordOverride::ETERNAL),
        ),
    ];
    row["effective_keywords"] = json!(keywords
        .into_iter()
        .filter_map(|(name, present)| present.then_some(name))
        .collect::<Vec<_>>());
    row["keyword_state_complete"] = json!(true);
    let mut dynamic = Vec::new();
    let damage = crate::public::public_card_damage(card);
    if damage != 0 || card.card_id == sts2_core::cards::CardId::Rampage {
        dynamic.push(json!({"name":"Damage","base_value":damage,"current_value":damage,"enchanted_value":damage}));
    }
    if card.base_block != 0 {
        dynamic.push(json!({"name":"Block","base_value":card.base_block,"current_value":card.base_block,"enchanted_value":card.base_block}));
    }
    row["dynamic_values"] = json!(dynamic);
    if let Some(a) = card.affliction {
        row["affliction"] =
            json!({"affliction_id":format!("{:?}",a.kind).to_uppercase(),"amount":a.stacks});
    }
    if let Some((id, amount)) = emit::run_manager::card_enchantment(&card.enchantments) {
        row["enchantment"] = json!({"enchantment_id":id,"amount":amount});
    }
    Ok(row)
}

/// Describe a generated choice from its publicly revealed base template, with no private instance scratch.
fn generated_card(
    combat: &sts2_core::CombatState,
    option: &sts2_core::ChoiceOption,
    index: usize,
) -> Result<Value, String> {
    let mut template = sts2_core::CombatState::default();
    template.game_version = combat.game_version;
    template.player.character = combat.player.character;
    let mut card =
        sts2_core::build_card_for_version(option.card_id, option.upgraded, combat.game_version)
            .ok_or("alignment_generated_public_template_missing")?;
    let uid = sts2_core::CardUid::new(1).unwrap();
    card.uid = uid;
    template.cards.push(card);
    template.player.hand.push(uid);
    let mut row = combat_card(&template, uid, index)?;
    row["instance_id"] = Value::Null;
    Ok(row)
}

/// Select the native hand surface without exposing a private continuation kind.
fn choice_uses_visible_hand(combat: &sts2_core::CombatState) -> bool {
    combat.pending_choice.as_ref().is_some_and(|choice| {
        !choice.options.is_empty()
            && choice.options.iter().all(|option| {
                option.source_pile == Some(sts2_core::cards::PileKind::Hand)
                    || (option.source_pile.is_none()
                        && option
                            .card_uid
                            .is_some_and(|uid| combat.player.hand.contains(&uid)))
            })
    })
}

/// Mirror native ordinary removal while retaining the publicly observed Reattach corpse.
fn public_enemy_roster(
    combat: &sts2_core::CombatState,
) -> Result<Vec<(usize, sts2_core::CreatureUid)>, String> {
    let mut roster = Vec::new();
    for (index, uid) in combat.enemy_uids.iter().copied().enumerate() {
        let enemy = combat
            .creatures
            .iter()
            .find(|enemy| enemy.id == uid.0)
            .ok_or("alignment_missing_enemy")?;
        let reattaches = enemy.powers.iter().any(|power_uid| {
            combat.powers.iter().any(|power| {
                power.id == power_uid.0 && power.kind == sts2_core::PowerKind::Reattach
            })
        });
        if enemy.current_hp > 0 || reattaches {
            roster.push((index, uid));
        }
    }
    Ok(roster)
}

/// Translate an actual world's target into the native current-roster slot.
fn witnessed_enemy_target(rm: &RunManager, target: Option<u32>) -> Result<Option<u32>, String> {
    target
        .map(|index| {
            let combat = rm.combat.as_ref().ok_or("alignment_missing_combat")?;
            public_enemy_roster(combat)?
                .iter()
                .position(|(core_index, _)| *core_index == index as usize)
                .map(|index| index as u32)
                .ok_or_else(|| "alignment_actual_target_not_in_public_roster".into())
        })
        .transpose()
}

/// Project a reachable engine combat through the real public schema, with unknown draw order sorted.
fn project_combat(rm: &RunManager, state: &mut Value) -> Result<(), String> {
    let combat = rm.combat.as_ref().ok_or("alignment_missing_combat")?;
    let wire = emit::combat::combat_state_to_value(combat).map_err(|e| e.to_string())?;
    let power_uids = combat.player_power_uids();
    let mut player = json!({"hp":format!("{}/{}",combat.player.current_hp,combat.player.max_hp),
        "energy":combat.player.energy,"stars":combat.player.stars,"block":combat.player.block,
        "power_details":public_powers(combat,&power_uids)?,"powers":[],
        "orb_capacity":wire["player"]["orb_slots"],"orb_details":wire["player"]["orbs"].as_array().into_iter().flatten().map(|orb|json!({
            "orb_id":orb["type"],"passive_value":orb["passive"],"evoke_value":orb["evoke"]})).collect::<Vec<_>>()});
    player["powers"] = json!(power_lines(&player["power_details"]));
    if let Some(uid) = combat.player.pet_uid {
        if let Some(pet) = combat.creatures.iter().find(|c| c.id == uid.0) {
            player["pet_details"] = json!([{"pet_id":"OSTY",
            "current_hp":pet.current_hp,"max_hp":pet.max_hp,"block":pet.block,"power_details":public_powers(combat,&pet.powers)?}]);
        }
    }
    let mut enemies = Vec::new();
    for (index, (_, uid)) in public_enemy_roster(combat)?.iter().enumerate() {
        let enemy = combat
            .creatures
            .iter()
            .find(|c| c.id == uid.0)
            .ok_or("alignment_missing_enemy")?;
        let alive = enemy.current_hp > 0;
        // Only the currently announced telegraph is public, never its private move id or history.
        let intents = enemy
            .ai
            .as_ref()
            .map(|ai| ai.current_move().intents.to_vec())
            .unwrap_or_else(|| enemy.intent.into_iter().collect());
        let visible=intents.into_iter().map(|intent|{let value=emit::combat::intent_to_value(intent,combat,*uid);
            json!({"intent_type":intent.python_name(),"damage":value["effective_amount"],"hits":value["times"].as_i64().unwrap_or(1)})}).collect::<Vec<_>>();
        let details = public_powers(combat, &enemy.powers)?;
        enemies.push(json!({"i":index,"enemy_id":enemy.species.map(|k|k.python_name()),"instance_id":format!("visible-enemy-{}",uid.0.get()),
            "hp":format!("{}/{}",enemy.current_hp,enemy.max_hp),"block":enemy.block,"alive":alive,"hittable":alive,
            "powers":power_lines(&details),"power_details":details,"intents":visible}));
    }
    let mut projected = json!({"player":player,"enemies":enemies,"players":[],
        "action_readiness":{"snapshot_stable":true,"actions_settled":combat.pending_choice.is_none(),
            "can_use_combat_actions":combat.pending_choice.is_none()}});
    for (name, pile) in [
        ("hand", combat.player.hand.as_slice()),
        ("draw", combat.player.draw.as_slice()),
        ("discard", combat.player.discard.as_slice()),
        ("exhaust", combat.player.exhaust.as_slice()),
    ] {
        let mut cards = pile
            .iter()
            .enumerate()
            .map(|(index, uid)| combat_card(combat, *uid, index))
            .collect::<Result<Vec<_>, _>>()?;
        if name != "hand" {
            for row in &mut cards {
                row.as_object_mut().unwrap().remove("i");
                row.as_object_mut().unwrap().remove("instance_id");
            }
            // Sort only the published face, after removing private order and unseen identities.
            cards.sort_by_cached_key(Value::to_string);
        }
        projected[name] = json!(cards);
    }
    if let Some(choice) = combat.pending_choice.as_ref() {
        if matches!(
            choice.kind,
            sts2_core::ChoiceKind::OrbSlot | sts2_core::ChoiceKind::EnemyTarget
        ) {
            return Err("alignment_non_card_choice_ui_not_projected".into());
        }
        let hand_selection = choice_uses_visible_hand(combat);
        let generated = choice
            .options
            .iter()
            .all(|option| option.card_uid.is_none());
        let mut cards = choice
            .options
            .iter()
            .enumerate()
            .map(|(index, option)| {
                let mut card = if let Some(uid) = option.card_uid {
                    combat_card(combat, uid, index)?
                } else {
                    generated_card(combat, option, index)?
                };
                card["i"] = json!(index);
                card["selected"] = json!(choice.picked.contains(&(index as u8)));
                // The actual producer currently leaves this predicate unknown.
                card["selectable"] = Value::Null;
                // Native publishes the pile type, but only Hand/Play have a public positional index.
                card["source_pile"] = option
                    .source_pile
                    .map(|pile| json!(format!("{pile:?}")))
                    .unwrap_or(Value::Null);
                card["source_index"] = Value::Null;
                if let (Some(pile), Some(uid)) = (option.source_pile, option.card_uid) {
                    let visible = match pile {
                        sts2_core::cards::PileKind::Hand => Some(combat.player.hand.as_slice()),
                        sts2_core::cards::PileKind::Play => Some(combat.player.play.as_slice()),
                        _ => None,
                    };
                    if let Some(index) =
                        visible.and_then(|cards| cards.iter().position(|card| *card == uid))
                    {
                        card["source_pile"] = json!(format!("{pile:?}"));
                        card["source_index"] = json!(index);
                    }
                }
                Ok(card)
            })
            .collect::<Result<Vec<_>, String>>()?;
        if hand_selection {
            if choice.options.iter().any(|option| {
                option
                    .card_uid
                    .is_none_or(|uid| !combat.player.hand.contains(&uid))
            }) {
                return Err("alignment_hand_choice_stage_not_representable".into());
            }
            // This simulator has no UI holders; stage the full public hand with eligibility unknown.
            cards = combat
                .player
                .hand
                .iter()
                .enumerate()
                .map(|(index, uid)| {
                    let mut row = combat_card(combat, *uid, index)?;
                    let option = choice
                        .options
                        .iter()
                        .position(|option| option.card_uid == Some(*uid));
                    row["selected"] =
                        json!(option.is_some_and(|option| choice.picked.contains(&(option as u8))));
                    row["selectable"] = Value::Null;
                    row["source_pile"] = json!("Hand");
                    row["source_index"] = json!(index);
                    Ok(row)
                })
                .collect::<Result<Vec<_>, String>>()?;
        }
        // A generated choose-card screen has no hand/grid confirmation control; skip is not confirm.
        let can_confirm = !generated && choice.picked.len() >= usize::from(choice.min_select);
        let kind = if generated {
            "choose_card_select"
        } else if hand_selection {
            if choice.kind == sts2_core::ChoiceKind::UpgradeInHand {
                "combat_hand_upgrade_select"
            } else {
                "combat_hand_select"
            }
        } else {
            "deck_card_select"
        };
        state["screen"] = json!("CARD_SELECTION");
        state["modal"] = json!({"underlying_screen":"COMBAT"});
        state["selection"] = json!({"kind":kind,"underlying_screen":"COMBAT","min":choice.min_select,"max":choice.max_select,
            "selected":choice.picked.len(),"requires_confirmation":!generated&&(choice.max_select>1||choice.min_select<choice.max_select),
            "confirm":can_confirm,"can_skip":generated&&choice.can_skip,"cards":cards});
        let mut available = vec!["select_deck_card"];
        if can_confirm {
            available.push("confirm_selection");
        }
        if generated && choice.can_skip {
            available.push("skip_selection");
        }
        state["available_actions"] = json!(available);
    } else {
        state["available_actions"] = json!(["play_card", "end_turn", "use_potion"]);
    }
    projected["play"] = json!(combat
        .player
        .play
        .iter()
        .enumerate()
        .map(|(i, uid)| combat_card(combat, *uid, i))
        .collect::<Result<Vec<_>, _>>()?);
    state["turn"] = json!(combat.round);
    state["combat"] = projected;
    state["run"]["max_energy"] = json!(combat.player.max_energy);
    state["run"]["act_index"] = json!(combat.act_index);
    // Reviewed Native-history equivalents are attached separately; unknown observations stay null.
    Ok(())
}

/// Remember opaque identities only after the card appeared on a visible hand, play or choice surface.
fn remember_visible_cards(source: &Value, known: &mut BTreeSet<String>) {
    for rows in [
        &source["combat"]["hand"],
        &source["combat"]["play"],
        &source["selection"]["cards"],
    ] {
        for row in rows.as_array().into_iter().flatten() {
            if let Some(id) = row["instance_id"].as_str().filter(|id| !id.is_empty()) {
                known.insert(id.to_owned());
            }
        }
    }
}

/// Group a public knowledge band by visible face, retaining only identities observed earlier.
fn knowledge_card_rows(
    combat: &sts2_core::CombatState,
    members: &[sts2_core::CardUid],
    known: &BTreeSet<String>,
) -> Result<Vec<Value>, String> {
    let mut faces = BTreeMap::<String, (Value, usize, BTreeSet<String>)>::new();
    for uid in members {
        let mut row = combat_card(combat, *uid, 0)?;
        let instance = row["instance_id"].as_str().unwrap().to_owned();
        row.as_object_mut().unwrap().remove("i");
        row.as_object_mut().unwrap().remove("instance_id");
        let (_, count, identities) = faces
            .entry(row.to_string())
            .or_insert_with(|| (row, 0, BTreeSet::new()));
        *count += 1;
        if known.contains(&instance) {
            identities.insert(instance);
        }
    }
    Ok(faces
        .into_values()
        .map(|(mut row, count, identities)| {
            row["card_count"] = json!(count);
            row["line"] = json!(format!(
                "{}{}*{count}",
                row["card_id"].as_str().unwrap_or(""),
                if row["upgraded"] == true { "+" } else { "" }
            ));
            row["known_instance_ids"] = json!(identities.into_iter().collect::<Vec<_>>());
            row
        })
        .collect())
}

/// Export recorded public completions and block attribution without deriving events from counters.
fn attach_public_combat_history(
    combat: &sts2_core::CombatState,
    source: &mut Value,
    known: &BTreeSet<String>,
) {
    let mut plays = Vec::new();
    let mut blocks = Vec::new();
    let mut complete = false;
    let mut blocks_complete = false;
    let history = combat.combat_log.event_log.public_history();
    if let Some(history) = history {
        complete = history.complete;
        blocks_complete = history.blocks_complete();
        let mut completed = BTreeMap::new();
        for (sequence, play) in history.plays.iter().enumerate() {
            let card = combat.cards.iter().find(|card| card.uid == play.card_uid);
            if card.is_none() {
                complete = false;
                blocks_complete = false;
            }
            if completed.insert(play.token, sequence).is_some() {
                complete = false;
                blocks_complete = false;
            }
            // Native history reads the referenced card object when serializing, rather than a play-time face.
            let id = card.map(|card| card.card_id).unwrap_or(play.card_id);
            let kind = card.map(|card| card.card_type).unwrap_or(play.card_type);
            let instance = format!("visible-{}", play.card_uid.get());
            plays.push(json!({"sequence":sequence,"card_id":id.to_python_name(),
                "card_type":format!("{kind:?}"),"auto_played":play.auto_played,
                "first_in_series":play.first_in_series,
                "was_ethereal":play.was_ethereal,"energy_value":play.energy_value,
                "energy_spent":play.energy_spent,"stars_spent":play.stars_spent,
                "this_turn":play.round==combat.round&&play.side==combat.side,
                "known_instance_id":known.contains(&instance).then_some(instance)}));
        }
        for block in &history.blocks {
            let Some(sequence) = completed.get(&block.play_token).copied() else {
                blocks_complete = false;
                // Native omits an unfinished reference and marks the entire summary incomplete.
                continue;
            };
            blocks.push(json!({"card_play_index":sequence,"amount":block.amount,
                "this_turn":block.round==combat.round&&block.side==combat.side}));
        }
    }
    source["combat"]["public_card_play_history"] = json!({"available":history.is_some(),
        "complete":complete,"scope":"current_combat","source":"offline_public_completed_card_plays","plays":plays});
    source["combat"]["public_card_block_history"] = json!({"available":history.is_some(),
        "complete":complete&&blocks_complete,"scope":"current_combat",
        "source":"offline_public_card_sourced_block_gains","entries":blocks});
}

/// Keep per-target hit identities in the public enemy namespace, never in slot-index coordinates.
fn public_hit_statistics(
    combat: &sts2_core::CombatState,
    hits: &[(sts2_core::CreatureUid, u32)],
) -> Value {
    if hits.iter().any(|(uid, _)| !combat.enemy_uids.contains(uid)) {
        return Value::Null;
    }
    let mut rows = hits
        .iter()
        .filter(|(_, count)| *count > 0)
        .map(|(uid, count)| {
            json!({"instance_id":format!("visible-enemy-{}",uid.0.get()),"hits":count})
        })
        .collect::<Vec<_>>();
    rows.sort_by_cached_key(|row| row["instance_id"].as_str().unwrap().to_owned());
    json!(rows)
}

/// Export only aggregates with an established Native public-history meaning and combat-birth coverage.
fn attach_public_combat_statistics(combat: &sts2_core::CombatState, source: &mut Value) {
    let events = &combat.combat_log;
    let mut statistics = json!({
        "energy_spent_this_turn":events.energy_spent_this_turn,
        "hp_loss_events_this_turn":events.hp_lost_this_turn,
        "hp_loss_events_this_combat":events.unblocked_damage_received_this_combat,
        "doom_applied_this_turn":events.doom_applied_by_player_this_turn,
        "cards_exhausted_this_turn":events.cards_exhausted_this_turn,
        "cards_discarded_this_turn":events.cards_discarded_this_turn,
        "cards_drawn_by_effect_this_turn":events.cards_drawn_by_effect_this_turn,
        "status_cards_drawn_this_turn":events.status_cards_drawn_this_turn,
        "cards_drawn_this_combat":(combat.cards_drawn<u16::MAX).then_some(combat.cards_drawn),
        "lightning_orbs_channeled_this_combat":events.lightning_orbs_channeled_this_combat,
        "stars_gained_this_turn":combat.player.stars_gained_this_turn,
        "osty_attacks_this_turn":events.osty_attacks_this_turn,
        "powered_hits_by_target_this_turn":public_hit_statistics(combat,&combat.player.powered_hits_by_target_this_turn),
        // This simulator is solo: the supported other same-side dealer is Osty.
        "ally_powered_hits_by_target_this_turn":public_hit_statistics(combat,&events.osty_powered_hits_by_target_this_turn),
        "generated_cards_this_combat":combat.player.generated_cards_this_combat,
        // Native's observed pre-turn-end bank includes Ice Cream carry, not merely discarded energy.
        "unspent_energy_at_turn_end_sum":combat.energy_wasted,
        "energy_wasted_this_combat":combat.energy_wasted
    });
    // The run factory arms this observer before initial hooks; a missing observer cannot prove prior coverage.
    if events.event_log.public_history().is_none() {
        for value in statistics.as_object_mut().unwrap().values_mut() {
            *value = Value::Null;
        }
    }
    for (key, value) in statistics.as_object().unwrap() {
        source["combat"][key] = value.clone();
    }
}

/// Serialize the simulator's public rule tracker through the actual compact knowledge contract.
fn attach_draw_knowledge(
    combat: &sts2_core::CombatState,
    source: &mut Value,
    known: &BTreeSet<String>,
) -> Result<(), String> {
    if !combat
        .cards
        .iter()
        .any(|card| card.has_enchantment(sts2_core::cards::EnchantmentId::PerfectFit))
        && combat.player.draw_knowledge.is_none()
        && combat.player.pinned_top.is_empty()
        && combat.player.pinned_bottom.is_empty()
    {
        return Ok(());
    }
    let knowledge =
        sts2_core::draw_knowledge::export_public_knowledge(&combat.player, &combat.cards);
    let bands = knowledge
        .bands
        .iter()
        .map(|members| Ok(json!({"cards":knowledge_card_rows(combat, members, known)?})))
        .collect::<Result<Vec<Value>, String>>()?;
    let discards = knowledge
        .discard_bands
        .iter()
        .map(|members| Ok(json!({"cards":knowledge_card_rows(combat, members, known)?})))
        .collect::<Result<Vec<Value>, String>>()?;
    source["combat"]["draw_knowledge"] = json!({
        "schema_version":1,"available":knowledge.available,"reason":knowledge.reason,
        "source":"offline_public_rule_observer","kind":"public_rule_state",
        "bands":bands,"inserted":knowledge_card_rows(combat, &knowledge.inserted, known)?,
        "discard_bands":discards,
    });
    Ok(())
}

/// Publish unordered bindings for previously seen instances, never new hidden IDs or hidden pile order.
fn attach_known_pile_ids(
    rm: &RunManager,
    source: &mut Value,
    known: &BTreeSet<String>,
) -> Result<(), String> {
    let combat = rm.combat.as_ref().ok_or("alignment_missing_combat")?;
    for (name, pile) in [
        ("draw", &combat.player.draw),
        ("discard", &combat.player.discard),
        ("exhaust", &combat.player.exhaust),
    ] {
        let mut bindings = BTreeMap::<String, Vec<String>>::new();
        for uid in pile {
            let id = format!("visible-{}", uid.get());
            if !known.contains(&id) {
                continue;
            }
            let mut face = combat_card(combat, *uid, 0)?;
            face.as_object_mut().unwrap().remove("i");
            face.as_object_mut().unwrap().remove("instance_id");
            bindings.entry(face.to_string()).or_default().push(id);
        }
        for ids in bindings.values_mut() {
            ids.sort();
            ids.reverse();
        }
        let rows = source["combat"][name]
            .as_array_mut()
            .ok_or("alignment_public_pile_missing")?;
        for row in rows.iter_mut() {
            let id = bindings.get_mut(&row.to_string()).and_then(Vec::pop);
            row["known_instance_ids"] = json!(id.into_iter().collect::<Vec<_>>());
        }
        rows.sort_by_cached_key(Value::to_string);
    }
    attach_draw_knowledge(combat, source, known)?;
    attach_public_combat_history(combat, source, known);
    attach_public_combat_statistics(combat, source);
    Ok(())
}

/// Convert only an action actually submitted to this world into its public control vocabulary.
fn witnessed_action(rm: &RunManager, action: RunAction) -> Option<Value> {
    Some(match action {
        RunAction::PlayCard { hand_idx, target } => {
            let target = witnessed_enemy_target(rm, target).ok()?;
            json!({"type":"play_card","card_index":hand_idx,"target_index":target})
        }
        RunAction::EndTurn => json!({"type":"end_turn"}),
        RunAction::DrinkPotion {
            potion_slot,
            target,
        } => {
            let state = rm.combat.as_ref()?;
            let target = witnessed_enemy_target(rm, target).ok()?;
            json!({"type":"use_potion","option_index":state.player.potion_slot_indices.get(potion_slot).copied().unwrap_or(potion_slot as u8),"target_index":target})
        }
        RunAction::CombatResolveChoice {
            option: Some(index),
        } => {
            let combat = rm.combat.as_ref()?;
            let index = if choice_uses_visible_hand(combat) {
                let uid = combat
                    .pending_choice
                    .as_ref()?
                    .options
                    .get(index)?
                    .card_uid?;
                combat
                    .player
                    .hand
                    .iter()
                    .position(|candidate| *candidate == uid)?
            } else {
                index
            };
            json!({"type":"select_deck_card","option_index":index})
        }
        RunAction::CombatResolveChoice { option: None } => {
            json!({"type":if rm.combat.as_ref().and_then(|state|state.pending_choice.as_ref())
            .is_some_and(|choice|choice.can_skip&&choice.picked.is_empty()&&choice.options.iter().all(|option|option.card_uid.is_none())){"skip_selection"}else{"confirm_selection"}})
        }
        _ => return None,
    })
}

/// Resolve the same five character names accepted by the upstream arena.
fn character(value: &str) -> Result<CharacterId, String> {
    match value.to_ascii_lowercase().as_str() {
        "ironclad" => Ok(CharacterId::Ironclad),
        "silent" => Ok(CharacterId::Silent),
        "defect" => Ok(CharacterId::Defect),
        "regent" => Ok(CharacterId::Regent),
        "necrobinder" => Ok(CharacterId::Necrobinder),
        _ => Err("alignment_unknown_character".into()),
    }
}

/// Build a native public card descriptor without exporting engine scratch variables.
fn deck_card(id: &str, upgraded: bool, enchantment: Option<&Option<(String, i64)>>) -> Value {
    // Native DTOs publish one canonical CardId spelling across the deck and every offer pool.
    let canonical = sts2_content::names::resolve_card_name(id, None)
        .ok()
        .map(|spec| spec.id.to_python_name());
    let id = canonical.unwrap_or(id);
    let mut value = json!({"card_id":id,"card_ids":[id],"card_count":1,"upgraded":upgraded,
        "line":format!("{id}{}*1",if upgraded {"+"}else{""})});
    if let Some(Some((name, amount))) = enchantment {
        value["enchantment"] = json!({"enchantment_id":name,"amount":amount});
    }
    value
}

/// Render only relic counters whose native public display mapping has been checked.
fn public_relic_display(relic: sts2_core::RelicId, run: &sts2_core::runstate::RunState) -> Value {
    let counter = run.relic_counters.get(&relic).copied().unwrap_or(0);
    match relic {
        sts2_core::RelicId::BookOfFiveRings => json!(counter),
        // Native HappyFlower displays 0,1,2 across fights; Core stores the lifetime turn count.
        sts2_core::RelicId::HappyFlower => json!(counter.rem_euclid(3)),
        _ => Value::Null,
    }
}

/// Project only Red Skull's reviewed public status, never a status inferred from current HP.
fn public_relic_status(
    relic: sts2_core::RelicId,
    combat: Option<&sts2_core::CombatState>,
) -> Value {
    if relic != sts2_core::RelicId::RedSkull {
        return Value::Null;
    }
    let Some(instance) = combat.and_then(|state| {
        state
            .relics
            .iter()
            .find(|instance| instance.kind == sts2_core::RelicKind::RedSkull)
    }) else {
        return Value::Null;
    };
    // Melting remains a separate published fact; no disabled status is guessed from it.
    if instance.is_melted {
        return Value::Null;
    }
    match instance.counter {
        0 => json!("Normal"),
        1 => json!("Active"),
        _ => Value::Null,
    }
}

/// Preserve only map topology, visible room types and the already revealed boss identity.
fn public_map(rm: &RunManager, actions: &[RunAction]) -> Value {
    let full = emit::run_manager::full_map(rm);
    let mut nodes = Vec::new();
    for row in full["rows"].as_array().into_iter().flatten() {
        for node in row.as_array().into_iter().flatten() {
            nodes.push(json!({"coord":{"row":node["row"],"col":node["col"]},
                "node_type":node["type"],"children":node["children"],"visited":node["visited"]}));
        }
    }
    let boss = &full["boss"];
    if boss.is_object()
        && !nodes
            .iter()
            .any(|node| node["coord"]["row"] == boss["row"] && node["coord"]["col"] == boss["col"])
    {
        nodes.push(
            json!({"coord":{"row":boss["row"],"col":boss["col"]},"node_type":"Boss","children":[]}),
        );
    }
    let current = full["current_coord"].clone();
    let next_row = current["row"].as_i64().unwrap_or(0) + 1;
    let options: Vec<_> = actions
        .iter()
        .enumerate()
        .filter_map(|(index, action)| {
            let RunAction::Move { col } = action else {
                return None;
            };
            nodes
                .iter()
                .find(|node| {
                    node["coord"]["col"].as_i64() == Some(i64::from(*col))
                        && node["coord"]["row"].as_i64() == Some(next_row)
                })
                .map(|node| json!({"i":index,"coord":node["coord"]}))
        })
        .collect();
    let compact = |coord: &Value| {
        if coord["row"].is_i64() && coord["col"].is_i64() {
            json!(format!("{},{}", coord["row"], coord["col"]))
        } else {
            Value::Null
        }
    };
    let compact_nodes=nodes.iter().map(|node|json!({"coord":compact(&node["coord"]),"node_type":node["node_type"],
        "visited":node["visited"],"children":node["children"].as_array().into_iter().flatten().map(&compact).collect::<Vec<_>>()})).collect::<Vec<_>>();
    json!({"nodes":compact_nodes,"options":options,"current":compact(&current),
        "boss_node":compact(boss),"second_boss_node":compact(&full["second_boss"])})
}

/// Preserve each original offer pool's order, eligibility and physical upgrade/enchantment state.
fn run_selection_cards(rm: &RunManager, operation: &str) -> Vec<Value> {
    let rs = &rm.run_state;
    let pool: Vec<&sts2_core::cards::Card> = match operation {
        "remove" => rs
            .removal_offer_order()
            .into_iter()
            .map(|index| &rs.master_deck[index])
            .collect(),
        "upgrade" => rs
            .master_deck
            .iter()
            .filter(|card| card.is_upgradable())
            .collect(),
        "transform" => rm
            .transform_eligible_deck_indices()
            .into_iter()
            .map(|index| &rs.master_deck[index])
            .collect(),
        "enchant" => rm
            .enchant_eligible_deck_indices()
            .into_iter()
            .map(|index| &rs.master_deck[index])
            .collect(),
        "grant" => rs
            .pending_card_grants
            .first()
            .map(|grant| grant.options.iter().collect())
            .unwrap_or_default(),
        _ => Vec::new(),
    };
    if operation == "smith" {
        let enchantments = emit::run_manager::smith_option_enchantments(rm);
        return rs
            .pending_choice
            .as_ref()
            .into_iter()
            .flat_map(|choice| choice.options.iter())
            .enumerate()
            .map(|(index, option)| {
                let mut row = deck_card(
                    option.card_id.to_python_name(),
                    option.upgraded,
                    enchantments.get(index),
                );
                row["i"] = json!(index);
                row
            })
            .collect();
    }
    pool.into_iter()
        .enumerate()
        .map(|(index, card)| {
            let enchantment = emit::run_manager::card_enchantment(&card.enchantments);
            let mut row = deck_card(
                card.card_id.to_python_name(),
                card.upgraded,
                Some(&enchantment),
            );
            row["i"] = json!(index);
            row
        })
        .collect()
}

/// Project actual revealed run facts through the production public observation vocabulary.
fn project_run(rm: &RunManager, actions: &[RunAction]) -> Result<Value, String> {
    let surface = classify_surface(rm.phase(), actions);
    let obs = build_obs(rm, phase_str(rm.phase()), surface);
    let map = public_map(rm, actions);
    let full = emit::run_manager::full_map(rm);
    let deck: Vec<_> = obs
        .deck
        .iter()
        .enumerate()
        .map(|(index, (id, upgraded))| {
            deck_card(
                id,
                *upgraded,
                obs.deck_enchantments
                    .as_ref()
                    .and_then(|rows| rows.get(index)),
            )
        })
        .collect();
    // Combat persists after settlement; only its active phase owns the visible belt.
    let occupied: Vec<_> = if let Some(combat) =
        rm.combat.as_ref().filter(|_| rm.phase() == Phase::Combat)
    {
        combat.player.potions.iter().enumerate().map(|(i,p)|json!({
        "i":combat.player.potion_slot_indices.get(i).copied().unwrap_or(i as u8),"potion_id":p.python_name(),"occupied":true,"usable":true})).collect()
    } else {
        rm.run_state
            .potions
            .iter()
            .enumerate()
            .map(|(i, p)| {
                json!({"i":i,
        "potion_id":p.python_name(),"occupied":true,"usable":true})
            })
            .collect()
    };
    let capacity = rm
        .combat
        .as_ref()
        .filter(|_| rm.phase() == Phase::Combat)
        .map(|combat| usize::from(combat.player.potion_slots))
        .filter(|capacity| *capacity > 0)
        .unwrap_or_else(|| rm.run_state.max_potion_slots.max(0) as usize)
        .max(
            occupied
                .iter()
                .filter_map(|row| row["i"].as_u64())
                .map(|slot| slot as usize + 1)
                .max()
                .unwrap_or(0),
        );
    let mut potions = (0..capacity)
        .map(|index| json!({"i":index,"potion_id":null,"occupied":false,"usable":false}))
        .collect::<Vec<_>>();
    for potion in occupied {
        let index = potion["i"].as_u64().unwrap() as usize;
        potions[index] = potion;
    }
    for potion in &mut potions {
        potion["discard"] = json!(potion["occupied"] == true);
    }
    let mut state = json!({"screen":"","supported":true,"run":{
        "character_id":format!("{:?}",rm.character_id).to_ascii_uppercase(),
        "ascension":rm.ascension_level,"hp":format!("{}/{}",rm.run_state.current_hp,rm.run_state.max_hp),
        "gold":rm.run_state.gold,"max_energy":rm.combat.as_ref().map(|c|c.player.max_energy),
        "act_index":rm.run_state.act_index,"act_count":rm.run_state.acts.len(),"is_last_act":rm.run_state.is_last_act(),
        "act_floor":rm.run_state.act_floor,"map_coord":rm.run_state.current_room.as_ref().and_then(|room|room.map_coord)
            .map(|coord|json!({"row":coord.row,"col":coord.col})),
        "awaiting_second_boss":rm.run_state.awaiting_second_boss(),
        "room_type":rm.run_state.current_room.as_ref().map(|room|format!("{:?}",room.room_type)),
        "act_id":rm.run_state.act_index.saturating_sub(1).to_string(),"floor":rm.run_state.total_floor+1,
        "deck":deck,"deck_size":obs.deck_size,"relic_ids":rm.run_state.relics.iter().map(|r|r.to_python_name()).collect::<Vec<_>>(),
        "relic_stacks":rm.run_state.relics.iter().map(|relic|public_relic_display(*relic,&rm.run_state)).collect::<Vec<_>>(),
        "relic_statuses":rm.run_state.relics.iter().map(|relic|public_relic_status(*relic,
            rm.combat.as_ref().filter(|_|rm.phase()==Phase::Combat))).collect::<Vec<_>>(),
        "relic_is_melted":rm.run_state.relics.iter().map(|relic|rm.run_state.melted_relics.contains(relic)).collect::<Vec<_>>(),
        "potions":potions,
        "boss_id":full["boss"]["id"]},"available_actions":[]});
    if rm.phase() == Phase::Combat {
        if let Some(combat) = rm.combat.as_ref() {
            state["run"]["hp"] = json!(format!(
                "{}/{}",
                combat.player.current_hp, combat.player.max_hp
            ));
            state["run"]["gold"] = json!(combat.gold);
        }
    }
    let selection_kind = actions.first().and_then(|action| match action {
        RunAction::RemoveDeckCard { .. } => Some(("deck_card_select", "remove")),
        RunAction::UpgradeDeckCard { .. } => Some(("deck_upgrade_select", "upgrade")),
        RunAction::TransformDeckCard { .. } => Some(("deck_transform_select", "transform")),
        RunAction::EnchantDeckCard { .. } => Some(("deck_enchant_select", "enchant")),
        RunAction::GrantDeckCard { .. } => Some(("choose_card_select", "grant")),
        RunAction::SmithCard { .. } => Some(("deck_upgrade_select", "smith")),
        _ => None,
    });
    if let Some((kind, operation)) = selection_kind {
        let cards = run_selection_cards(rm, operation);
        state["screen"] = json!("CARD_SELECTION");
        state["selection"] = json!({"kind":kind,"operation":operation,"min":1,"max":1,"selected":0,"confirm":false,
            "cards":cards});
        state["available_actions"] = json!(["select_deck_card"]);
    } else {
        if actions
            .iter()
            .all(|action| matches!(action, RunAction::PotionSwap { .. }))
            && !actions.is_empty()
        {
            state["screen"] = json!("REWARD");
            let allowed = !sts2_core::run_effects::would_veto_procure_potion(&rm.run_state);
            let slot = rm.run_state.potions.len() < (rm.run_state.max_potion_slots.max(0) as usize);
            // Pending queues contain revealed offers that have not been claimed or declined.
            let mut rewards=rm.run_state.pending_potion_swaps.iter().enumerate().map(|(i,potion)|json!({
                    "i":i,"reward_type":"Potion","potion_id":potion.python_name(),"claimable":allowed,"visible":true,"successfully_selected":false,
                    "potion_slot_available":slot,"potion_procurement_allowed":allowed,"potion_can_receive":allowed&&slot})).collect::<Vec<_>>();
            if rm.phase() == Phase::CardReward {
                for reward in &rm.run_state.relic_rewards {
                    rewards.push(json!({"i":rewards.len(),"reward_type":"Relic",
                    "relic_id":reward.relic_id.to_python_name(),"claimable":true,"visible":true,"successfully_selected":false}));
                }
                if !rm.card_reward_options().is_empty() {
                    rewards.push(json!({"i":rewards.len(),"reward_type":"Card","claimable":true,"visible":true,"successfully_selected":false}));
                }
            }
            state["reward"] = json!({"pending_card_choice":false,"can_proceed":true,"cards":[],"rewards":rewards});
            state["available_actions"] = json!(["claim_reward", "discard_potion", "proceed"]);
        } else {
            match rm.phase() {
                Phase::MapChoice => {
                    state["screen"] = json!("MAP");
                    state["map"] = map.clone();
                    state["available_actions"] = json!(["choose_map_node"]);
                }
                Phase::CardReward
                    if actions
                        .iter()
                        .any(|a| matches!(a, RunAction::PickCardReward { .. })) =>
                {
                    state["screen"] = json!("REWARD");
                    state["reward"] = json!({"cards":rm.card_reward_options().iter().enumerate().map(|(i,id)|{
                let mut c=deck_card(id.to_python_name(),rm.card_reward_upgraded_flags().get(i).copied().unwrap_or(false),None);
                c["i"]=json!(i);c
            }).collect::<Vec<_>>()});
                    let mut legal = vec!["choose_reward_card"];
                    if actions.contains(&RunAction::PickCardReward { idx: None }) {
                        legal.push("skip_reward_cards");
                    }
                    state["available_actions"] = json!(legal);
                }
                Phase::CardReward if !rm.run_state.relic_rewards.is_empty() => {
                    state["screen"] = json!("REWARD");
                    state["reward"] = json!({"pending_card_choice":false,"can_proceed":true,"cards":[],
                    "rewards":rm.run_state.relic_rewards.iter().enumerate().map(|(i,reward)|json!({"i":i,
                        "reward_type":"Relic","relic_id":reward.relic_id.to_python_name(),"claimable":true,"visible":true,"successfully_selected":false})).collect::<Vec<_>>()});
                    state["available_actions"] = json!(["claim_reward", "proceed"]);
                }
                Phase::BossRelic => {
                    let event = rm
                        .run_state
                        .current_event
                        .ok_or("alignment_ancient_event_not_materialized")?;
                    state["screen"] = json!("EVENT");
                    state["available_actions"] = json!(["choose_event_option"]);
                    // The real active event is bound by the engine. Ordinary event presentation
                    // would reroll a different palette, so expose the already presented cache only.
                    let options = rm.boss_relic_options().iter().enumerate().map(|(index, relic)| {
                        let id = relic.to_python_name();
                        let title_key = format!("{id}.title");
                        let description_key = format!("{id}.eventDescription");
                        json!({"i":index,"text_key":format!("{}.pages.INITIAL.options.{id}",event.python_name()),
                            "label":RELIC_TEXT[&title_key].as_str().unwrap_or(id),"description":"",
                            "label_loc_key":title_key,"label_loc_table":"relics",
                            "description_template":RELIC_TEXT[&description_key],"description_loc_key":description_key,
                            "description_loc_table":"relics","description_text_partial":true,
                            "locked":false,"kill":null,"preview_relic_ids":[id]})
                    }).collect::<Vec<_>>();
                    state["event"] = json!({"id":event.python_name(),"options":options});
                }
                Phase::Event => {
                    state["screen"] = json!("EVENT");
                    state["available_actions"] = json!(["choose_event_option"]);
                    let source = emit::run_manager::event_option_infos(rm);
                    let revealed = if rm
                        .run_state
                        .current_event
                        .is_some_and(|event| event.python_name().eq_ignore_ascii_case("neow"))
                    {
                        emit::run_manager::neow_offers(rm).map(|(a, b, c)| vec![a, b, c])
                    } else {
                        None
                    };
                    let options:Vec<_>=source.as_array().into_iter().flatten().enumerate().map(|(i,row)|{
                let mut option=json!({"i":i,"text_key":row["loc_key"].as_str().filter(|s|!s.is_empty()).unwrap_or_else(||row["option_id"].as_str().unwrap_or("visible_option")),
                    "label":row["label"],"description":row["description"],"locked":row["enabled"]==false,
                    "kill":null,"preview_relic_ids":revealed.as_ref().and_then(|offers|offers.get(i)).map(|id|vec![id]),
                    "vars":row["vars"],"title_vars":row["title_vars"]});
                public_option_text(row,&mut option);option
            }).collect();
                    state["event"] = json!({"id":rm.run_state.current_event.map(|e|e.python_name()),"options":options});
                }
                Phase::Shop => {
                    state["screen"] = json!("SHOP");
                    let inv = emit::run_manager::shop_inventory(rm);
                    let items = |key: &str, idkey: &str| {
                        inv[key].as_array().into_iter().flatten().map(|row|json!({
                "i":row["idx"],idkey:row["id"],"price":row["price"],"stocked":row["available"],
                "affordable":row["available"]==true&&row["price"].as_i64().is_some_and(|p|p<=i64::from(rm.run_state.gold)),"upgraded":false})).collect::<Vec<_>>()
                    };
                    let mut cards = items("cards", "card_id");
                    if let Some(inventory) = rm.shop_inventory() {
                        for (row, entry) in cards
                            .iter_mut()
                            .zip(inventory.cards.iter().chain(&inventory.colorless_cards))
                        {
                            row["upgraded"] =
                                json!(entry.card.as_ref().is_some_and(|card| card.upgraded));
                        }
                    }
                    state["shop"] = json!({"open":true,"cards":cards,"relics":items("relics","relic_id"),"potions":items("potions","potion_id"),
                "remove":{"available":inv["removal_used"]==false,"price":inv["removal_cost"],"affordable":inv["removal_cost"].as_i64().is_some_and(|p|p<=i64::from(rm.run_state.gold))}});
                    state["available_actions"] = json!([
                        "remove_card_at_shop",
                        "close_shop_inventory",
                        "buy_card",
                        "buy_relic",
                        "buy_potion"
                    ]);
                }
                Phase::RestSite
                    if actions
                        .iter()
                        .any(|a| matches!(a, RunAction::RestOption { .. })) =>
                {
                    state["screen"] = json!("REST");
                    state["rest"] = json!({"options":emit::run_manager::rest_site_options(rm).iter().enumerate()
                .map(|(i,(id,enabled))|json!({"i":i,"option_id":if id.eq_ignore_ascii_case("heal"){"REST".into()}else{id.to_ascii_uppercase()},"enabled":enabled,"requires_target":false})).collect::<Vec<_>>()});
                    state["available_actions"] = json!(["choose_rest_option"]);
                }
                Phase::Combat => {
                    state["screen"] = json!("COMBAT");
                    project_combat(rm, &mut state)?;
                }
                _ => {
                    state["screen"] = json!("CARD_SELECTION");
                    let source = emit::run_manager::smith_options(rm);
                    let ids: Vec<_> = source.iter().map(String::as_str).collect();
                    let kind = if actions
                        .iter()
                        .any(|a| matches!(a, RunAction::SmithCard { .. }))
                    {
                        "deck_upgrade_select"
                    } else {
                        "unsupported_engine_selection"
                    };
                    state["selection"] = json!({"kind":kind,"min":1,"max":1,"selected":0,"confirm":false,
                "cards":ids.iter().enumerate().map(|(i,id)|{let mut c=deck_card(id,false,None);c["i"]=json!(i);c}).collect::<Vec<_>>()});
                    state["available_actions"] = json!(["select_deck_card"]);
                }
            }
        }
    }
    Ok(json!({"public_state":state,"public_map":map,
        "projection_notes":["run seed/RNG/unrevealed offers excluded","no engine-private effect preview outcomes imported",
            "simple public localization variables rendered; unsupported formatting retains original engine prose",
            "Ancient relic identities/titles are published; unresolved dynamic description text stays unknown"]}))
}

/// Map the selected native action back to the current engine action list without changing it.
fn map_action(response: &Value, request: &Value, actions: &[RunAction]) -> Result<usize, String> {
    let action = &response["best_action"];
    let index = action["option_index"].as_u64().map(|v| v as usize);
    let target = match action["type"].as_str().unwrap_or("") {
        "choose_map_node" => {
            let option = request["public_state"]["map"]["options"]
                .as_array()
                .and_then(|rows| {
                    rows.iter()
                        .find(|row| row["i"].as_u64() == index.map(|v| v as u64))
                })
                .ok_or("alignment_map_handle_missing")?;
            RunAction::Move {
                col: option["coord"]["col"]
                    .as_i64()
                    .ok_or("alignment_map_col_missing")? as i32,
            }
        }
        "choose_reward_card" => RunAction::PickCardReward {
            idx: Some(index.ok_or("alignment_reward_handle_missing")?),
        },
        "skip_reward_cards" => RunAction::PickCardReward { idx: None },
        "choose_event_option" => {
            if actions
                .iter()
                .any(|action| matches!(action, RunAction::PickBossRelic { .. }))
            {
                RunAction::PickBossRelic {
                    idx: index.ok_or("alignment_ancient_handle_missing")?,
                }
            } else {
                RunAction::EventChoice {
                    idx: index.ok_or("alignment_event_handle_missing")?,
                }
            }
        }
        "choose_rest_option" => RunAction::RestOption {
            idx: index.ok_or("alignment_rest_handle_missing")?,
        },
        "buy_card" => RunAction::ShopBuyCard {
            idx: index.ok_or("alignment_shop_handle_missing")?,
        },
        "buy_relic" => RunAction::ShopBuyRelic {
            idx: index.ok_or("alignment_shop_handle_missing")?,
        },
        "buy_potion" => RunAction::ShopBuyPotion {
            idx: index.ok_or("alignment_shop_handle_missing")?,
        },
        "close_shop_inventory" | "proceed" => RunAction::LeaveShop,
        "remove_card_at_shop" => {
            let carry = &response["next_context"];
            let target = &carry["target"];
            let occurrence = carry["occurrence"].as_u64().unwrap_or(0) as usize;
            let cards = request["public_state"]["run"]["deck"]
                .as_array()
                .ok_or("alignment_remove_deck_missing")?;
            let deck_idx = cards
                .iter()
                .enumerate()
                .filter(|(_, c)| {
                    c["card_id"] == target["id"] && c["upgraded"] == target["upgraded"]
                })
                .nth(occurrence)
                .map(|(idx, _)| idx)
                .ok_or("alignment_remove_context_target_missing")?;
            RunAction::ShopRemoveCard { deck_idx }
        }
        "select_deck_card" => {
            let handle = index.ok_or("alignment_selection_handle_missing")?;
            let idx = request["public_state"]["selection"]["cards"]
                .as_array()
                .and_then(|cards| {
                    cards
                        .iter()
                        .position(|card| card["i"].as_u64() == Some(handle as u64))
                })
                .ok_or("alignment_selection_handle_not_in_pool")?;
            match actions.first() {
                Some(RunAction::SmithCard { .. }) => RunAction::SmithCard { idx },
                Some(RunAction::RemoveDeckCard { .. }) => RunAction::RemoveDeckCard { idx },
                Some(RunAction::UpgradeDeckCard { .. }) => RunAction::UpgradeDeckCard { idx },
                Some(RunAction::TransformDeckCard { .. }) => RunAction::TransformDeckCard { idx },
                Some(RunAction::EnchantDeckCard { .. }) => RunAction::EnchantDeckCard { idx },
                Some(RunAction::GrantDeckCard { .. }) => RunAction::GrantDeckCard { idx },
                _ => return Err("alignment_selection_action_unknown".into()),
            }
        }
        other => return Err(format!("alignment_native_action_not_mapped:{other}")),
    };
    actions
        .iter()
        .position(|a| *a == target)
        .ok_or_else(|| format!("alignment_selected_action_not_legal:{target:?}"))
}

/// Decode the production native action before translating public handles to this persistent world.
fn map_combat_action(
    public: &Value,
    source: &Value,
    root: &PublicPosition,
    rm: &RunManager,
    actions: &[RunAction],
) -> Result<usize, String> {
    let core = root.decode_search_action(public)?;
    if !sts2_core::legal_actions(&root.state).contains(&core) {
        return Err("alignment_production_action_not_root_legal".into());
    }
    let action = root.encode_search_action(&core);
    let roster = public_enemy_roster(
        rm.combat
            .as_ref()
            .ok_or("alignment_actual_combat_missing")?,
    )?;
    let target = action["target_index"]
        .as_u64()
        .map(|index| {
            roster
                .get(index as usize)
                .map(|(core_index, _)| *core_index as u32)
                .ok_or_else(|| "alignment_public_enemy_handle_missing".to_owned())
        })
        .transpose()?;
    let wanted = match action["type"].as_str() {
        Some("play_card") => RunAction::PlayCard {
            hand_idx: action["card_index"]
                .as_u64()
                .ok_or("alignment_public_hand_handle_missing")? as usize,
            target,
        },
        Some("end_turn") => RunAction::EndTurn,
        Some("use_potion") => {
            let visible = action["option_index"]
                .as_u64()
                .ok_or("alignment_public_potion_handle_missing")? as u8;
            let combat = rm
                .combat
                .as_ref()
                .ok_or("alignment_actual_combat_missing")?;
            let slot = if combat.player.potion_slot_indices.is_empty() {
                usize::from(visible)
            } else {
                combat
                    .player
                    .potion_slot_indices
                    .iter()
                    .position(|slot| *slot == visible)
                    .ok_or("alignment_public_potion_not_occupied")?
            };
            RunAction::DrinkPotion {
                potion_slot: slot,
                target,
            }
        }
        Some("select_deck_card") => {
            let index = action["option_index"]
                .as_u64()
                .ok_or("alignment_public_choice_handle_missing")?;
            let row = source["selection"]["cards"]
                .as_array()
                .and_then(|rows| rows.iter().find(|row| row["i"].as_u64() == Some(index)))
                .ok_or("alignment_public_choice_row_missing")?;
            let choice = rm
                .combat
                .as_ref()
                .and_then(|combat| combat.pending_choice.as_ref())
                .ok_or("alignment_actual_choice_missing")?;
            let matching = if let Some(id) = row["instance_id"].as_str() {
                choice
                    .options
                    .iter()
                    .enumerate()
                    .filter_map(|(index, option)| {
                        option
                            .card_uid
                            .filter(|uid| format!("visible-{}", uid.get()) == id)
                            .map(|_| index)
                    })
                    .collect::<Vec<_>>()
            } else {
                let card = row["card_id"]
                    .as_str()
                    .and_then(|id| sts2_content::names::resolve_card_name(id, None).ok())
                    .map(|spec| spec.id);
                choice
                    .options
                    .iter()
                    .enumerate()
                    .filter_map(|(index, option)| {
                        (option.card_uid.is_none()
                            && Some(option.card_id) == card
                            && row["upgraded"].as_bool() == Some(option.upgraded))
                        .then_some(index)
                    })
                    .collect::<Vec<_>>()
            };
            if matching.len() != 1 {
                return Err("alignment_public_choice_identity_not_unique".into());
            }
            RunAction::CombatResolveChoice {
                option: Some(matching[0]),
            }
        }
        Some("confirm_selection" | "skip_selection") => {
            RunAction::CombatResolveChoice { option: None }
        }
        _ => return Err("alignment_production_action_kind_not_mapped".into()),
    };
    actions
        .iter()
        .position(|candidate| *candidate == wanted)
        .ok_or("alignment_production_action_not_actual_world_legal".into())
}

/// A declared native command may require several original legal engine edges.
#[derive(Debug)]
struct RewardMacro {
    actions: Vec<RunAction>,
    report: Value,
    next_context: Value,
}

/// Plan only declines explicitly selected by the real PPO continuation, in original engine order.
fn reward_macro_plan(
    rm: &RunManager,
    response: &Value,
    request: &Value,
) -> Result<Option<RewardMacro>, String> {
    let source = &request["public_state"];
    if source["screen"] != "REWARD" || !source["reward"]["rewards"].is_array() {
        return Ok(None);
    }
    let native = &response["best_action"];
    if !matches!(
        native["type"].as_str(),
        Some("claim_reward" | "discard_potion" | "proceed")
    ) {
        return Ok(None);
    }
    let mut declined = BTreeSet::<String>::new();
    for list in [
        &response["logical_declines_pending"],
        &response["next_context"]["deferred_rows"],
        &request["selection_context"]["deferred_rows"],
    ] {
        for item in list
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            declined.insert(item.to_owned());
        }
    }
    for step in response["policy_steps"].as_array().into_iter().flatten() {
        if let Some(key) = step["logical_decline"].as_str() {
            declined.insert(key.to_owned());
        }
    }
    let rows = source["reward"]["rewards"].as_array().unwrap();
    let target = native["option_index"]
        .as_u64()
        .and_then(|index| rows.iter().find(|row| row["i"].as_u64() == Some(index)));
    let mut actions = Vec::new();
    let mut report = json!({"native_commands":[native],"policy_steps":response["policy_steps"],"declines_authorized":declined,
        "scope":"policy_level_native_command_to_original_legal_core_edges","native_intermediate_capture":false});
    for potion in &rm.run_state.pending_potion_swaps {
        let key = format!("POTION:{}", potion.python_name());
        let chosen = native["type"] == "discard_potion"
            && response["next_context"]["claim_after_discard"]["row_key"] == key;
        if chosen {
            let slot = native["option_index"]
                .as_u64()
                .ok_or("alignment_reward_discard_slot_missing")? as usize;
            let held = source["run"]["potions"]
                .as_array()
                .ok_or("alignment_reward_belt_missing")?;
            let occupied = held
                .iter()
                .filter(|row| row["occupied"] == true)
                .collect::<Vec<_>>();
            let compact = occupied
                .iter()
                .position(|row| row["i"].as_u64() == Some(slot as u64))
                .ok_or("alignment_reward_discard_slot_not_held")?;
            actions.push(RunAction::PotionSwap {
                discard_slot: Some(compact),
            });
            // Core swaps atomically. This explicitly virtual public frame tests the real follow-up
            // consumer; it is never reported as an observed native intermediate state.
            let mut follow = request.clone();
            follow["selection_context"] = response["next_context"].clone();
            for row in follow["public_state"]["run"]["potions"]
                .as_array_mut()
                .unwrap()
            {
                if row["i"].as_u64() == Some(slot as u64) {
                    row["potion_id"] = Value::Null;
                    row["occupied"] = json!(false);
                    row["discard"] = json!(false);
                    row["usable"] = json!(false);
                }
            }
            for row in follow["public_state"]["reward"]["rewards"]
                .as_array_mut()
                .unwrap()
            {
                if row["reward_type"]
                    .as_str()
                    .is_some_and(|kind| kind.eq_ignore_ascii_case("POTION"))
                {
                    row["potion_slot_available"] = json!(true);
                    let allowed = row["potion_procurement_allowed"] == true;
                    row["potion_can_receive"] = json!(allowed);
                    row["claimable"] = json!(allowed);
                }
            }
            let followup = crate::ppo::policy_evaluate(&follow)?;
            if followup["ok"] != true || followup["best_action"]["type"] != "claim_reward" {
                return Err(format!(
                    "alignment_atomic_swap_followup_unavailable:{}",
                    followup["errors"]
                ));
            }
            let chosen_row = rows
                .iter()
                .find(|row| row["i"] == followup["best_action"]["option_index"])
                .ok_or("alignment_atomic_swap_followup_row_missing")?;
            if chosen_row["potion_id"].as_str() != Some(potion.python_name()) {
                return Err("alignment_atomic_swap_followup_identity_changed".into());
            }
            report["scope"]=json!("atomic_core_swap_with_explicit_virtual_public_discard_followup_not_native_intermediate_proof");
            report["virtual_after_discard"] = follow["public_state"].clone();
            report["followup_response"] = followup.clone();
            report["native_commands"] = json!([native, followup["best_action"]]);
            return Ok(Some(RewardMacro {
                actions,
                report,
                next_context: followup["next_context"].clone(),
            }));
        }
        if !declined.contains(&key) {
            return Err(format!(
                "alignment_reward_macro_missing_potion_decline:{key}"
            ));
        }
        actions.push(RunAction::PotionSwap { discard_slot: None });
    }
    if target.is_some_and(|row| {
        row["reward_type"]
            .as_str()
            .is_some_and(|kind| kind.eq_ignore_ascii_case("CARD"))
    }) {
        if rm.card_reward_options().is_empty() || actions.is_empty() {
            return Err("alignment_card_open_has_no_representable_core_transition".into());
        }
        report["scope"] = json!("preemptive_core_declines_then_automatic_card_choice_view");
        return Ok(Some(RewardMacro {
            actions,
            report,
            next_context: response["next_context"].clone(),
        }));
    }
    for reward in rm.run_state.relic_rewards.iter().rev() {
        let key = format!("RELIC:{}", reward.relic_id.to_python_name());
        if target.is_some_and(|row| {
            row["reward_type"]
                .as_str()
                .is_some_and(|kind| kind.eq_ignore_ascii_case("RELIC"))
                && row["relic_id"].as_str() == Some(reward.relic_id.to_python_name())
        }) {
            actions.push(RunAction::PickRelicReward);
            return Ok(Some(RewardMacro {
                actions,
                report,
                next_context: response["next_context"].clone(),
            }));
        }
        if !declined.contains(&key) {
            return Err(format!(
                "alignment_reward_macro_missing_relic_decline:{key}"
            ));
        }
        actions.push(RunAction::SkipRelic);
    }
    if native["type"] != "proceed" || actions.is_empty() {
        return Err("alignment_reward_macro_target_not_in_current_queues".into());
    }
    Ok(Some(RewardMacro {
        actions,
        report,
        next_context: response["next_context"].clone(),
    }))
}

#[derive(Debug)]
struct Meta {
    raw: PpoPolicy,
    mode: String,
    request: Value,
    context: Value,
    trace: Arc<Mutex<Vec<Value>>>,
    error: Arc<Mutex<Option<String>>>,
    pending_macro: Option<RewardMacro>,
    batch: Option<Arc<Mutex<BatchDiagnostics>>>,
}

impl DecisionTelemetry for Meta {
    /// Preserve actual PPO inference, auto-take and combat-fallback source counters.
    fn take_meta_eval(&mut self) -> Option<MetaEvalRecord> {
        self.raw.take_meta_eval()
    }
}

impl RunPolicy for Meta {
    /// Materialize event choices exactly as the original PPO policy does.
    fn prepare(&mut self, rm: &mut RunManager) {
        self.raw.prepare(rm);
    }

    /// Compare actual reachable public adapter outputs and optionally let them choose.
    fn choose(&mut self, rm: &RunManager, actions: &[RunAction]) -> Option<usize> {
        self.pending_macro = None;
        if self.mode == "raw" {
            return self.raw.choose(rm, actions);
        }
        if actions.len() == 1 {
            self.trace.lock().unwrap().push(json!({"kind":"forced_meta","phase":phase_str(rm.phase()),
                "action":emit::run_action::run_action_to_value(&actions[0]),"network_ran":false,"adapter_attempted":false}));
            return Some(0);
        }
        let raw = self.raw.try_evaluate_with_actions(rm, actions);
        let mut projection = match project_run(rm, actions) {
            Ok(p) => p,
            Err(e) => {
                if self.batch.is_some() {
                    record_alignment(&self.trace, &self.batch, json!({"kind":"meta","floor":rm.run_state.total_floor,
                        "phase":phase_str(rm.phase()),"projection_error":e}));
                    return self.raw.choose(rm, actions);
                }
                *self.error.lock().unwrap() = Some(e);
                return None;
            }
        };
        for key in [
            "model_path",
            "model_dir",
            "features",
            "character_id",
            "public_uncertainty",
        ] {
            if let Some(value) = self.request.get(key) {
                projection[key] = value.clone();
            }
        }
        projection["selection_context"] = self.context.clone();
        let public_started = Instant::now();
        let public = crate::ppo::policy_evaluate(&projection);
        record_elapsed(&self.batch, "meta_public_policy", public_started);
        let public_value = match &public {
            Ok(value) => value.clone(),
            Err(e) => json!({"ok":false,"errors":[e]}),
        };
        let mut planned_macro = None;
        let chosen_public = public.as_ref().map_err(Clone::clone).and_then(|value| {
            if value["ok"] == true {
                if let Some(plan) = reward_macro_plan(rm, value, &projection)? {
                    let index = actions
                        .iter()
                        .position(|action| Some(action) == plan.actions.first())
                        .ok_or("alignment_reward_macro_first_edge_not_legal")?;
                    planned_macro = Some(plan);
                    Ok(index)
                } else {
                    map_action(value, &projection, actions)
                }
            } else {
                Err(format!("alignment_adapter_unavailable:{}", value["errors"]))
            }
        });
        let expected = match raw {
            Ok(Some((masked, probs, value, pick))) => {
                json!({"actions":masked.iter().map(emit::run_action::run_action_to_value).collect::<Vec<_>>(),
                "action_descs":masked.iter().filter_map(|a|ActionDesc::try_from(a).ok()).collect::<Vec<_>>(),"probs":probs,"value":value,
                "grouped_argmax":pick,"selected_action":emit::run_action::run_action_to_value(&masked[pick]),
                "obs":build_obs(rm,phase_str(rm.phase()),classify_surface(rm.phase(),&masked))})
            }
            Ok(None) => json!({"status":"upstream_no_network_decision"}),
            Err(e) => json!({"error":e}),
        };
        let mut expected_request = self.request.clone();
        expected_request["obs"] = expected["obs"].clone();
        expected_request["actions"] = expected["action_descs"].clone();
        let expected_diagnostic = if self.batch.is_some() { json!({"skipped":"batch_checks_adapter_availability"}) }
            else { crate::ppo::network_diagnostics(&expected_request)
                .unwrap_or_else(|e| json!({"ok":false,"error":e})) };
        let compared_public = public_value["policy_steps"]
            .as_array()
            .and_then(|steps| steps.iter().find(|step| step["obs"].is_object()))
            .unwrap_or(&public_value);
        let mut public_request = self.request.clone();
        public_request["obs"] = compared_public["obs"].clone();
        public_request["actions"] = compared_public["actions"].clone();
        let public_diagnostic = if self.batch.is_some() { json!({"skipped":"batch_checks_adapter_availability"}) }
            else { crate::ppo::network_diagnostics(&public_request)
                .unwrap_or_else(|e| json!({"ok":false,"error":e})) };
        let mut obs_differences = Vec::new();
        differences(
            &expected["obs"],
            &compared_public["obs"],
            "obs",
            &mut obs_differences,
        );
        let mut tensor_differences = Vec::new();
        differences(
            &expected_diagnostic["encoder"],
            &public_diagnostic["encoder"],
            "encoder",
            &mut tensor_differences,
        );
        record_alignment(&self.trace, &self.batch, json!({"kind":"meta","phase":phase_str(rm.phase()),"floor":rm.run_state.total_floor,
            "projection":projection,"upstream":expected,"production":public_value,"mapped_index":chosen_public.as_ref().ok(),
            "mapping_error":chosen_public.as_ref().err(),
            "reward_macro_plan":planned_macro.as_ref().map(|plan|&plan.report),
            "comparison":{"full_obs_difference_paths":obs_differences,"encoder_difference_paths":tensor_differences,
                "scope":"current_original_root_vs_first_scored_public_continuation_layer",
                "diagnostics_available":expected_diagnostic["ok"]==true&&public_diagnostic["ok"]==true,
                "candidate_order_equal":expected["action_descs"]==compared_public["actions"],
                "logits_equal":expected_diagnostic["raw_logits"]==public_diagnostic["raw_logits"],
                "probs_equal":expected_diagnostic["probs"]==public_diagnostic["probs"],
                "group_pick_equal":expected_diagnostic["grouped_argmax"]==public_diagnostic["grouped_argmax"]},
            "upstream_diagnostic":expected_diagnostic,"public_diagnostic":public_diagnostic}));
        if self.mode == "public" {
            match chosen_public {
                Ok(index) => {
                    self.context = planned_macro
                        .as_ref()
                        .map(|plan| plan.next_context.clone())
                        .unwrap_or_else(|| public_value["next_context"].clone());
                    self.pending_macro = planned_macro;
                    Some(index)
                }
                Err(e) => {
                    *self.error.lock().unwrap() = Some(e);
                    None
                }
            }
        } else {
            let raw_pick = self.raw.choose(rm, actions);
            if raw_pick == chosen_public.ok() {
                self.context = public_value["next_context"].clone();
            } else {
                self.context = Value::Null;
            }
            raw_pick
        }
    }
}

/// Run one seeded policy cell; independent processes provide the hard timeout in Python.
pub fn full_run(request: &Value) -> Result<Value, String> {
    if request["diagnostic"] == "public_fixture" {
        return replay_public_fixture(request);
    }
    if request["diagnostic"] == "combat_networks" {
        sts2_runai::rollout::ensure_content_installed();
        let state = sts2_wire::state_from_wire(&&request["wire_state"])
            .map_err(|e| format!("alignment_wire_state:{e}"))?;
        let mut control = request.clone();
        control["network_diagnostics"] = json!(true);
        let cfg = crate::mcts::config(&control, &state, request["seed"].as_u64().unwrap_or(0))?;
        let setup = crate::networks::prepare(&control, &state, cfg)?;
        return Ok(
            json!({"ok":true,"scope":"explicit_same_wire_simulator_component_diagnostic_not_public_import",
            "network_identity":setup.identity,"canonical_wire":emit::combat::combat_state_to_value(&state).map_err(|e|e.to_string())?}),
        );
    }
    let started = Instant::now();
    let seed = request["seed"]
        .as_i64()
        .and_then(|n| i32::try_from(n).ok())
        .ok_or("alignment_seed_missing")?;
    let class = character(
        request["character"]
            .as_str()
            .or_else(|| request["character_id"].as_str())
            .unwrap_or("ironclad"),
    )?;
    let mode = request["mode"].as_str().unwrap_or("audit");
    if !["raw", "audit", "public"].contains(&mode) {
        return Err("alignment_invalid_mode".into());
    }
    let batch = (request["diagnostic"] == "public_batch")
        .then(|| Arc::new(Mutex::new(BatchDiagnostics::default())));
    if batch.is_some() && mode != "audit" {
        return Err("alignment_public_batch_requires_audit_driver".into());
    }
    let path = request["model_path"]
        .as_str()
        .ok_or("alignment_explicit_model_path_required")?;
    let features = net::features_for(class);
    let raw = PpoPolicy::from_path_with(Path::new(path), features);
    sts2_runai::rollout::ensure_content_installed();
    let mut rm = sts2_content::run_callbacks::new_run_manager_for_character_with_ascension(
        seed,
        class,
        request["ascension"].as_u64().unwrap_or(0) as u8,
    )
    .with_neow_start();
    rm.set_public_history_recording(mode != "raw");
    rm.run_state.interactive_card_removal = true;
    rm.run_state.interactive_card_upgrade = true;
    rm.run_state.interactive_card_transform = true;
    let stage_profile = request["engine_stage_profile"]
        .as_str()
        .unwrap_or(if mode == "public" {
            "native_ui"
        } else {
            "served"
        });
    if !["served", "native_ui"].contains(&stage_profile) {
        return Err("alignment_stage_profile_invalid".into());
    }
    rm.run_state.direct_event_relic_grant = stage_profile == "served";
    rm.run_state.interactive_potion_swap = request["interactive_potion_swap"]
        .as_bool()
        .unwrap_or_else(|| {
            stage_profile == "native_ui" || sts2_runai::policy::resolve_potion_swap_flag_from_env()
        });
    if request["act1"] == "roll" {
        rm = rm.with_rolled_act1();
    } else if request["act1"] == "underdocks" {
        rm = rm.with_act1(sts2_core::act_selection::ActVariant::Underdocks);
    }
    sts2_content::run_callbacks::initialize_run_setup(&mut rm);
    let trace = Arc::new(Mutex::new(Vec::new()));
    let error = Arc::new(Mutex::new(None));
    let meta = Meta {
        raw,
        mode: mode.into(),
        request: request.clone(),
        context: Value::Null,
        trace: trace.clone(),
        error: error.clone(),
        pending_macro: None,
        batch: batch.clone(),
    };
    // Public runs call the actual endpoint, without an extra private-world event veto from the reference wrapper.
    let mut public_meta = (mode == "public").then(|| Meta {
        raw: PpoPolicy::from_path_with(Path::new(path), features),
        mode: mode.into(),
        request: request.clone(),
        context: Value::Null,
        trace: trace.clone(),
        error: error.clone(),
        pending_macro: None,
        batch: None,
    });
    let mut budget = CombatBudget::eval_cheap();
    budget.sims = request["simulations"]
        .as_u64()
        .unwrap_or(u64::from(DEFAULT_SIMS)) as u32;
    budget.boss_sims = request["boss_simulations"]
        .as_u64()
        .unwrap_or(u64::from(DEFAULT_BOSS_SIMS)) as u32;
    let mut policy = MctsCombatPolicy::new(meta, budget, seed);
    let execution_profile = request["execution_profile"].as_str().unwrap_or("corrected");
    if !["pristine", "corrected"].contains(&execution_profile) {
        return Err("alignment_execution_profile_invalid".into());
    }
    if mode == "public" && execution_profile != "corrected" {
        return Err("alignment_public_mode_uses_actual_corrected_production_entrypoint".into());
    }
    let identities = Arc::new(Mutex::new(Vec::new()));
    if execution_profile == "corrected" {
        let controls = request.clone();
        let recorded = identities.clone();
        policy = policy.with_search_transforms(
            move |rm, state, original| {
                // Invalid component requests remain explicit failures; no fallback policy is substituted.
                let setup = crate::networks::transform_upstream(&controls, rm, state, original)
                    .unwrap_or_else(|error| panic!("alignment_search_setup:{error}"));
                recorded.lock().unwrap().push(setup.identity);
                (setup.config, setup.leaf, setup.prior)
            },
            crate::mcts::correct_execution,
        );
    } else if [
        "rollout_policy",
        "action_prior",
        "value_model",
        "search_profile",
    ]
    .iter()
    .any(|key| {
        request.get(*key).is_some_and(|v| {
            v.as_str()
                != Some(if *key == "value_model" {
                    "texel"
                } else {
                    "upstream"
                })
        })
    }) {
        return Err("alignment_pristine_profile_cannot_apply_component_controls".into());
    }
    let max_steps = request["max_steps"]
        .as_u64()
        .unwrap_or(DEFAULT_MAX_STEPS as u64) as usize;
    let stopping = sts2_core::full_run::FullRunConfig {
        max_steps,
        livelock_stall_limit: request["livelock_stall_limit"]
            .as_u64()
            .unwrap_or(sts2_core::full_run::DEFAULT_LIVELOCK_STALL_LIMIT as u64)
            as usize,
        invincible: false,
        record_trace: false,
    };
    let deadline =
        started + Duration::from_millis(request["timeout_ms"].as_u64().unwrap_or(600_000));
    let mut steps = Vec::new();
    let mut meta_sources = std::collections::BTreeMap::<String, u64>::new();
    let mut known_card_ids = BTreeSet::<String>::new();
    let mut witnessed_room = None;
    let mut internal_roots = Vec::<Value>::new();
    let mut captured_room = None;
    let mut production_counter = 0u64;
    let mut production_simulations = 0u64;
    let mut production_rollouts = 0u64;
    let mut production_depth_cuts = 0u64;
    let mut production_belief_cuts = 0u64;
    let mut production_belief_reasons = BTreeMap::<String, u64>::new();
    let mut production_elite = 0u64;
    let mut production_boss = 0u64;
    let mut pending_macro = VecDeque::<(RunAction, usize)>::new();
    let mut macros = Vec::<Value>::new();
    let mut native_commands_selected = 0u64;
    let outcome: String;
    let mut upstream_steps = 0usize;
    let mut last_hash = sts2_core::hashing::run_hash(&rm);
    let mut stall = 0usize;
    'run_loop: loop {
        if rm.is_over() {
            outcome = if rm.run_state.player_won {
                "won"
            } else {
                "lost"
            }
            .into();
            break;
        }
        if upstream_steps >= stopping.max_steps {
            outcome = "max_steps".into();
            break;
        }
        if Instant::now() >= deadline {
            outcome = "timeout".into();
            break;
        }
        let step = upstream_steps;
        if let Some(batch) = &batch { batch.lock().unwrap().step = step; }
        rm.bind_pending_event();
        policy.prepare(&mut rm);
        let actions = rm.get_available_actions();
        if actions.is_empty() {
            outcome = "empty_actions".into();
            break;
        }
        if request["capture_internal_roots"] == true && rm.phase() == Phase::Combat {
            let room = (rm.run_state.act_index, rm.run_state.total_floor);
            if captured_room != Some(room) {
                let state = rm
                    .combat
                    .as_ref()
                    .ok_or("alignment_capture_combat_missing")?;
                internal_roots.push(json!({"floor":room.1,"act_index":room.0,"round":state.round,
                    "room_type":rm.run_state.current_room.as_ref().map(|room|format!("{:?}",room.room_type)),
                    "scope":"authorized_simulator_internal_wire_diagnostic_never_fed_to_public_adapter",
                    "limitations":["wire_codec_drops_some_private_AI_history_and_scratch; not_a_full_CombatState_checkpoint"],
                    "wire_state":emit::combat::combat_state_to_value(state).map_err(|e|e.to_string())?}));
                captured_room = Some(room);
            }
        }
        let mut macro_index = None;
        let mut native_command_start = native_commands_selected;
        let continuing_macro = pending_macro.pop_front();
        let decision_started = Instant::now();
        let chosen = if let Some((wanted, index)) = continuing_macro {
            macro_index = Some(index);
            native_command_start = macros[index]["native_command_start"].as_u64().unwrap();
            match actions.iter().position(|action| *action == wanted) {
                Some(index) => Some(index),
                None => {
                    pending_macro.push_front((wanted, index));
                    *error.lock().unwrap() =
                        Some(format!("alignment_reward_macro_edge_not_legal:{wanted:?}"));
                    None
                }
            }
        } else if mode != "raw" && rm.phase() == Phase::Combat {
          'combat_check: {
            let mut projection = match project_run(&rm, &actions) {
                Ok(projection) => projection,
                Err(reason) if batch.is_some() => {
                    record_alignment(&trace, &batch, json!({"kind":"combat_import","floor":rm.run_state.total_floor,
                        "projection_error":reason}));
                    break 'combat_check policy.choose(&rm, &actions);
                }
                Err(reason) => return Err(reason),
            };
            // This actor's completed control-command ordinal mirrors the native receipt revision.
            projection["public_state"]["action_revision"] = json!(if mode == "public" {
                native_commands_selected
            } else {
                upstream_steps as u64
            });
            let room = (rm.run_state.act_index, rm.run_state.total_floor);
            if witnessed_room != Some(room) {
                known_card_ids.clear();
                witnessed_room = Some(room);
            }
            remember_visible_cards(&projection["public_state"], &mut known_card_ids);
            if let Err(reason) = attach_known_pile_ids(&rm, &mut projection["public_state"], &known_card_ids) {
                if batch.is_none() { return Err(reason); }
                record_alignment(&trace, &batch, json!({"kind":"combat_import","floor":rm.run_state.total_floor,
                    "projection":projection,"projection_error":reason}));
                break 'combat_check policy.choose(&rm, &actions);
            }
            let import_started = Instant::now();
            let root = PublicPosition::from_core(
                rm.combat.as_ref().ok_or("alignment_actual_combat_missing")?.clone(),
                &projection["public_state"], false,
            );
            record_elapsed(&batch, "mirror_clone", import_started);
            let mut record = json!({"kind":"combat_import","floor":rm.run_state.total_floor,"projection":projection,
                "state_source":"complete_upstream_run_manager_clone",
                "search_information":"public_search_blinds_future_rng_and_draw_order_before_setup",
                "actual_legal_actions":actions.iter().map(emit::run_action::run_action_to_value).collect::<Vec<_>>()});
            record["actual_native_legal_actions"] = json!(actions
                .iter()
                .map(|action| witnessed_action(&rm, *action))
                .collect::<Vec<_>>());
            record["actual_enemy_slot_map"] = json!(public_enemy_roster(
                rm.combat.as_ref().ok_or("alignment_actual_combat_missing")?
            )?
            .iter()
            .enumerate()
            .map(|(public_index, (core_index, _))| json!({"core_index":core_index,"public_index":public_index}))
            .collect::<Vec<_>>());
            let selected = {
                record["support"] = root.search_support();
                record["imported_snapshot"] = root.snapshot();
                if mode == "public"
                    && record["support"]["blocking_reasons"]
                        .as_array()
                        .is_some_and(|rows| !rows.is_empty())
                {
                    *error.lock().unwrap() = Some("alignment_public_combat_blocked".into());
                    trace.lock().unwrap().push(record);
                    outcome = "policy_halt".into();
                    break 'run_loop;
                }
                let mut adapted = rm.clone();
                adapted.combat = Some(root.state.clone());
                let imported_actions = adapted.get_available_actions();
                record["imported_legal_actions"] = json!(imported_actions
                    .iter()
                    .map(emit::run_action::run_action_to_value)
                    .collect::<Vec<_>>());
                record["imported_native_legal_actions"] = json!(imported_actions
                    .iter()
                    .map(|action| witnessed_action(&adapted, *action))
                    .collect::<Vec<_>>());
                if mode == "public" {
                    let mut search = request.clone();
                    search["op"] = json!("search");
                    search["public_state"] = projection["public_state"].clone();
                    search["combat_decision_mode"] = json!("upstream");
                    search["read_access"] = json!("public");
                    search.as_object_mut().unwrap().remove("scenario_seed");
                    search["decision_counter"] = json!(production_counter);
                    search["benchmark_run_seed"] = json!(seed);
                    search["decision_counter_source"] =
                        json!("offline_public_loop_wrapper_counter");
                    search["timeout_ms"] = json!(deadline
                        .saturating_duration_since(Instant::now())
                        .as_millis()
                        .clamp(1, 600_000)
                        as u64);
                    if rm.run_state.current_room.as_ref().is_some_and(|room| {
                        room.room_type == sts2_core::runstate::RoomType::Boss
                    }) {
                        search["simulations"] = json!(request["boss_simulations"]
                            .as_u64()
                            .unwrap_or(u64::from(DEFAULT_BOSS_SIMS)));
                        production_boss += 1;
                    } else if rm.run_state.current_room.as_ref().is_some_and(|room| {
                        room.room_type == sts2_core::runstate::RoomType::Elite
                    }) {
                        production_elite += 1;
                    }
                    let response = crate::mcts::search_position(&search, root.clone())
                        .unwrap_or_else(|reason| json!({"ok":false,"errors":[reason]}));
                    production_counter += 1;
                    production_simulations +=
                        response["work"]["simulations"].as_u64().unwrap_or(0);
                    production_rollouts +=
                        response["mcts"]["rollouts_total"].as_u64().unwrap_or(0);
                    production_depth_cuts +=
                        response["mcts"]["depth_cap_hits"].as_u64().unwrap_or(0);
                    production_belief_cuts += response["mcts"]["belief_unsupported_cuts"]
                        .as_u64()
                        .unwrap_or(0);
                    for (reason, count) in response["mcts"]["belief_cut_reasons"]
                        .as_object()
                        .into_iter()
                        .flatten()
                    {
                        *production_belief_reasons.entry(reason.clone()).or_default() +=
                            count.as_u64().unwrap_or(0);
                    }
                    if response["network_identity"].is_object() {
                        identities
                            .lock()
                            .unwrap()
                            .push(response["network_identity"].clone());
                    }
                    let action = response["best"]["actions"][0].clone();
                    record["selected_public_action"] = action.clone();
                    record["production_search"] = response;
                    if record["production_search"]["ok"] == true
                        && !record["production_search"]["decision_identity"].is_object()
                    {
                        *error.lock().unwrap() =
                            Some("alignment_production_decision_identity_missing".into());
                        None
                    } else if record["production_search"]["ok"] != true {
                        *error.lock().unwrap() =
                            Some("alignment_production_search_unavailable".into());
                        None
                    } else {
                        match map_combat_action(
                            &action,
                            &record["projection"]["public_state"],
                            &root,
                            &rm,
                            &actions,
                        ) {
                            Ok(index) => Some(index),
                            Err(reason) => {
                                *error.lock().unwrap() = Some(reason);
                                None
                            }
                        }
                    }
                } else {
                    policy.choose(&rm, &actions)
                }
            };
            if mode == "public" && selected.is_none() && error.lock().unwrap().is_none() {
                *error.lock().unwrap() =
                    Some("alignment_imported_combat_action_not_mappable".into());
            }
            record_alignment(&trace, &batch, record);
            selected
          }
        } else if let Some(meta) = public_meta.as_mut() {
            meta.choose(&rm, &actions)
        } else {
            policy.choose(&rm, &actions)
        };
        record_elapsed(&batch, "decision_total", decision_started);
        let Some(index) = chosen else {
            outcome = "policy_halt".into();
            break;
        };
        if index >= actions.len() {
            outcome = "illegal_policy_index".into();
            break;
        }
        let action = actions[index];
        if mode == "public" && continuing_macro.is_none() {
            if let Some(plan) = public_meta
                .as_mut()
                .and_then(|meta| meta.pending_macro.take())
            {
                let index = macros.len();
                macro_index = Some(index);
                let mut report = plan.report;
                report["planned_engine_edges"] = json!(plan
                    .actions
                    .iter()
                    .map(|action| format!("{action:?}"))
                    .collect::<Vec<_>>());
                report["executed_engine_edges"] = json!([]);
                report["status"] = json!("pending");
                report["native_command_start"] = json!(native_commands_selected);
                let commands = report["native_commands"].as_array().map_or(1, Vec::len) as u64;
                native_commands_selected += commands;
                report["native_command_count"] = json!(commands);
                pending_macro.extend(
                    plan.actions
                        .into_iter()
                        .skip(1)
                        .map(|action| (action, index)),
                );
                macros.push(report);
            } else {
                native_commands_selected += 1;
            }
        }
        let meta_telemetry=policy.take_meta_eval().map(|evaluation|{
            *meta_sources.entry(evaluation.source.to_owned()).or_default()+=1;
            json!({"source":evaluation.source,"actions":evaluation.actions.iter().map(emit::run_action::run_action_to_value).collect::<Vec<_>>(),
                "probs":evaluation.probs,"value":evaluation.value,"grouped_argmax":evaluation.argmax})
        });
        let search_telemetry = if rm.phase() == Phase::Combat && batch.is_none() {
            policy.combat_search().map(|search|json!({
            "chosen":format!("{:?}",search.chosen),"outcomes":search.outcomes.iter().map(|row|json!({
                "action":format!("{:?}",row.action),"visits":row.visits,"wins":row.wins,"losses":row.losses,"ties":row.ties,
                "value_sum":row.value_sum,"mean_value":row.mean_value,"hp_n":row.hp_n,"hp_sum":row.hp_sum})).collect::<Vec<_>>()
        }))
        } else {
            None
        };
        let before = json!({"phase":phase_str(rm.phase()),"floor":rm.run_state.total_floor,"hp":rm.run_state.current_hp,
            "legal_actions":actions.iter().map(emit::run_action::run_action_to_value).collect::<Vec<_>>()});
        let action_started = Instant::now();
        let result = rm.take_action(action);
        record_elapsed(&batch, "action_apply", action_started);
        if rm.phase() != Phase::Combat {
            // A later encounter must not inherit previously observed card identities.
            witnessed_room = None;
            known_card_ids.clear();
        }
        steps.push(json!({"step":step,"before":before,"action":emit::run_action::run_action_to_value(&action),"result":format!("{result:?}"),
            "meta_telemetry":meta_telemetry,"search_telemetry":search_telemetry,"reward_macro_index":macro_index,
            "native_command_start":if mode=="public"{Some(native_command_start)}else{None},"macro_continuation":continuing_macro.is_some()}));
        if let Some(index) = macro_index {
            macros[index]["executed_engine_edges"].as_array_mut().unwrap().push(json!({"step":step,"action":format!("{action:?}"),
                "result":format!("{result:?}"),"phase":phase_str(rm.phase()),"hp":rm.run_state.current_hp,"gold":rm.run_state.gold,
                "owned_relics":rm.run_state.relics.iter().map(|id|id.to_python_name()).collect::<Vec<_>>(),
                "remaining_relic_queue":rm.run_state.relic_rewards.iter().map(|reward|reward.relic_id.to_python_name()).collect::<Vec<_>>(),
                "potions":rm.run_state.potions.iter().map(|potion|potion.python_name()).collect::<Vec<_>>(),
                "pending_potion_swaps":rm.run_state.pending_potion_swaps.iter().map(|potion|potion.python_name()).collect::<Vec<_>>()}));
            if result != ActionResult::Advanced && result != ActionResult::RunOver {
                macros[index]["status"] = json!("engine_macro_edge_rejected");
            } else if !pending_macro.iter().any(|(_, pending)| *pending == index) {
                macros[index]["status"] = json!("engine_macro_complete");
            }
        }
        match result {
            ActionResult::Advanced => {
                upstream_steps += 1;
            }
            ActionResult::RunOver => {
                outcome = if rm.run_state.player_won {
                    "won"
                } else {
                    "lost"
                }
                .into();
                break;
            }
            other => {
                outcome = format!("engine_action_not_advanced:{other:?}");
                break;
            }
        }
        // Match drive_full_run exactly: hash after Advanced, and never count the RunOver action.
        if stopping.livelock_stall_limit > 0 {
            let hash = sts2_core::hashing::run_hash(&rm);
            if hash == last_hash {
                stall += 1;
                if stall >= stopping.livelock_stall_limit {
                    outcome = "livelock".into();
                    break;
                }
            } else {
                stall = 0;
                last_hash = hash;
            }
        }
    }
    let (elite, boss, motif, simulations) = policy.combat_budget_spent();
    let ordinary = policy.combat_decisions().saturating_sub(elite + boss);
    let errors = error.lock().unwrap().clone();
    let combat_report = if mode == "public" {
        json!({"ordinary_decisions":production_counter.saturating_sub(production_elite+production_boss),
        "elite_decisions":production_elite,"boss_decisions":production_boss,"motif_decisions":0,
        "simulations_spent":production_simulations,"actual_rollouts":production_rollouts,"depth_cap_hits":production_depth_cuts,
        "belief_unsupported_cuts":production_belief_cuts,"belief_cut_reasons":production_belief_reasons,
        "decision_entrypoint":"production_mcts_search","fallbacks":"see_each_production_search_response; unavailable_roots_halt",
        "event_vetoes":"production_public_guard_only_no_extra_private_wrapper_veto"})
    } else {
        json!({"ordinary_decisions":ordinary,"elite_decisions":elite,"boss_decisions":boss,"motif_decisions":motif,"simulations_spent":simulations,
            "fallbacks":format!("{:?}",policy.fallbacks()),"event_vetoes":format!("{:?}",policy.event_vetoes())})
    };
    let source_hashes = json!({"core":sts2_core::SRC_HASH,"content":sts2_content::SRC_HASH,"runai":sts2_runai::SRC_HASH});
    Ok(
        json!({"ok":errors.is_none(),"scope":"offline_run_wrapper_and_public_adapter_alignment_not_native_win_rate",
        "mode":mode,"execution_profile":execution_profile,"source_variant":"patched_vendor_rules",
        "engine_stage_profile":stage_profile,"stage_flags":{"direct_event_relic_grant":rm.run_state.direct_event_relic_grant,
            "interactive_potion_swap":rm.run_state.interactive_potion_swap},
        "decision_entrypoints":if mode=="public"{json!({"combat":"production_mcts_search","noncombat":"production_policy_evaluate",
            "extra_private_wrapper_event_veto":false,"benchmark_seed_control":"known_offline_run_seed_plus_actual_decision_counter"})}
            else{json!({"combat":"original_full_run_wrapper","noncombat":"original_wrapper_with_optional_adapter_audit"})},
        "wrapper_construction":if execution_profile=="pristine"{"original_no_embedding_hooks"}else{"explicit_production_corrections_and_components"},
        "search_component_identities":identities.lock().unwrap().clone(),"meta_policy_sources":meta_sources,
        "outcome":outcome,"won":rm.run_state.player_won,"seed":seed,"character":format!("{class:?}"),
        "upstream_steps":upstream_steps,"action_trace_count":steps.len(),
        "stopping":{"source":"sts2_core::full_run::drive_full_run config_and_run_hash_guard",
            "max_steps":stopping.max_steps,"livelock_stall_limit":stopping.livelock_stall_limit,
            "invincible":false,"record_trace":false,"run_hash_stall_count":stall,
            "step_count_semantics":"Advanced increments; terminal RunOver action is retained in trace but not report.steps"},
        "ascension":request["ascension"],"final_hp":alignment_current_hp(&rm),"act_index":rm.run_state.act_index,
        "steps":steps,"decisions":trace.lock().unwrap().clone(),"adapter_error":errors,"source_hashes":source_hashes,
        "internal_root_diagnostics":internal_roots,
        "batch_diagnostics":batch.as_ref().map(|batch|batch.lock().unwrap().report()),
        "native_commands_selected":if mode=="public"{Some(native_commands_selected)}else{None},
        "engine_internal_edges":steps.len(),"reward_macros":macros,
        "pending_macro_edges":pending_macro.iter().map(|(action,index)|json!({"macro_index":index,"action":format!("{action:?}")})).collect::<Vec<_>>(),
        "combat":combat_report,
        "production_combat":{"decisions":production_counter,"simulations_completed":production_simulations,
            "actual_rollouts":production_rollouts,"depth_cap_hits":production_depth_cuts,
            "belief_unsupported_cuts":production_belief_cuts,"belief_cut_reasons":production_belief_reasons},
        "elapsed_ms":started.elapsed().as_secs_f64()*1000.0}),
    )
}

/// Active combat HP is authoritative until settlement copies it back to the run.
fn alignment_current_hp(rm: &RunManager) -> i32 {
    if rm.phase() == Phase::Combat {
        rm.combat.as_ref().map_or(rm.run_state.current_hp,|combat|combat.player.current_hp)
    } else { rm.run_state.current_hp }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Explicit meta fixtures retain the PPO failure even when their public state contains combat.
    #[test]
    fn public_fixture_explicit_meta_kind_overrides_combat_shape() {
        let projection = json!({"public_state":{"screen":"COMBAT","combat":{},
            "run":{"character_id":"IRONCLAD","act_id":"0","floor":1,"ascension":0,
                "hp":"70/80","gold":0,"deck":[],"relic_ids":[],"potions":[]}}});
        let meta = replay_public_fixture(&json!({"kind":"meta","projection":projection})).unwrap();
        assert_eq!(meta["ok"], false);
        assert_eq!(meta["errors"], json!(["ppo_scene_not_supported:COMBAT"]));
        assert_eq!(meta["scope"], "public_meta_availability_replay_without_private_driver_action_mapping");

        assert_eq!(replay_public_fixture(&json!({"kind":"combat_import","projection":projection}))
            .unwrap_err(), "alignment_fixture_complete_wire_required");
        let wire = emit::combat::combat_state_to_value(&sts2_core::CombatState::default()).unwrap();
        let explicit = replay_public_fixture(&json!({"kind":"combat_import","projection":projection,"wire_state":wire})).unwrap();
        let legacy = replay_public_fixture(&json!({"projection":projection,"wire_state":wire})).unwrap();
        assert_eq!(explicit, legacy);
        assert_eq!(legacy["scope"], "complete_wire_support_replay_not_full_search_or_run");
    }

    /// A first error must not hide later checks or overwrite the encounter's original fixture.
    #[test]
    fn batch_diagnostics_retains_independent_failures_and_first_fixtures() {
        let mut batch = BatchDiagnostics::default();
        batch.step = 2;
        batch.observe(&json!({"kind":"combat_import","floor":3,"import_errors":["a","a"],
            "projection":{"public_state":{"marker":"original"}}}));
        batch.step = 3;
        batch.observe(&json!({"kind":"combat_import","floor":3,"import_errors":["a"],
            "projection":{"public_state":{"marker":"later"}}}));
        batch.step = 4;
        batch.observe(&json!({"kind":"combat_import","floor":3,"support":{"blocking_reasons":[]}}));
        batch.step = 5;
        batch.observe(&json!({"kind":"combat_import","floor":4,"import_errors":["b"]}));
        let report = batch.report();
        assert_eq!(report["checks"], 4);
        assert_eq!(report["failed_checks"], 3);
        assert_eq!(report["checks_after_first_failure"], 3);
        assert_eq!(report["first_failure_step"], 2);
        assert_eq!(report["groups"].as_array().unwrap().len(), 2);
        let original = report["groups"].as_array().unwrap().iter()
            .find(|row| row["signature"] == "a").unwrap();
        assert_eq!(original["count"], 2);
        assert_eq!(original["first_record"]["projection"]["public_state"]["marker"], "original");
    }

    /// A halted combat reports live HP; a retained combat outside Combat cannot override settled run HP.
    #[test]
    fn final_hp_uses_active_combat_only() {
        let mut rm = RunManager::new(719, CharacterId::Defect);
        rm.run_state.current_hp = 68;
        let mut combat = sts2_core::CombatState::default();
        combat.player.current_hp = 64;
        rm.combat = Some(combat.clone());
        assert_eq!(alignment_current_hp(&rm),68);
        rm.debug_enter_combat(combat,sts2_core::runstate::RoomType::Monster);
        assert_eq!(alignment_current_hp(&rm),64);
    }

    /// Match the actual emitter name and expose only the reviewed public facing field.
    #[test]
    fn surrounded_facing_uses_actual_emitter_name() {
        sts2_runai::rollout::ensure_content_installed();
        let mut combat = sts2_core::CombatState::default();
        combat.player.current_hp = 70;
        combat.player.max_hp = 70;
        // Rocket's opponent-targeted spawn hook requires the normal player creature first.
        combat.init_player_creature();
        combat.spawn_enemy(sts2_core::MonsterKind::Rocket);
        let uid = combat
            .player_power_uids()
            .into_iter()
            .find(|uid| {
                combat.powers.iter().any(|power| {
                    power.id == uid.0 && power.kind == sts2_core::PowerKind::Surrounded
                })
            })
            .unwrap();
        for (counter, expected) in [
            (sts2_core::hooks::SURROUNDED_FACING_LEFT, json!("Left")),
            (sts2_core::hooks::SURROUNDED_FACING_RIGHT, json!("Right")),
            (99, Value::Null),
        ] {
            combat
                .powers
                .iter_mut()
                .find(|power| power.id == uid.0)
                .unwrap()
                .counter = counter;
            let emitted =
                emit::combat::powers_to_value(&[uid], &combat.powers, "test.facing").unwrap();
            assert_eq!(emitted[0]["name"], "SURROUNDED");
            let projected = public_powers(&combat, &[uid]).unwrap();
            assert_eq!(projected[0]["facing"], expected);
            assert!(projected[0].get("counter").is_none());
        }
    }

    /// Match native ordinary removal and retained Reattach corpses without reusing stale target slots.
    #[test]
    fn public_enemy_roster_retains_reattach_and_reindexes_ordinary_deaths() {
        sts2_runai::rollout::ensure_content_installed();
        let mut combat = sts2_core::CombatState::default();
        let ordinary = combat.spawn_enemy(sts2_core::MonsterKind::Nibbit);
        let reviving = combat.spawn_enemy(sts2_core::MonsterKind::DecimillipedeSegmentFront);
        let live = combat.spawn_enemy(sts2_core::MonsterKind::Nibbit);
        for uid in [ordinary, reviving] {
            combat
                .creatures
                .iter_mut()
                .find(|enemy| enemy.id == uid.0)
                .unwrap()
                .current_hp = 0;
        }
        assert_eq!(
            public_enemy_roster(&combat).unwrap(),
            vec![(1, reviving), (2, live)]
        );
        let mut rm = RunManager::new(719, CharacterId::Ironclad);
        rm.combat = Some(combat);
        assert_eq!(witnessed_enemy_target(&rm, Some(2)).unwrap(), Some(1));
        assert!(witnessed_enemy_target(&rm, Some(0)).is_err());
        let mut source = json!({"run":{}});
        project_combat(&rm, &mut source).unwrap();
        assert_eq!(source["combat"]["enemies"][0]["i"], 0);
        assert_eq!(source["combat"]["enemies"][0]["alive"], false);
        assert_eq!(source["combat"]["enemies"][1]["i"], 1);
        assert_eq!(source["combat"]["enemies"][1]["alive"], true);
    }

    /// Project Ghost Seed's real keyword modifier and preserve immutable play-time history.
    #[test]
    fn ghost_seed_keywords_match_original_end_turn_and_finished_history() {
        sts2_runai::rollout::ensure_content_installed();
        let run = sts2_core::runstate::RunState {
            character_id: CharacterId::Regent,
            current_hp: 70,
            max_hp: 70,
            relics: vec![sts2_core::RelicId::GhostSeed],
            master_deck: vec![
                card("DEFEND_REGENT", false),
                card("DEFEND_REGENT", false),
                card("BASH", false),
            ],
            record_public_combat_history: true,
            ..Default::default()
        };
        // Entering combat dispatches the original Ghost Seed listener before the opening draw.
        let mut combat = sts2_content::run_bridge::enter_combat_from_roster(
            &run,
            &[(
                sts2_core::MonsterKind::Nibbit,
                sts2_content::monster_dispatch::MonsterSpawnArgs::None,
                9999,
            )],
            sts2_core::runstate::RoomType::Monster,
        );
        let defends: Vec<_> = combat.cards.iter()
            .filter(|card| card.card_id == sts2_core::cards::CardId::DefendRegent)
            .map(|card| card.uid).collect();
        assert_eq!(defends.len(), 2);
        for uid in &defends {
            let instance = combat.cards.iter().find(|card| card.uid == *uid).unwrap();
            assert_eq!(instance.combat_var(sts2_core::cards::VarKey::EtherealThisCombat), 1);
            assert!(!sts2_core::card_effects::card_ethereal_inst(instance));
            assert_eq!(combat_card(&combat, *uid, 0).unwrap()["effective_keywords"], json!(["Ethereal"]));
        }
        let bash = combat.cards.iter()
            .find(|card| card.card_id == sts2_core::cards::CardId::Bash).unwrap().uid;
        assert_eq!(combat_card(&combat, bash, 0).unwrap()["effective_keywords"], json!([]));
        let mut ordinary = combat.clone();
        for uid in &defends {
            sts2_core::card_effects::set_card_var(&mut ordinary, *uid,
                sts2_core::cards::VarKey::EtherealThisCombat, 0);
        }
        assert_eq!(combat_card(&ordinary, defends[0], 0).unwrap()["effective_keywords"], json!([]));
        let _ = sts2_core::apply(&mut ordinary, sts2_core::Action::EndTurn);
        assert!(defends.iter().all(|uid| !ordinary.player.exhaust.contains(uid)));
        let hand_index = combat.player.hand.iter().position(|uid| *uid == defends[0]).unwrap();
        let _ = sts2_core::apply(&mut combat, sts2_core::Action::PlayCard {
            hand_index: hand_index as u8, target: None,
        });
        // Later card changes must not rewrite the completion's original Ethereal observation.
        sts2_core::card_effects::set_card_var(&mut combat, defends[0],
            sts2_core::cards::VarKey::EtherealThisCombat, 0);
        let mut source = json!({"combat":{}});
        attach_public_combat_history(&combat, &mut source, &BTreeSet::new());
        assert_eq!(source["combat"]["public_card_play_history"]["plays"][0]["was_ethereal"], true);
        let _ = sts2_core::apply(&mut combat, sts2_core::Action::EndTurn);
        assert!(combat.player.exhaust.contains(&defends[1]));
        assert!(!combat.player.exhaust.contains(&bash));
        let exhausted = knowledge_card_rows(&combat, &[defends[1]], &BTreeSet::new()).unwrap();
        assert_eq!(exhausted[0]["effective_keywords"], json!(["Ethereal"]));
        assert_eq!(exhausted[0]["known_instance_ids"], json!([]));
    }

    /// Serialize real dispatched completions and block joins, keeping never-observed IDs null.
    #[test]
    fn public_history_serializer_uses_actual_finished_events_and_seen_identities() {
        sts2_runai::rollout::ensure_content_installed();
        let mut run = sts2_core::runstate::RunState {
            character_id: CharacterId::Ironclad,
            current_hp: 80,
            max_hp: 80,
            record_public_combat_history: true,
            ..Default::default()
        };
        sts2_content::run_callbacks::populate_starter_deck(&mut run, CharacterId::Ironclad);
        let mut combat = sts2_content::run_bridge::enter_combat_from_roster(
            &run,
            &[(
                sts2_core::MonsterKind::Nibbit,
                sts2_content::monster_dispatch::MonsterSpawnArgs::None,
                9999,
            )],
            sts2_core::runstate::RoomType::Monster,
        );
        combat.player.hand.clear();
        combat.player.draw.clear();
        combat.player.discard.clear();
        let mut instance = card("DEFEND_IRONCLAD", false);
        instance.uid = sts2_core::CardUid::new(combat.cards.len() as u32 + 1).unwrap();
        let identity = format!("visible-{}", instance.uid.get());
        combat.player.hand.push(instance.uid);
        combat.cards.push(instance);
        let _ = sts2_core::apply(
            &mut combat,
            sts2_core::Action::PlayCard {
                hand_index: 0,
                target: None,
            },
        );
        // Finished resources are immutable even if the same card instance changes later.
        combat
            .cards
            .iter_mut()
            .find(|card| format!("visible-{}", card.uid.get()) == identity)
            .unwrap()
            .energy_spent = 99;
        let mut source = json!({"combat":{}});
        attach_public_combat_history(&combat, &mut source, &BTreeSet::new());
        let first = &source["combat"]["public_card_play_history"]["plays"][0];
        assert_eq!(first["was_ethereal"], false);
        assert_eq!(first["energy_value"], 1);
        assert_eq!(first["energy_spent"], 1);
        assert_eq!(first["stars_spent"], 0);
        attach_public_combat_statistics(&combat, &mut source);
        assert_eq!(source["combat"]["energy_spent_this_turn"], 1);
        assert_eq!(
            source["combat"]["unspent_energy_at_turn_end_sum"],
            source["combat"]["energy_wasted_this_combat"]
        );
        assert_eq!(
            source["combat"]["public_card_play_history"]["complete"],
            true
        );
        assert_eq!(
            source["combat"]["public_card_play_history"]["plays"][0]["known_instance_id"],
            Value::Null
        );
        attach_public_combat_history(&combat, &mut source, &BTreeSet::from([identity.clone()]));
        assert_eq!(
            source["combat"]["public_card_play_history"]["plays"][0]["known_instance_id"],
            identity
        );
        assert_eq!(
            source["combat"]["public_card_play_history"]["plays"][0]["card_type"],
            "Skill"
        );
        assert_eq!(
            source["combat"]["public_card_play_history"]["plays"][0]["first_in_series"],
            true
        );
        assert_eq!(
            source["combat"]["public_card_block_history"]["complete"],
            true
        );
        assert_eq!(
            source["combat"]["public_card_block_history"]["entries"],
            json!([{"card_play_index":0,"amount":5,"this_turn":true}])
        );
    }

    /// Keep anonymous Fetch copies distinct using only completed public references from this turn.
    #[test]
    fn fetch_projection_uses_finished_public_instances_without_assigning_hidden_ids() {
        sts2_runai::rollout::ensure_content_installed();
        let run = sts2_core::runstate::RunState {
            character_id: CharacterId::Necrobinder,
            current_hp: 70,
            max_hp: 70,
            record_public_combat_history: true,
            ..Default::default()
        };
        let mut combat = sts2_content::run_bridge::enter_combat_from_roster(
            &run,
            &[(
                sts2_core::MonsterKind::Nibbit,
                sts2_content::monster_dispatch::MonsterSpawnArgs::None,
                9999,
            )],
            sts2_core::runstate::RoomType::Monster,
        );
        combat.player.hand.clear();
        combat.player.draw.clear();
        combat.player.discard.clear();
        let mut copies = Vec::new();
        for _ in 0..2 {
            let mut instance = card("FETCH", false);
            instance.uid = sts2_core::CardUid::new(combat.cards.len() as u32 + 1).unwrap();
            copies.push(instance.uid);
            combat.player.hand.push(instance.uid);
            combat.cards.push(instance);
        }
        assert_eq!(
            combat_card(&combat, copies[0], 0).unwrap()["fetch_played_this_turn"],
            false
        );
        let _ = sts2_core::apply(
            &mut combat,
            sts2_core::Action::PlayCard {
                hand_index: 0,
                target: Some(0),
            },
        );
        assert_eq!(
            combat.combat_log.event_log.public_history().unwrap().plays.len(),
            1
        );
        // A scratch value cannot make the never-completed second copy publicly played.
        combat.combat_log.fetch_played_this_turn.push(copies[1]);
        let rows = knowledge_card_rows(&combat, &copies, &BTreeSet::new()).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows.iter().filter(|row| row["fetch_played_this_turn"] == true).count(),
            1
        );
        assert_eq!(
            rows.iter().filter(|row| row["fetch_played_this_turn"] == false).count(),
            1
        );
        assert!(rows.iter().all(|row| row["known_instance_ids"] == json!([])
            && row.get("instance_id").is_none()));
        let mut source = json!({"combat":{}});
        attach_public_combat_history(&combat, &mut source, &BTreeSet::new());
        assert!(
            source["combat"]["public_card_play_history"]["plays"][0]["known_instance_id"].is_null()
        );
        combat.round += 1;
        assert_eq!(
            combat_card(&combat, copies[0], 0).unwrap()["fetch_played_this_turn"],
            false
        );
        combat.combat_log.event_log.set_public_history_recording(false);
        assert!(combat_card(&combat, copies[0], 0).unwrap()["fetch_played_this_turn"].is_null());
        combat.combat_log.event_log.set_public_history_recording(true);
        assert!(combat_card(&combat, copies[0], 0).unwrap()["fetch_played_this_turn"].is_null());
    }

    /// A late or absent public observer cannot turn private counters into fabricated known observations.
    #[test]
    fn public_combat_statistics_without_birth_coverage_remain_unknown() {
        let mut combat = sts2_core::CombatState::default();
        combat.combat_log.energy_spent_this_turn = 9;
        combat.energy_wasted = 12;
        let mut source = json!({"combat":{}});
        attach_public_combat_statistics(&combat, &mut source);
        let statistics = source["combat"].as_object().unwrap();
        assert_eq!(statistics.len(), 17);
        assert!(statistics.values().all(Value::is_null));
    }

    /// Knowledge bands preserve multiplicity while hiding member order and never-seen identities.
    #[test]
    fn knowledge_band_groups_faces_without_exporting_hidden_identities() {
        sts2_runai::rollout::ensure_content_installed();
        let mut combat = sts2_core::CombatState::default();
        let mut members = Vec::new();
        for index in 0..2 {
            let mut instance = card("STRIKE_IRONCLAD", false);
            instance.uid = sts2_core::CardUid::new(index + 1).unwrap();
            members.push(instance.uid);
            combat.player.draw.push(instance.uid);
            combat.cards.push(instance);
        }
        let known = BTreeSet::from(["visible-1".to_owned()]);
        let rows = knowledge_card_rows(&combat, &members, &known).unwrap();
        members.reverse();
        assert_eq!(
            rows,
            knowledge_card_rows(&combat, &members, &known).unwrap()
        );
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["card_count"], 2);
        assert_eq!(rows[0]["known_instance_ids"], json!(["visible-1"]));
        assert!(rows[0].get("instance_id").is_none());
        assert!(rows[0].get("i").is_none());

        combat.cards[0].set_enchantment(sts2_core::cards::EnchantmentId::PerfectFit, 1);
        sts2_core::draw_knowledge::set_root_knowledge(
            &mut combat.player,
            vec![vec![sts2_core::CardUid::new(1).unwrap()]],
            vec![],
            vec![],
        )
        .unwrap();
        let mut source = json!({"combat":{}});
        attach_draw_knowledge(&combat, &mut source, &known).unwrap();
        let certificate = source["combat"]["draw_knowledge"].clone();
        assert_eq!(certificate["available"], true);
        assert_eq!(certificate["source"], "offline_public_rule_observer");
        assert_eq!(certificate["kind"], "public_rule_state");
        assert_eq!(
            certificate["bands"][0]["cards"][0]["known_instance_ids"],
            json!(["visible-1"])
        );
        combat.player.draw.reverse();
        attach_draw_knowledge(&combat, &mut source, &known).unwrap();
        assert_eq!(source["combat"]["draw_knowledge"], certificate);
    }

    /// Build real factory instances so eligibility follows registered upstream card rules.
    fn card(id: &str, upgraded: bool) -> sts2_core::cards::Card {
        let spec = sts2_content::names::resolve_card_name(id, None).unwrap();
        sts2_core::build_card_for_version(spec.id, upgraded, sts2_core::GameVersion::V0_107_1)
            .unwrap()
    }

    /// Exclude non-upgradable curses, preserve modifiers and decode sparse native UI handles.
    #[test]
    fn upgrade_pool_matches_upstream_and_sparse_handles() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm = RunManager::new(719, CharacterId::Ironclad);
        rm.run_state.master_deck = vec![
            card("INJURY", false),
            card("STRIKE_IRONCLAD", false),
            card("DEFEND_IRONCLAD", true),
            card("BASH", false),
        ];
        rm.run_state.master_deck[3].set_enchantment(sts2_core::cards::EnchantmentId::Sharp, 2);
        rm.run_state.pending_deck_upgrades = 1;
        let legal = rm.get_available_actions();
        assert_eq!(
            legal,
            vec![
                RunAction::UpgradeDeckCard { idx: 0 },
                RunAction::UpgradeDeckCard { idx: 1 }
            ]
        );
        let mut projected = project_run(&rm, &legal).unwrap();
        let rows = projected["public_state"]["selection"]["cards"]
            .as_array_mut()
            .unwrap();
        assert_eq!(rows.len(), legal.len());
        assert_eq!(rows[0]["card_id"], "STRIKE_IRONCLAD");
        assert_eq!(rows[1]["card_id"], "BASH");
        assert_eq!(rows[1]["enchantment"]["amount"], 2);
        rows[0]["i"] = json!(41);
        rows[1]["i"] = json!(7);
        let selected = map_action(
            &json!({"best_action":{"type":"select_deck_card","option_index":7}}),
            &projected,
            &legal,
        )
        .unwrap();
        assert_eq!(legal[selected], RunAction::UpgradeDeckCard { idx: 1 });
        assert_eq!(
            projected["public_state"]["run"]["deck"][1]["card_id"],
            "STRIKE_IRONCLAD"
        );
        projected["model_path"]=json!(Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../../../vendor/spire-mentor/sts2-pycli/data/ppo/ppo_fullrun_ironclad_a10_it700.onnx").to_string_lossy());
        projected["features"] = json!("");
        let scored = crate::ppo::policy_evaluate(&projected).unwrap();
        assert_eq!(scored["ok"], true, "{scored}");
        assert!(map_action(&scored, &projected, &legal).is_ok());
    }

    /// Preserve native public inventory capacity, melted flags and the reviewed Book display only.
    #[test]
    fn public_inventory_preserves_capacity_and_visible_relic_state() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm = RunManager::new(719, CharacterId::Regent);
        rm.run_state.max_potion_slots = 2;
        rm.run_state.potions = vec![sts2_core::potions::PotionKind::FirePotion];
        rm.run_state.relics = vec![
            sts2_core::RelicId::BookOfFiveRings,
            sts2_core::RelicId::BurningBlood,
            sts2_core::RelicId::HappyFlower,
        ];
        rm.run_state
            .relic_counters
            .insert(sts2_core::RelicId::BookOfFiveRings, 4);
        rm.run_state
            .relic_counters
            .insert(sts2_core::RelicId::BurningBlood, 42);
        rm.run_state
            .relic_counters
            .insert(sts2_core::RelicId::HappyFlower, 8);
        rm.run_state
            .melted_relics
            .insert(sts2_core::RelicId::BurningBlood);
        let projected = project_run(&rm, &[]).unwrap();
        let belt = projected["public_state"]["run"]["potions"]
            .as_array()
            .unwrap();
        assert_eq!(belt.len(), 2);
        assert_eq!(belt[0]["occupied"], true);
        assert_eq!(belt[1]["occupied"], false);
        assert_eq!(
            projected["public_state"]["run"]["relic_stacks"],
            json!([4, null, 2])
        );
        assert_eq!(
            projected["public_state"]["run"]["relic_is_melted"],
            json!([false, true, false])
        );
    }

    /// Preserve Native's history-dependent status even when the current HP ratio suggests otherwise.
    #[test]
    fn red_skull_status_uses_reviewed_state_without_hp_ratio_inference() {
        let mut combat = sts2_core::CombatState::default();
        combat.relics.push(sts2_core::Relic::new(
            std::num::NonZeroU32::new(1).unwrap(),
            sts2_core::RelicKind::RedSkull,
        ));
        combat.player.current_hp = 60;
        combat.player.max_hp = 160;
        assert_eq!(
            public_relic_status(sts2_core::RelicId::RedSkull, Some(&combat)),
            "Normal"
        );
        combat.relics[0].counter = 1;
        combat.player.current_hp = 59;
        combat.player.max_hp = 80;
        assert_eq!(
            public_relic_status(sts2_core::RelicId::RedSkull, Some(&combat)),
            "Active"
        );
        assert!(public_relic_status(sts2_core::RelicId::BurningBlood, Some(&combat)).is_null());
        combat.relics[0].is_melted = true;
        assert!(public_relic_status(sts2_core::RelicId::RedSkull, Some(&combat)).is_null());
        assert!(public_relic_status(sts2_core::RelicId::RedSkull, None).is_null());
    }

    /// Retain only observed identities while making hidden pile output independent of its real order.
    #[test]
    fn known_hidden_instances_are_observed_and_unordered() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm = RunManager::new(719, CharacterId::Ironclad);
        let mut combat = sts2_core::CombatState::default();
        for (index, id) in ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "DEFEND_IRONCLAD"]
            .into_iter()
            .enumerate()
        {
            let mut instance = card(id, false);
            instance.uid = sts2_core::CardUid::new(index as u32 + 1).unwrap();
            combat.player.draw.push(instance.uid);
            combat.cards.push(instance);
        }
        rm.combat = Some(combat);
        let known = BTreeSet::from(["visible-2".to_owned()]);
        let mut first = json!({"run":{}});
        project_combat(&rm, &mut first).unwrap();
        attach_known_pile_ids(&rm, &mut first, &known).unwrap();
        rm.combat.as_mut().unwrap().player.draw.reverse();
        let mut reversed = json!({"run":{}});
        project_combat(&rm, &mut reversed).unwrap();
        attach_known_pile_ids(&rm, &mut reversed, &known).unwrap();
        assert_eq!(first["combat"]["draw"], reversed["combat"]["draw"]);
        let rows = first["combat"]["draw"].as_array().unwrap();
        assert!(rows.iter().all(|row| row.get("instance_id").is_none()));
        let ids = rows
            .iter()
            .flat_map(|row| row["known_instance_ids"].as_array().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec![&json!("visible-2")]);
    }

    /// Spawn and fake-upgrade original rule cards whose displayed damage is independent of their raw POD base.
    #[test]
    fn wither_projection_uses_original_dynamic_damage_for_generated_instances() {
        sts2_runai::rollout::ensure_content_installed();
        for upgrades in 0..3 {
            let mut combat = sts2_core::CombatState::default();
            let uid = sts2_content::cards::status::spawn_wither(&mut combat,sts2_core::card_effects::PileKind::Hand);
            for _ in 0..upgrades {
                sts2_content::cards::status::wither::fake_upgrade(&mut combat.cards[uid.get() as usize - 1]);
            }
            let damage = sts2_content::cards::status::wither::turn_end_damage(upgrades);
            let row = combat_card(&combat,uid,0).unwrap();
            assert_eq!(row["dynamic_values"],json!([{"name":"Damage","base_value":damage,
                "current_value":damage,"enchanted_value":damage}]));
            // Withering can create a zero-POD card; the display must still expose its original rule damage.
            combat.cards[uid.get() as usize - 1].base_damage = 0;
            assert_eq!(combat_card(&combat,uid,0).unwrap()["dynamic_values"],row["dynamic_values"]);
        }
    }

    /// Native hand holders include ineligible cards and leave selectability unknown.
    #[test]
    fn hand_choice_uses_public_holders_and_native_handles() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm = RunManager::new(719, CharacterId::Ironclad);
        let mut combat = sts2_core::CombatState::default();
        for (index, id) in ["DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "BASH"]
            .into_iter()
            .enumerate()
        {
            let mut instance = card(id, false);
            instance.uid = sts2_core::CardUid::new(index as u32 + 1).unwrap();
            if index == 0 {
                instance.cost = 0;
            }
            combat.player.hand.push(instance.uid);
            combat.cards.push(instance);
        }
        combat.pending_choice = Some(sts2_core::PendingChoice {
            kind: sts2_core::ChoiceKind::MakeFreeInHand,
            options: combat
                .cards
                .iter()
                .skip(1)
                .map(|card| sts2_core::ChoiceOption {
                    card_id: card.card_id,
                    upgraded: false,
                    card_uid: Some(card.uid),
                    source_pile: Some(sts2_core::cards::PileKind::Hand),
                })
                .collect(),
            min_select: 1,
            max_select: 1,
            ..Default::default()
        });
        rm.combat = Some(combat);
        let mut projected = json!({"run":{}});
        project_combat(&rm, &mut projected).unwrap();
        let rows = projected["selection"]["cards"].as_array().unwrap();
        assert_eq!(rows.len(), 3);
        assert!(rows.iter().all(|row| row["selectable"].is_null()));
        assert_eq!(projected["selection"]["kind"], "combat_hand_select");
        assert_eq!(projected["selection"]["underlying_screen"], "COMBAT");
        assert!(projected["combat"].get("pending_choice").is_none());
        assert_eq!(
            witnessed_action(&rm, RunAction::CombatResolveChoice { option: Some(0) }).unwrap(),
            json!({"type":"select_deck_card","option_index":1})
        );
        assert_eq!(
            witnessed_action(&rm, RunAction::CombatResolveChoice { option: Some(1) }).unwrap(),
            json!({"type":"select_deck_card","option_index":2})
        );
    }

    /// Apply an actual producer's multirow PPO choice through the same production macro planner.
    #[test]
    fn producer_multirow_reward_uses_one_legal_head_claim() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm =
            sts2_content::run_callbacks::new_run_manager_for_character(0, CharacterId::Ironclad);
        // The native-UI arm retains unclaimed rows; arena TestMode would drain all remaining rows.
        rm.run_state.direct_event_relic_grant = false;
        let mut common = vec![sts2_core::RelicId::Anchor];
        let mut uncommon = common.clone();
        let mut rare = common.clone();
        assert_eq!(
            rm.enter_treasure(&mut common, &mut uncommon, &mut rare),
            Some(sts2_core::RelicId::Anchor)
        );
        rm.run_state
            .offer_relic_rewards(sts2_core::RelicId::Vajra, 1);
        assert!(rm
            .get_available_actions()
            .contains(&RunAction::CollectTreasure));
        assert_eq!(
            rm.take_action(RunAction::CollectTreasure),
            ActionResult::Advanced
        );

        let envelope: Value = serde_json::from_str(include_str!(
            "../../../solver_validation/fixtures/producer-reward-multirow-relics.json"
        ))
        .unwrap();
        let mut request = json!({"public_state":envelope["public_state"],"features":""});
        request["model_path"] = json!(Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../../../vendor/spire-mentor/sts2-pycli/data/ppo/ppo_fullrun_ironclad_a10_it700.onnx"
        ).to_string_lossy());
        let scored = crate::ppo::policy_evaluate(&request).unwrap();
        assert_eq!(scored["ok"], true, "{scored}");
        assert_eq!(
            scored["best_action"],
            json!({"type":"claim_reward","option_index":1})
        );
        let plan = reward_macro_plan(&rm, &scored, &request).unwrap().unwrap();
        assert_eq!(plan.actions, vec![RunAction::PickRelicReward]);
        for action in plan.actions {
            assert!(rm.get_available_actions().contains(&action));
            assert_eq!(rm.take_action(action), ActionResult::Advanced);
        }
        assert!(rm.run_state.relics.contains(&sts2_core::RelicId::Vajra));
        assert!(!rm.run_state.relics.contains(&sts2_core::RelicId::Anchor));
        assert_eq!(rm.run_state.relic_rewards.len(), 1);
        assert_eq!(
            rm.run_state.relic_rewards[0].relic_id,
            sts2_core::RelicId::Anchor
        );
    }

    /// Check the controlled producer's full-belt continuation without claiming a native UI capture.
    #[test]
    fn producer_full_belt_virtual_followup_matches_atomic_core_inventory() {
        sts2_runai::rollout::ensure_content_installed();
        let mut rm =
            sts2_content::run_callbacks::new_run_manager_for_character(0, CharacterId::Ironclad);
        rm.run_state.direct_event_relic_grant = false;
        rm.run_state.interactive_potion_swap = true;
        let mut common = vec![sts2_core::RelicId::Vajra];
        let mut uncommon = common.clone();
        let mut rare = common.clone();
        rm.enter_treasure(&mut common, &mut uncommon, &mut rare);
        assert!(rm
            .get_available_actions()
            .contains(&RunAction::CollectTreasure));
        assert_eq!(
            rm.take_action(RunAction::CollectTreasure),
            ActionResult::Advanced
        );
        rm.run_state.max_potion_slots = 2;
        rm.run_state.potions = vec![
            sts2_core::potions::PotionKind::FirePotion,
            sts2_core::potions::PotionKind::BlockPotion,
        ];
        assert!(!rm
            .run_state
            .claim_potion_reward(sts2_core::potions::PotionKind::EnergyPotion));

        let envelope: Value = serde_json::from_str(include_str!(
            "../../../solver_validation/fixtures/producer-reward-full-belt-enabled.json"
        ))
        .unwrap();
        let mut request = json!({"public_state":envelope["public_state"],"features":""});
        request["model_path"] = json!(Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../../../vendor/spire-mentor/sts2-pycli/data/ppo/ppo_fullrun_ironclad_a10_it700.onnx"
        ).to_string_lossy());
        let scored = crate::ppo::policy_evaluate(&request).unwrap();
        assert_eq!(scored["ok"], true, "{scored}");
        assert_eq!(
            scored["best_action"],
            json!({"type":"discard_potion","option_index":1})
        );
        let plan = reward_macro_plan(&rm, &scored, &request).unwrap().unwrap();
        assert_eq!(
            plan.actions,
            vec![RunAction::PotionSwap {
                discard_slot: Some(1)
            }]
        );
        assert_eq!(plan.report["native_intermediate_capture"], false);
        assert_eq!(
            plan.report["native_commands"][1],
            json!({"type":"claim_reward","option_index":2})
        );
        // Only inventory and the remaining relic queue are compared; the card page is not materialized here.
        for action in plan.actions {
            assert!(rm.get_available_actions().contains(&action));
            assert_eq!(rm.take_action(action), ActionResult::Advanced);
        }
        assert_eq!(
            rm.run_state.potions,
            vec![
                sts2_core::potions::PotionKind::FirePotion,
                sts2_core::potions::PotionKind::EnergyPotion
            ]
        );
        assert!(rm.run_state.pending_potion_swaps.is_empty());
        assert_eq!(rm.run_state.relic_rewards.len(), 1);
        assert_eq!(
            rm.run_state.relic_rewards[0].relic_id,
            sts2_core::RelicId::Vajra
        );
    }

    /// The published amount is authoritative over approximate fallback metadata prose.
    #[test]
    fn visible_event_variables_replace_approximate_fallback() {
        let source = json!({"loc_key":"THIS_OR_THAT.pages.INITIAL.options.PLAIN","title_vars":{},"vars":{"Gold":48,"HpLoss":6}});
        let mut option = json!({"label":"Plain","description":"Take 6 damage, gain ~55 gold."});
        public_option_text(&source, &mut option);
        let description = option["description"].as_str().unwrap();
        assert!(description.contains("48"));
        assert!(!description.contains("55"));
        assert_eq!(option["description_loc_table"], "events");
    }
}

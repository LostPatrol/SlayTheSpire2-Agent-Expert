//! Shared upstream search service for the JSONL CLI and the persistent Python mirror.
mod public;
mod production;
mod mcts;
mod validation;
mod ppo;
mod networks;
mod alignment;
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use sts2_core::cards::{Card, CardId};
use sts2_core::{Action, CombatPhase, CombatState, MonsterKind, StepResult};

// Keep synthetic probes finite; these bounds do not describe production coverage.
const MAX_ACTIONS: usize = 16;
// Small offline scenarios need no large deck or arbitrary generated input.
const MAX_CARDS: usize = 100;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CardInput {
    id: String,
    #[serde(default)]
    upgraded: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Scenario {
    hand: Vec<CardInput>,
    #[serde(default)]
    draw: Vec<CardInput>,
    #[serde(default)]
    discard: Vec<CardInput>,
    #[serde(default)]
    potions: Vec<String>,
    #[serde(default = "default_hp")]
    hp: i32,
    #[serde(default = "default_energy")]
    energy: i32,
    #[serde(default = "default_enemy_hp")]
    enemy_hp: i32,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", deny_unknown_fields)]
enum ActionInput {
    #[serde(rename = "play_card")]
    PlayCard { card_index: u8, target_index: Option<u8> },
    #[serde(rename = "select_card")]
    SelectCard { choice_indices: Vec<u8> },
    #[serde(rename = "use_potion")]
    UsePotion { potion_index: u8, target_index: Option<u8> },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    scenario: Scenario,
    actions: Vec<ActionInput>,
    #[serde(default)]
    scenario_seed: u64,
}

/// Return the controlled player's initial maximum and current HP.
fn default_hp() -> i32 { 80 }

/// Return enough energy for a small multi-card continuation probe.
fn default_energy() -> i32 { 10 }

/// Keep the synthetic target alive across ordinary test attacks.
fn default_enemy_hp() -> i32 { 100 }

/// Build a canonical card from the content factory; unknown IDs never become placeholders.
fn card_from_input(input: &CardInput, raw_uid: usize) -> Result<Card, String> {
    let spec = sts2_content::names::resolve_card_name(&input.id, None)
        .map_err(|_| format!("unknown_card_id:{}", input.id))?;
    // Echo Form's separate replay loop still drains choices inline; exclude that known gap.
    if spec.id == CardId::EchoFormCard {
        return Err("unsupported_echo_form_replay_choice_path".into());
    }
    let mut card = sts2_core::build_card_for_version(spec.id, input.upgraded, sts2_core::GameVersion::V0_107_1)
        .ok_or_else(|| format!("card_factory_missing:{}", input.id))?;
    card.uid = sts2_core::CardUid::new(raw_uid as u32).ok_or("invalid_card_uid")?;
    Ok(card)
}

/// Build a controlled stable-boundary scenario, not an importer for a real game snapshot.
fn build_scenario(input: &Scenario, seed: u64) -> Result<CombatState, String> {
    if input.hand.len() + input.draw.len() + input.discard.len() > MAX_CARDS || input.hand.len() > 10 {
        return Err("scenario_card_limit".into());
    }
    if input.hp < 1 || input.hp > 80 || !(0..=127).contains(&input.energy) || input.enemy_hp < 1 {
        return Err("invalid_scenario_resources".into());
    }
    let mut state = CombatState::default();
    state.player.current_hp = input.hp;
    state.player.max_hp = 80;
    state.player.energy = input.energy as i8;
    state.player.max_energy = 3;
    state.player.character = Some(sts2_core::CharacterId::Ironclad);
    state.init_player_creature();
    state.phase = CombatPhase::PlayerPlayPhase;
    state.round = 1;
    state.side = sts2_core::Side::Player;
    let enemy_uid = state.spawn_enemy(MonsterKind::Nibbit);
    let enemy = state.creatures.iter_mut().find(|creature| creature.id == enemy_uid.0).ok_or("enemy_spawn_failed")?;
    enemy.current_hp = input.enemy_hp;
    enemy.max_hp = input.enemy_hp;
    for input_card in &input.hand {
        let card = card_from_input(input_card, state.cards.len() + 1)?;
        state.player.hand.push(card.uid);
        state.cards.push(card);
    }
    // Normalize the unknown multiset before assigning UIDs, not after shuffling IDs.
    let mut draw = input.draw.clone();
    draw.sort_by(|left, right| (&left.id, left.upgraded).cmp(&(&right.id, right.upgraded)));
    for input_card in &draw {
        let card = card_from_input(input_card, state.cards.len() + 1)?;
        state.player.draw.push(card.uid);
        state.cards.push(card);
    }
    for input_card in &input.discard {
        let card = card_from_input(input_card, state.cards.len() + 1)?;
        state.player.discard.push(card.uid);
        state.cards.push(card);
    }
    if input.potions.len() > 3 { return Err("scenario_potion_limit".into()); }
    state.player.potion_slots = 3;
    for (slot, name) in input.potions.iter().enumerate() {
        let potion = sts2_content::names::potion_kind_from_str(name)
            .ok_or_else(|| format!("unknown_potion_id:{name}"))?;
        state.player.potions.push(potion);
        state.player.potion_slot_indices.push(slot as u8);
    }
    sts2_core::blindfold::blindfold_root(&mut state, seed);
    Ok(state)
}

/// Emit only the controlled resources and revealed cards needed to inspect continuation.
fn snapshot(state: &CombatState) -> Value {
    let hand: Vec<Value> = state.player.hand.iter().map(|uid| {
        let card = &state.cards[uid.0.get() as usize - 1];
        json!({"id":canonical_public_card_id(card.card_id),"upgraded":card.upgraded,"uid":uid.0.get()})
    }).collect();
    let enemies: Vec<Value> = state.enemy_uids.iter().filter_map(|uid| {
        state.creatures.iter().find(|creature| creature.id == uid.0)
            .map(|creature| json!({"hp":creature.current_hp,"max_hp":creature.max_hp,
                "block":creature.block,"powers":power_rows(state, &creature.powers)}))
    }).collect();
    let pending = state.pending_choice.as_ref().map(|choice| {
        let mut value = sts2_core::parity::pending_choice_to_bridge_value(choice, &state.cards);
        if let Some(cards) = value.get_mut("cards").and_then(Value::as_array_mut) {
            for (row, option) in cards.iter_mut().zip(&choice.options) {
                row["id"] = json!(canonical_public_card_id(option.card_id));
            }
        }
        value
    });
    json!({
        "player":{"hp":state.player.current_hp,"max_hp":state.player.max_hp,
            "energy":state.player.energy,"block":state.player.block,
            "card_plays_finished_this_turn":state.combat_log.card_plays_finished_this_turn,
            "powers":power_rows(state, &state.player_power_uids())},
        "hand":hand,"enemies":enemies,
        "piles":{"draw":state.player.draw.len(),"discard":state.player.discard.len(),
                 "exhaust":state.player.exhaust.len(),"play":state.player.play.len()},
        "pending_choice":pending,"pending_play":state.pending_play.is_some(),
        "pending_draw":state.pending_draw_resume.is_some(),
        "pending_potion_hook":state.pending_potion_hook_resume.is_some(),
        "terminal":state.is_over,
        "potion_count":state.player.potions.len(),
    })
}

/// Map only the native-observed alias verified by Mentor's existing name resolver.
fn canonical_public_card_id(card_id: CardId) -> &'static str {
    match card_id {
        // names::card_id_from_str resolves DARK_EMBRACE to this exact registry variant.
        CardId::DarkEmbraceCard => "DARK_EMBRACE",
        _ => card_id.to_python_name(),
    }
}

/// Resolve public registry names and nonzero magnitudes, excluding private power counters.
fn power_rows(state: &CombatState, uids: &[sts2_core::PowerUid]) -> Vec<Value> {
    let mut rows: Vec<_> = uids.iter().filter_map(|uid| state.powers.get_by_id(uid.0))
        .filter(|power| power.amount != 0)
        .map(|power| (power.kind.python_name(), power.amount)).collect();
    rows.sort_unstable();
    rows.into_iter().map(|(id, amount)| json!({"id":id,"amount":amount})).collect()
}

/// Expand the upstream slot-only potion enumeration for a declared, valid target.
fn action_is_legal(state: &CombatState, action: &Action) -> bool {
    let legal = sts2_core::legal_actions(state);
    if legal.contains(action) { return true; }
    let Action::DrinkPotion { potion_slot, target: Some(target) } = action else { return false; };
    // Upstream lists DrinkPotion(target=None) for every legal slot; apply supports targets.
    if !legal.contains(&Action::DrinkPotion { potion_slot: *potion_slot, target: None }) { return false; }
    let Some(kind) = state.player.potions.get(*potion_slot as usize) else { return false; };
    match sts2_core::potion_effects::lookup_target_type(*kind) {
        Some(sts2_core::potions::PotionTargetType::AnyEnemy) => state.enemy_uids
            .get(*target as usize).is_some_and(|uid| state.creatures.iter()
                .any(|creature| creature.id == uid.0 && creature.current_hp > 0)),
        Some(sts2_core::potions::PotionTargetType::AnyPlayer) => *target == 0,
        _ => false,
    }
}

/// Execute exactly the declared synthetic plan, surfacing choices without an automatic pick.
fn evaluate(request: Request) -> Result<Value, String> {
    if request.actions.len() > MAX_ACTIONS { return Err("scenario_action_limit".into()); }
    let mut state = build_scenario(&request.scenario, request.scenario_seed)?;
    let mut events = Vec::new();
    for input in request.actions {
        let action = match input {
            ActionInput::PlayCard { card_index, target_index } => Action::PlayCard { hand_index: card_index, target: target_index },
            ActionInput::SelectCard { choice_indices } => Action::ResolveChoice { selection: choice_indices.into_iter().collect() },
            ActionInput::UsePotion { potion_index, target_index } => Action::DrinkPotion { potion_slot: potion_index, target: target_index },
        };
        if !action_is_legal(&state, &action) {
            return Ok(json!({"ok":false,"error":"action_not_legal","state":snapshot(&state),"events":events}));
        }
        let result = sts2_core::apply(&mut state, action);
        events.push(json!({"result":format!("{result:?}"),"state":snapshot(&state)}));
        if matches!(result, StepResult::CombatWon | StepResult::CombatLost) { break; }
    }
    Ok(json!({"ok":true,"scope":"offline_synthetic_scenarios_not_live_public_adapter",
        "upstream_commit":"28429066d91ed05c10b7b57597fc624196d197a4",
        "source_modifications":["held_potion_generation_lock_removed","manual_replay_choice_continuation"],
        "game_version":"v0.107.1","state":snapshot(&state),"events":events}))
}

/// Serve one independent synthetic scenario per JSON line with no game or file access.
pub fn run_jsonl() {
    sts2_content::cards::dispatch::ensure_installed();
    sts2_content::relics::dispatch::ensure_installed();
    sts2_content::potions::dispatch::ensure_installed();
    sts2_content::monsters::ensure_power_handlers_installed();
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    for line in stdin.lock().lines() {
        let response = line.map_err(|error| error.to_string())
            .and_then(|line| serde_json::from_str::<Value>(&line).map_err(|error| error.to_string()))
            .and_then(|value| {
                if value.get("op").is_some() || value.get("public_state").is_some() {
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| production::handle(&value)))
                        .map_err(|_| "mentor_engine_panic".to_owned())
                } else {
                    serde_json::from_value::<Request>(value).map_err(|error| error.to_string()).and_then(evaluate)
                }
            })
            .unwrap_or_else(|error| json!({"ok":false,"error":error,"backend":"mentor",
                "rule_snapshot":"mentor-v0.107.1-public-v1"}));
        if writeln!(stdout, "{response}").and_then(|_| stdout.flush()).is_err() { break; }
    }
}

/// Execute stateless metadata, run-policy, and offline validation operations.
pub fn handle(request: &Value) -> Value {
    production::handle(request)
}

/// Search a clone of the complete upstream mirror without a lossy wire round trip.
pub fn handle_state(request: &Value, state: CombatState) -> Value {
    production::handle_state(request, state)
}

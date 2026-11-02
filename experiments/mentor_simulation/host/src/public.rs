//! Action coordinates and public projections over an existing upstream combat state.
//! Native state reconstruction and continuous reconciliation belong to upstream CombatMirror.
use serde_json::{json, Value};
use std::collections::BTreeMap;
use sts2_core::cards::{Card, CardId};
use sts2_core::{Action, CardUid, CombatState, CreatureUid, StepResult};

/// Keep real hand indices attached to the original cards while simulated hands change.
#[derive(Clone, Debug)]
pub struct PublicPosition {
    pub state: CombatState,
    hand: BTreeMap<CardUid, (String, i64)>,
    enemies: BTreeMap<CreatureUid, i64>,
    source: Value,
    internal: bool,
    confirm_single: bool,
}

/// Use the original dynamic Wither display rather than duplicating its rule.
pub(crate) fn public_card_damage(card: &Card) -> i32 {
    if card.card_id == CardId::Wither {
        sts2_content::cards::status::wither::turn_end_damage(
            card.combat_var(sts2_core::cards::VarKey::WitherFakeUpgradeLevel))
    } else { card.base_damage }
}

/// Normalize registry spelling for user-declared card names.
fn normalized(value: &str) -> String {
    value.chars().filter(|c| c.is_ascii_alphanumeric()).flat_map(char::to_uppercase).collect()
}

impl PublicPosition {
    /// Attach current UI coordinates without rebuilding cards, powers, histories, or pending effects.
    pub fn from_core(mut state: CombatState, source: &Value, internal: bool) -> Self {
        let hand = state.player.hand.iter().enumerate().map(|(index, uid)| {
            let slot = source["combat"]["hand"].get(index).and_then(|c| c["i"].as_i64())
                .unwrap_or(index as i64);
            (*uid, (format!("h{}", index), slot))
        }).collect();
        let mut enemies = BTreeMap::new();
        if let Some(rows) = source["combat"]["enemies"].as_array() {
            for (position, row) in rows.iter().enumerate() {
                let matches = state.enemy_uids.iter().filter(|uid| !enemies.contains_key(*uid)).find(|uid| {
                    state.creatures.iter().find(|c| c.id == uid.0).is_some_and(|creature|
                        row["enemy_id"].as_str().is_some_and(|name| creature.species.is_some_and(|kind|
                            normalized(name) == normalized(kind.python_name())))
                        && row["alive"].as_bool().is_none_or(|alive| alive == (creature.current_hp > 0)))
                }).copied();
                // Some public surfaces retain revivable corpses. Bind once at the root; later deaths cannot shift targets.
                let fallback = if rows.len() == state.enemy_uids.len() { state.enemy_uids.get(position).copied() }
                    else { state.enemy_uids.iter().filter(|uid| state.creatures.iter()
                        .any(|c| c.id == uid.0 && c.current_hp > 0)).nth(position).copied() };
                if let Some(uid) = matches.or(fallback) {
                    enemies.insert(uid, row["i"].as_i64().unwrap_or(position as i64));
                }
            }
        } else {
            enemies.extend(state.enemy_uids.iter().enumerate().map(|(i, uid)| (*uid, i as i64)));
        }
        let selected: Vec<usize> = source["selection"]["cards"].as_array().into_iter().flatten()
            .enumerate().filter_map(|(i, card)| (card["selected"] == true).then_some(i)).collect();
        let mut confirm_single = false;
        if let Some(choice) = state.pending_choice.as_mut() {
            // UI clicks do not resolve game effects until confirmation. Only this search copy accumulates them.
            if choice.max_select > 1 {
                choice.picked = selected.iter().filter(|i| **i < choice.options.len()).map(|i| *i as u8).collect();
            } else if selected.len() == 1 && source["selection"]["requires_confirmation"] == true {
                if let Some(option) = choice.options.get(selected[0]).cloned() {
                    choice.options = vec![option];
                    choice.min_select = 1;
                    choice.can_skip = false;
                    confirm_single = true;
                }
            }
        }
        Self { state, hand, enemies, source: source.clone(), internal, confirm_single }
    }

    /// Offline complete-wire inputs use the same upstream importer as the live mirror.
    pub fn for_search(source: &Value, _seed: u64, internal: Option<&Value>) -> Result<Self, Vec<String>> {
        let wire = source.get("wire_state").or_else(|| internal.and_then(|v| v.get("wire_state")))
            .ok_or_else(|| vec!["upstream_mirror_state_required".to_owned()])?;
        let state = sts2_wire::state_from_json(wire).map_err(|e| vec![e.to_string()])?;
        Ok(Self::from_core(state, source, internal.is_some()))
    }

    /// Resolve native enemy coordinates against the current living upstream roster.
    fn enemy_slot(&self, index: u8) -> Option<i64> {
        let uid = self.state.enemy_uids.get(index as usize)?;
        self.enemies.get(uid).copied()
    }

    /// Map one engine action to the existing controller command contract.
    pub fn encode_search_action(&self, action: &Action) -> Value {
        match action {
            Action::PlayCard { hand_index, target } => {
                let Some(uid) = self.state.player.hand.get(*hand_index as usize) else {
                    return json!({"error":"hand_index_missing"});
                };
                let card = &self.state.cards[uid.get() as usize - 1];
                let mut result = json!({"type":"play_card", "card_id":crate::canonical_public_card_id(card.card_id)});
                if let Some((handle, slot)) = self.hand.get(uid) {
                    result["card_handle"] = json!(handle);
                    result["card_index"] = json!(slot);
                }
                if let Some(slot) = target.and_then(|index| self.enemy_slot(index)) {
                    result["target_index"] = json!(slot);
                }
                result
            }
            Action::DrinkPotion { potion_slot, target } | Action::UsePotion { slot:potion_slot, target } => {
                let mut result = json!({"type":"use_potion", "option_index":
                    self.state.player.potion_slot_indices.get(*potion_slot as usize).copied().unwrap_or(*potion_slot)});
                if let Some(slot) = target.and_then(|index| self.enemy_slot(index)) {
                    result["target_index"] = json!(slot);
                }
                result
            }
            Action::EndTurn => json!({"type":"end_turn"}),
            Action::ResolveChoice { selection } => {
                if self.source["selection"].is_object() {
                    if self.confirm_single { return json!({"type":"confirm_selection"}); }
                    if selection.is_empty() {
                        let confirm = self.source["available_actions"].as_array().is_some_and(|rows|
                            rows.iter().any(|row| row == "confirm_selection" || row["type"] == "confirm_selection"));
                        return json!({"type":if confirm {"confirm_selection"} else {"skip_selection"}});
                    }
                    let index = selection[0] as usize;
                    let slot = self.source["selection"]["cards"].get(index)
                        .and_then(|row| row["i"].as_i64()).unwrap_or(index as i64);
                    json!({"type":"select_deck_card", "option_index":slot})
                } else { json!({"type":"select_card", "choice_indices":selection.as_slice()}) }
            }
            _ => json!({"error":"unsupported_native_action"}),
        }
    }

    /// Resolve a declared prefix against the current simulated hand and original UI indices.
    pub fn decode_search_action(&self, action: &Value) -> Result<Action, String> {
        let target = action["target_index"].as_i64().map(|slot| (0..self.state.enemy_uids.len())
            .find(|index| self.enemy_slot(*index as u8) == Some(slot)).map(|i| i as u8)
            .ok_or_else(|| "invalid_enemy_index".to_owned())).transpose()?;
        match action["type"].as_str().unwrap_or("") {
            "end_turn" => Ok(Action::EndTurn),
            "play_card" => {
                let handle = action["card_handle"].as_str().or_else(|| action["handle"].as_str());
                let index = action["card_index"].as_i64();
                let name = action["card_id"].as_str();
                let hand_index = self.state.player.hand.iter().position(|uid| {
                    let named = name.is_none_or(|id| normalized(id) == normalized(
                        crate::canonical_public_card_id(self.state.cards[uid.get() as usize - 1].card_id)));
                    named && self.hand.get(uid).map_or(handle.is_none() && index.is_none(), |(h, i)|
                        handle.is_none_or(|expected| h == expected) && index.is_none_or(|expected| *i == expected))
                }).ok_or("declared_card_not_in_revealed_hand")?;
                Ok(Action::PlayCard { hand_index:hand_index as u8, target })
            }
            "use_potion" => {
                let slot = action["option_index"].as_u64().or_else(|| action["potion_index"].as_u64())
                    .ok_or("missing_potion_slot")?;
                let compact = self.state.player.potion_slot_indices.iter().position(|i| u64::from(*i) == slot)
                    .ok_or("potion_slot_not_occupied")?;
                Ok(Action::DrinkPotion { potion_slot:compact as u8, target })
            }
            "select_card" | "select_deck_card" => {
                let indices = if let Some(rows) = action["choice_indices"].as_array() {
                    rows.iter().map(|v| v.as_u64().and_then(|i| u8::try_from(i).ok())
                        .ok_or_else(|| "invalid_choice_index".to_owned())).collect::<Result<Vec<_>,_>>()?
                } else {
                    let slot = action["option_index"].as_u64().or_else(|| action["choice_index"].as_u64())
                        .ok_or("missing_choice_index")?;
                    let index = self.source["selection"]["cards"].as_array().and_then(|rows|
                        rows.iter().position(|row| row["i"].as_u64() == Some(slot))).unwrap_or(slot as usize);
                    vec![u8::try_from(index).map_err(|_| "invalid_choice_index")?]
                };
                Ok(Action::ResolveChoice { selection:indices.into_iter().collect() })
            }
            "confirm_selection" if self.confirm_single => Ok(Action::ResolveChoice { selection:[0].into_iter().collect() }),
            "confirm_selection" | "skip_selection" => Ok(Action::ResolveChoice { selection:Default::default() }),
            _ => Err("unsupported_action_type".into()),
        }
    }

    /// Run original effects on the private simulation copy.
    pub fn apply(&mut self, action: &Value) -> Result<(), String> {
        let core = self.decode_search_action(action)?;
        if !crate::action_is_legal(&self.state, &core) { return Err("core_action_not_legal".into()); }
        if sts2_mcts::apply_search_action(&mut self.state, core) == StepResult::InvalidTarget {
            return Err("core_invalid_target".into());
        }
        Ok(())
    }

    /// State fidelity is reported by the upstream mirror, independently of search statistics.
    pub fn search_support(&self) -> Value {
        json!({"level":"supported", "blocking_reasons":[], "reasons":[],
            "state_source":"upstream_combat_mirror", "scope":"multi_turn_combat_until_terminal_or_depth_cutoff",
            "hidden_information":if self.internal {"authorized_clairvoyant"} else {"honest_belief_draw"},
            "future_enemy_rules":"upstream_state_machine_cycles_weights_repeat_limits_and_phase_conditions"})
    }

    /// Expose only card faces and current hand handles, never private arena allocation or pile order.
    fn card_view(&self, uid: CardUid, hand: bool) -> Value {
        let card = &self.state.cards[uid.get() as usize - 1];
        let original = sts2_wire::emit::combat::card_to_value(&self.state, card);
        let mut result = json!({"card_id":crate::canonical_public_card_id(card.card_id),
            "upgraded":card.upgraded, "energy_cost":original["cost_override"],
            "star_cost":sts2_core::effective_star_cost_in_combat(&self.state, card),
            "enchantment":original["enchantment"], "flags":original["flags"], "supported":true});
        if hand {
            if let Some((handle, index)) = self.hand.get(&uid) {
                result["handle"] = json!(handle); result["original_index"] = json!(index);
            }
        }
        result
    }

    /// Project upstream display output through the established public result schema.
    pub fn snapshot(&self) -> Value {
        let wire = sts2_wire::emit::combat::combat_state_to_value(&self.state).unwrap_or(Value::Null);
        let mut piles = serde_json::Map::new();
        for (name, pile) in [("draw", &self.state.player.draw), ("discard", &self.state.player.discard),
            ("exhaust", &self.state.player.exhaust)] {
            let mut rows: Vec<_> = pile.iter().map(|uid| self.card_view(*uid, false)).collect();
            rows.sort_by_cached_key(Value::to_string);
            piles.insert(name.into(), json!(rows));
        }
        let enemies: Vec<_> = self.state.enemy_uids.iter().enumerate().filter_map(|(index, uid)|
            self.state.creatures.iter().find(|c| c.id == uid.0).map(|c| json!({
                "i":self.enemy_slot(index as u8), "enemy_id":c.species.map(|s| s.python_name()),
                "hp":{"current":c.current_hp,"max":c.max_hp}, "block":c.block, "alive":c.current_hp>0,
                "powers":c.powers.iter().filter_map(|p|self.state.powers.get_by_id(p.0))
                    .filter(|p|p.amount!=0).map(|p|format!("{} {}",p.kind.python_name(),p.amount)).collect::<Vec<_>>()}))).collect();
        json!({"schema_version":2, "player":{"hp":{"current":self.state.player.current_hp,"max":self.state.player.max_hp},
            "block":self.state.player.block,"energy":self.state.player.energy,"stars":self.state.player.stars,
            "orb_capacity":self.state.player.orbs.capacity,"orb_details":wire["player"]["orbs"],
            "powers":self.state.player_power_uids().iter().filter_map(|uid|self.state.powers.get_by_id(uid.0))
                .filter(|p|p.amount!=0).map(|p|format!("{} {}",p.kind.python_name(),p.amount)).collect::<Vec<_>>()},
            "hand":self.state.player.hand.iter().map(|uid|self.card_view(*uid,true)).collect::<Vec<_>>(),
            "enemies":enemies,"piles":{"draw":self.state.player.draw.len(),"discard":self.state.player.discard.len(),
                "exhaust":self.state.player.exhaust.len()},"pile_cards":piles,"pile_order":"unordered_multiset",
            "pending_choice":self.state.pending_choice.as_ref().map(|choice| json!({"kind":format!("{:?}",choice.kind),
                "min_select":choice.min_select,"max_select":choice.max_select,"can_skip":choice.can_skip})),
            "pending_play":self.state.pending_play.is_some(),"terminal":self.state.is_over,
            "round":self.state.round,"phase":format!("{:?}",self.state.phase),
            "horizon":"sampled_successor_not_final_search_outcome"})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Revivable corpses retain UI slots, and later simulated deaths cannot renumber original targets.
    #[test]
    fn target_coordinates_preserve_corpse_slots_and_root_identity() {
        let mut state = CombatState::default();
        state.init_player_creature();
        let first = state.spawn_enemy(sts2_core::MonsterKind::Nibbit);
        let middle = state.spawn_enemy(sts2_core::MonsterKind::Nibbit);
        state.spawn_enemy(sts2_core::MonsterKind::Nibbit);
        state.creatures.iter_mut().find(|c| c.id == first.0).unwrap().current_hp = 0;
        let source = json!({"combat":{"enemies":[
            {"i":7,"enemy_id":"NIBBIT","alive":false},
            {"i":11,"enemy_id":"NIBBIT","alive":true},
            {"i":29,"enemy_id":"NIBBIT","alive":true}]}});
        let mut root = PublicPosition::from_core(state.clone(), &source, false);
        assert_eq!(root.enemy_slot(1), Some(11));
        root.state.creatures.iter_mut().find(|c| c.id == middle.0).unwrap().current_hp = 0;
        assert_eq!(root.enemy_slot(2), Some(29));
        let living = json!({"combat":{"enemies":[
            {"i":11,"enemy_id":"NIBBIT","alive":true}, {"i":29,"enemy_id":"NIBBIT","alive":true}]}});
        let root = PublicPosition::from_core(state, &living, false);
        assert_eq!(root.enemy_slot(0), None);
        assert_eq!(root.enemy_slot(1), Some(11));
        assert_eq!(root.enemy_slot(2), Some(29));
    }
}

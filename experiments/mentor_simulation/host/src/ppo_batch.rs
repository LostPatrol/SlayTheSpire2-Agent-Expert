//! Projects publicly queued native batch targets through the original deterministic run actions.
//! Returned changes are hypothetical until the native batch is confirmed, never observed mutations.

use std::collections::BTreeSet;
use sts2_core::cards::EnchantmentId;
use sts2_core::run_manager::{ActionResult, RunAction, RunManager};
use sts2_core::runstate::{PendingDeckEnchant, RunState};

/// Counterfactual run and stable original-deck identities after already-selected targets.
pub struct BatchProjection {
    pub run: RunState,
    pub original_deck_indices: Vec<usize>,
    pub applied_original_indices: Vec<usize>,
}

/// Apply only a strict, deterministic prefix of a mandatory native batch using upstream run actions.
/// The caller must label the returned effects as queued and wait for real native confirmation.
pub fn project_selected(
    mut public_run: RunState,
    operation: &str,
    selected_deck_indices: &[usize],
    required_count: usize,
    enchantment: Option<(EnchantmentId, i32)>,
) -> Result<BatchProjection, String> {
    let operation = operation.to_ascii_lowercase();
    if operation == "transform" {
        return Err("ppo_batch_random_transform_continuation_not_observed".into());
    }
    if !matches!(operation.as_str(), "upgrade" | "remove" | "enchant") {
        return Err(format!("ppo_batch_operation_not_supported:{operation}"));
    }
    if required_count < 2 {
        return Err("ppo_batch_requires_multiple_mandatory_targets".into());
    }
    if selected_deck_indices.len() >= required_count {
        return Err("ppo_batch_complete_requires_native_confirmation".into());
    }
    let required = u32::try_from(required_count).map_err(|_| "ppo_batch_target_count_invalid")?;
    let mut unique = BTreeSet::new();
    for selected in selected_deck_indices {
        if *selected >= public_run.master_deck.len() || !unique.insert(*selected) {
            return Err("ppo_batch_selected_target_invalid_or_duplicate".into());
        }
    }

    sts2_content::cards::dispatch::ensure_installed();
    sts2_content::relics::dispatch::install_default_registry();
    match operation.as_str() {
        "upgrade" => {
            if public_run.pending_deck_upgrades != 0 && public_run.pending_deck_upgrades != required
            {
                return Err("ppo_batch_upgrade_queue_conflict".into());
            }
            public_run.pending_deck_upgrades = required;
        }
        "remove" => {
            if public_run.pending_deck_removals != 0 && public_run.pending_deck_removals != required
            {
                return Err("ppo_batch_removal_queue_conflict".into());
            }
            public_run.pending_deck_removals = required;
        }
        "enchant" => {
            let (id, amount) = enchantment.ok_or("ppo_batch_enchantment_identity_missing")?;
            if public_run.pending_deck_enchants.is_empty() {
                public_run.pending_deck_enchants = vec![
                    PendingDeckEnchant {
                        enchantment: id,
                        amount,
                        type_restriction: None,
                        secondary_type: None,
                    };
                    required_count
                ];
            } else if public_run.pending_deck_enchants.len() != required_count
                || public_run
                    .pending_deck_enchants
                    .iter()
                    .any(|entry| entry.enchantment != id || entry.amount != amount)
            {
                return Err("ppo_batch_enchantment_queue_conflict".into());
            }
        }
        _ => unreachable!(),
    }

    let mut original_deck_indices: Vec<_> = (0..public_run.master_deck.len()).collect();
    // The scratch manager never receives a native seed or hidden continuation; these actions are deterministic.
    let mut manager = RunManager::new(0, public_run.character_id);
    manager.run_state = public_run;
    for original in selected_deck_indices {
        let current = original_deck_indices
            .iter()
            .position(|index| index == original)
            .ok_or("ppo_batch_selected_target_no_longer_present")?;
        let action = match operation.as_str() {
            "upgrade" => {
                let eligible: Vec<_> = manager
                    .run_state
                    .master_deck
                    .iter()
                    .enumerate()
                    .filter(|(_, card)| card.is_upgradable())
                    .map(|(index, _)| index)
                    .collect();
                let idx = eligible
                    .iter()
                    .position(|index| *index == current)
                    .ok_or("ppo_batch_selected_upgrade_target_not_eligible")?;
                RunAction::UpgradeDeckCard { idx }
            }
            "remove" => {
                let idx = manager
                    .run_state
                    .removal_offer_order()
                    .iter()
                    .position(|index| *index == current)
                    .ok_or("ppo_batch_selected_removal_target_not_eligible")?;
                RunAction::RemoveDeckCard { idx }
            }
            "enchant" => {
                let idx = manager
                    .enchant_eligible_deck_indices()
                    .iter()
                    .position(|index| *index == current)
                    .ok_or("ppo_batch_selected_enchantment_target_not_eligible")?;
                RunAction::EnchantDeckCard { idx }
            }
            _ => unreachable!(),
        };
        if manager.take_action(action) != ActionResult::Advanced {
            return Err("ppo_batch_original_action_did_not_advance".into());
        }
        if operation == "remove" {
            original_deck_indices.remove(current);
        }
    }
    // A strictly partial prefix leaves its original queue nonzero, so upstream settle cannot fire batch tails.
    Ok(BatchProjection {
        run: manager.run_state,
        original_deck_indices,
        applied_original_indices: selected_deck_indices.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use sts2_core::cards::{Card, CardId};

    /// Build the actual pinned card template rather than reproducing upgrade or enchant rules.
    fn card(id: CardId) -> Card {
        sts2_content::cards::dispatch::ensure_installed();
        sts2_content::relics::dispatch::install_default_registry();
        sts2_core::build_card_for_version(id, false, sts2_core::GameVersion::V0_107_1).unwrap()
    }

    /// Keep a synthetic public resource baseline, explicitly separate from a native capture.
    fn run(cards: Vec<Card>) -> RunState {
        let mut run = RunState::new();
        run.game_version = sts2_core::GameVersion::V0_107_1;
        run.master_deck = cards;
        run.current_hp = 70;
        run.max_hp = 80;
        run.gold = 123;
        run
    }

    /// Partial upgrade batches use the original actions without committing deferred costs.
    #[test]
    fn selected_upgrades_use_the_original_subset_actions_and_preserve_pending_damage() {
        let mut source = run(vec![
            card(CardId::StrikeIronclad),
            card(CardId::DefendIronclad),
            card(CardId::Anger),
        ]);
        source.pending_post_upgrade_hp_loss = 10;
        let projected = project_selected(source.clone(), "upgrade", &[1, 0], 3, None).unwrap();
        let mut original = RunManager::new(0, source.character_id);
        original.run_state = source;
        original.run_state.pending_deck_upgrades = 3;
        assert_eq!(
            original.take_action(RunAction::UpgradeDeckCard { idx: 1 }),
            ActionResult::Advanced
        );
        assert_eq!(
            original.take_action(RunAction::UpgradeDeckCard { idx: 0 }),
            ActionResult::Advanced
        );
        assert_eq!(projected.run.master_deck, original.run_state.master_deck);
        assert_eq!(projected.run.current_hp, 70);
        assert_eq!(projected.run.pending_post_upgrade_hp_loss, 10);
        assert_eq!(projected.run.pending_deck_upgrades, 1);
        assert_eq!(projected.original_deck_indices, vec![0, 1, 2]);
        assert_eq!(projected.applied_original_indices, vec![1, 0]);
    }

    /// Removals preserve remaining native identities despite original deck-index shifts.
    #[test]
    fn selected_removals_preserve_physical_indices_and_do_not_finalize_the_batch() {
        let mut source = run(vec![
            card(CardId::StrikeIronclad),
            card(CardId::Injury),
            card(CardId::DefendIronclad),
            card(CardId::Anger),
        ]);
        source.pending_post_removal_hp_loss = 16;
        source.pending_post_removal_gold_loss = 11;
        source.pending_post_removal_cards.push(card(CardId::Wound));
        let projected = project_selected(source, "remove", &[2, 1], 3, None).unwrap();
        assert_eq!(
            projected
                .run
                .master_deck
                .iter()
                .map(|card| card.card_id)
                .collect::<Vec<_>>(),
            vec![CardId::StrikeIronclad, CardId::Anger]
        );
        assert_eq!(projected.original_deck_indices, vec![0, 3]);
        assert_eq!(projected.applied_original_indices, vec![2, 1]);
        assert_eq!((projected.run.current_hp, projected.run.gold), (70, 123));
        assert_eq!(projected.run.pending_deck_removals, 1);
        assert_eq!(projected.run.pending_post_removal_hp_loss, 16);
        assert_eq!(projected.run.pending_post_removal_gold_loss, 11);
        assert_eq!(projected.run.pending_post_removal_cards.len(), 1);
    }

    /// Fixed enchantments use the upstream type restriction and actual enchant mutation.
    #[test]
    fn fixed_enchantment_maps_to_the_original_eligible_subset() {
        let source = run(vec![
            card(CardId::StrikeIronclad),
            card(CardId::DefendIronclad),
            card(CardId::DefendIronclad),
        ]);
        let projected =
            project_selected(source, "enchant", &[2], 2, Some((EnchantmentId::Nimble, 3))).unwrap();
        assert_eq!(
            projected.run.master_deck[2].enchantment(EnchantmentId::Nimble),
            3
        );
        assert_eq!(
            projected.run.master_deck[1].enchantment(EnchantmentId::Nimble),
            0
        );
        assert_eq!(projected.run.pending_deck_enchants.len(), 1);
        assert_eq!(projected.original_deck_indices, vec![0, 1, 2]);
    }

    /// An empty prefix retains every observed card and leaves the queue pending.
    #[test]
    fn zero_selected_targets_never_mutate_a_card_or_finish_the_queue() {
        let source = run(vec![
            card(CardId::StrikeIronclad),
            card(CardId::DefendIronclad),
        ]);
        let original = source.master_deck.clone();
        let projected = project_selected(source, "upgrade", &[], 2, None).unwrap();
        assert_eq!(projected.run.master_deck, original);
        assert_eq!(projected.run.pending_deck_upgrades, 2);
        assert!(projected.applied_original_indices.is_empty());
    }

    /// Invalid and stochastic prefixes cannot masquerade as deterministic projection.
    #[test]
    fn duplicate_ineligible_random_and_complete_batches_remain_explicit_errors() {
        let source = run(vec![
            card(CardId::StrikeIronclad),
            card(CardId::DefendIronclad),
            card(CardId::Anger),
        ]);
        assert!(project_selected(source.clone(), "upgrade", &[0, 0], 3, None).is_err());
        assert!(project_selected(source.clone(), "remove", &[0, 1], 2, None).is_err());
        assert!(project_selected(source.clone(), "transform", &[0], 2, None).is_err());
        assert!(project_selected(
            source.clone(),
            "enchant",
            &[0],
            2,
            Some((EnchantmentId::Nimble, 1))
        )
        .is_err());
        let mut upgraded = source;
        upgraded.master_deck[0].upgrade_for(upgraded.game_version);
        assert!(project_selected(upgraded, "upgrade", &[0], 2, None).is_err());
    }
}

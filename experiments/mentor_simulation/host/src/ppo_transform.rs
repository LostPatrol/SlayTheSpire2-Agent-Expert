//! Projects selected native transform targets through original versioned run actions and sampled RNG.
//! Public source descriptors and observed relic counters belong to the caller; effects remain hypothetical.

use std::collections::BTreeSet;
use sts2_core::run_manager::{ActionResult, RunAction, RunManager};
use sts2_core::run_rng_set::RunRngSet;
use sts2_core::runstate::{PendingDeckTransform, RunState};

/// A sampled conditional run; appended hook cards have no original native target identity.
pub struct TransformProjection {
    pub run: RunState,
    pub original_deck_indices: Vec<Option<usize>>,
    pub applied_original_indices: Vec<usize>,
}

/// Replay a strict selected prefix using publicly justified original descriptors and one hypothetical world.
/// The caller must exclude applied identities from further native choices and retain every failed world.
pub fn project_selected_transform(
    mut public_run: RunState,
    pending: Vec<PendingDeckTransform>,
    selected_original_indices: &[usize],
    required_count: usize,
    world_seed: u64,
) -> Result<TransformProjection, String> {
    if required_count == 0 || pending.len() != required_count {
        return Err("ppo_transform_public_source_descriptor_count_mismatch".into());
    }
    if selected_original_indices.len() >= required_count {
        return Err("ppo_transform_complete_requires_native_confirmation".into());
    }
    if public_run.current_hp <= 0 || public_run.is_over {
        return Err("ppo_transform_public_run_already_terminal".into());
    }
    let mut seen = BTreeSet::new();
    for index in selected_original_indices {
        if *index >= public_run.master_deck.len() || !seen.insert(*index) {
            return Err("ppo_transform_selected_target_invalid_or_duplicate".into());
        }
    }
    if !public_run.pending_deck_transforms.is_empty()
        && public_run.pending_deck_transforms != pending
    {
        return Err("ppo_transform_public_pending_source_conflict".into());
    }

    sts2_content::cards::dispatch::ensure_installed();
    sts2_content::relics::dispatch::ensure_installed();
    public_run.pending_deck_transforms = pending;
    // All streams are hypothetical. Named event rolls derive from this sampled seed with original skip counts.
    public_run.rng = RunRngSet::new_for(public_run.game_version, world_seed);
    let mut original_deck_indices: Vec<_> = (0..public_run.master_deck.len()).map(Some).collect();
    // The constructor seed is discarded; the installed public run owns the independent sampled RNG.
    let mut manager = RunManager::new(0, public_run.character_id)
        .with_content_callbacks(sts2_content::run_callbacks::ironclad_content_callbacks());
    manager.run_state = public_run;
    for original in selected_original_indices {
        let current = original_deck_indices
            .iter()
            .position(|index| *index == Some(*original))
            .ok_or("ppo_transform_selected_original_identity_missing")?;
        let eligible = manager.transform_eligible_deck_indices();
        let idx = eligible
            .iter()
            .position(|index| *index == current)
            .ok_or("ppo_transform_selected_target_not_in_original_filter")?;
        let action = RunAction::TransformDeckCard { idx };
        if !manager.get_available_actions().contains(&action) {
            return Err("ppo_transform_other_original_queue_preempts_this_action".into());
        }
        let mut survivors = manager.run_state.master_deck.clone();
        survivors.remove(current);
        if manager.take_action(action) != ActionResult::Advanced {
            return Err("ppo_transform_original_action_did_not_advance".into());
        }
        let deck = &manager.run_state.master_deck;
        if deck.len() <= survivors.len() {
            return Err("ppo_transform_original_callback_did_not_materialize_replacement".into());
        }
        // The original Deck transform appends the replacement before deck-add listeners append their cards.
        if deck[..survivors.len()] != survivors[..] {
            return Err("ppo_transform_hook_changed_existing_deck_identity_order".into());
        }
        original_deck_indices.remove(current);
        original_deck_indices.push(Some(*original));
        original_deck_indices.resize(deck.len(), None);
        if manager.run_state.current_hp <= 0 || manager.run_state.is_over {
            return Err("ppo_transform_projected_prefix_ends_run_before_next_choice".into());
        }
    }
    Ok(TransformProjection {
        run: manager.run_state,
        original_deck_indices,
        applied_original_indices: selected_original_indices.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use sts2_core::cards::{Card, CardId};
    use sts2_core::RelicId;

    /// Build the actual pinned card template for deterministic reference assertions.
    fn card(id: CardId) -> Card {
        sts2_content::cards::dispatch::ensure_installed();
        sts2_content::relics::dispatch::ensure_installed();
        sts2_core::build_card_for_version(id, false, sts2_core::GameVersion::V0_107_1).unwrap()
    }

    /// Model only a synthetic public resource state, never a native save or hidden RNG snapshot.
    fn run() -> RunState {
        let mut run = RunState::new();
        run.game_version = sts2_core::GameVersion::V0_107_1;
        run.current_hp = 40;
        run.max_hp = 80;
        run.master_deck = vec![
            card(CardId::StrikeIronclad),
            card(CardId::DefendIronclad),
            card(CardId::Anger),
        ];
        run
    }

    /// Construct the source's known fixed transform descriptor without duplicating its mutation rule.
    fn fixed() -> PendingDeckTransform {
        PendingDeckTransform {
            basic_only: true,
            fixed_target: Some(card(CardId::Peck)),
            ..Default::default()
        }
    }

    /// Named event skips retain the original callback's RNG domain.
    #[test]
    fn named_stream_skip_matches_the_original_callback_and_keeps_niche_untouched() {
        let source = run();
        let entry = PendingDeckTransform {
            event_stream_roll: Some(("MORPHIC_GROVE", 1)),
            ..Default::default()
        };
        let world_seed = 714u64;
        let projected = project_selected_transform(
            source.clone(),
            vec![entry.clone(), entry],
            &[0],
            2,
            world_seed,
        )
        .unwrap();
        let mut expected = source;
        expected.rng = RunRngSet::new_for(expected.game_version, world_seed);
        let id = sts2_content::cards::spec_index::roll_transform_replacement_event_stream(
            &expected,
            expected.character_id,
            CardId::StrikeIronclad,
            "MORPHIC_GROVE",
            1,
        )
        .unwrap();
        assert_eq!(projected.run.master_deck.last().unwrap().card_id, id);
        assert_eq!(
            projected.run.rng.niche.counter(),
            expected.rng.niche.counter()
        );
        assert_eq!(projected.run.pending_deck_transforms.len(), 1);
        assert_eq!(
            projected.original_deck_indices,
            vec![Some(1), Some(2), Some(0)]
        );
    }

    /// Fixed replacements preserve original target identities without a random draw.
    #[test]
    fn fixed_target_consumes_no_rng_and_moves_the_selected_identity_to_the_tail() {
        let world_seed = 17u64;
        let projected =
            project_selected_transform(run(), vec![fixed(), fixed()], &[1], 2, world_seed).unwrap();
        assert_eq!(
            projected
                .run
                .master_deck
                .iter()
                .map(|card| card.card_id)
                .collect::<Vec<_>>(),
            vec![CardId::StrikeIronclad, CardId::Anger, CardId::Peck]
        );
        let initial_rng = RunRngSet::new_for(projected.run.game_version, world_seed);
        assert_eq!(
            projected.run.rng.niche.counter(),
            initial_rng.niche.counter()
        );
        assert_eq!(
            projected.run.rng.transformations.counter(),
            initial_rng.transformations.counter()
        );
        assert_eq!(
            projected.original_deck_indices,
            vec![Some(0), Some(2), Some(1)]
        );
        assert_eq!(projected.applied_original_indices, vec![1]);
    }

    /// Original add hooks can append cards and consume publicly known counters.
    #[test]
    fn bing_bong_hook_cards_have_no_native_identity_and_public_book_counter_heals() {
        let mut source = run();
        source.relics = vec![RelicId::BingBong, RelicId::BookOfFiveRings];
        // Original-DLL public DisplayAmount4 was verified to mean four accumulated deck additions.
        source.relic_counters.insert(RelicId::BookOfFiveRings, 4);
        let projected =
            project_selected_transform(source, vec![fixed(), fixed()], &[0], 2, 51).unwrap();
        assert_eq!(projected.run.master_deck.len(), 4);
        assert_eq!(projected.run.master_deck[2].card_id, CardId::Peck);
        assert_eq!(projected.run.master_deck[3].card_id, CardId::Peck);
        assert_eq!(
            projected.original_deck_indices,
            vec![Some(1), Some(2), Some(0), None]
        );
        assert_eq!(projected.run.current_hp, 60);
        assert_eq!(
            projected.run.relic_counters.get(&RelicId::BookOfFiveRings),
            Some(&1)
        );
        assert_eq!(projected.run.pending_deck_transforms.len(), 1);
    }

    /// Native targets still obey the original source's Basic-only predicate.
    #[test]
    fn the_original_basic_filter_rejects_an_unoffered_nonbasic_target() {
        assert!(project_selected_transform(run(), vec![fixed(), fixed()], &[2], 2, 11).is_err());
    }

    /// Invalid prefixes never become silently scored partial worlds.
    #[test]
    fn duplicate_complete_and_missing_source_descriptors_are_explicit_failures() {
        assert!(project_selected_transform(run(), vec![fixed(); 3], &[0, 0], 3, 0).is_err());
        assert!(project_selected_transform(run(), vec![fixed(); 2], &[0, 1], 2, 0).is_err());
        assert!(project_selected_transform(run(), vec![], &[0], 2, 0).is_err());
    }
}

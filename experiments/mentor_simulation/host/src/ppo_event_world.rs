//! Public ThisOrThat prior: preserve displayed Plain gold while sampling future relic effects.
//! The representative named-event seed is not the actual master seed or its full posterior.
use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};

use sts2_content::events::dispatch::{present, preview_option_effect, spec_for, OptionEffectDelta};
use sts2_core::events::EventVar;
use sts2_core::run_rng_set::RunRngSet;
use sts2_core::runstate::RelicRarity;
use sts2_core::{EventId, GameVersion, RunState};

// A bounded, deterministic public search suffices for the displayed event Gold values.
const CONDITION_SEED_SEARCH_LIMIT: u64 = 4096;
// Cache only public Gold-to-representative mappings; no root seed or cursor is stored.
static CONDITION_SEEDS: OnceLock<Mutex<BTreeMap<i64, u64>>> = OnceLock::new();
// Original player-bag setup order; shared bags and future rooms are deliberately not generated.
const SHUFFLE_ORDER: [RelicRarity; 4] = [
    RelicRarity::Uncommon,
    RelicRarity::Common,
    RelicRarity::Rare,
    RelicRarity::Shop,
];

/// Find a deterministic independent seed whose original presentation displays this Gold.
/// Cache failures never block recomputing the public mapping.
pub fn conditioning_seed(public_gold: i64) -> Result<u64, String> {
    let cache = CONDITION_SEEDS.get_or_init(|| Mutex::new(BTreeMap::new()));
    if let Ok(guard) = cache.lock() {
        if let Some(seed) = guard.get(&public_gold) {
            return Ok(*seed);
        }
    }
    let mut probe = RunState::new();
    probe.game_version = GameVersion::V0_107_1;
    probe.current_event = Some(EventId::ThisOrThat);
    for seed in 0..CONDITION_SEED_SEARCH_LIMIT {
        probe.rng = RunRngSet::new_for(GameVersion::V0_107_1, seed);
        let matching = present(&probe).is_some_and(|options| {
            options.iter().any(|option| {
                option.vars.iter().any(|(name, value)| {
                    *name == "Gold" && matches!(value, EventVar::Int(amount) if *amount == public_gold)
                })
            })
        });
        if matching {
            if let Ok(mut guard) = cache.lock() {
                guard.insert(public_gold, seed);
            }
            return Ok(seed);
        }
    }
    Err(format!("this_or_that_public_gold_has_no_representative_seed:{public_gold}"))
}

/// Preview original effects under a declared public prior, never a hidden root RNG.
/// The named event seed preserves observed Plain Gold; other streams and the shuffled bag
/// come from world_seed. This separation is not a full master-seed conditional posterior.
pub fn preview_this_or_that(
    mut public_run: RunState,
    public_gold: i64,
    option_key: &str,
    world_seed: u64,
    full_unlock: bool,
) -> Result<OptionEffectDelta, String> {
    let spec = spec_for(EventId::ThisOrThat);
    let index = spec
        .options
        .iter()
        .position(|option| option.option_id.eq_ignore_ascii_case(option_key))
        .ok_or_else(|| format!("unknown_this_or_that_option:{option_key}"))?;
    let ornate = spec.options[index].option_id == "ornate";
    if ornate && !full_unlock {
        return Err("this_or_that_ornate_requires_declared_full_unlock_relic_prior".into());
    }
    sts2_content::cards::dispatch::ensure_installed();
    sts2_content::relics::dispatch::ensure_installed();
    public_run.game_version = GameVersion::V0_107_1;
    public_run.current_event = Some(EventId::ThisOrThat);
    public_run.rng = RunRngSet::new_for(GameVersion::V0_107_1, world_seed);
    if full_unlock {
        let (mut buckets, fallback, boss) =
            sts2_content::run_callbacks::default_relic_grab_bag_for(public_run.character_id);
        for rarity in SHUFFLE_ORDER {
            if let Some(bucket) = buckets.get_mut(&rarity) {
                public_run.rng.up_front.shuffle(bucket);
            }
        }
        public_run.populate_relic_grab_bag_by_rarity(buckets, fallback, boss);
    }
    // named() derives from this field; constructed reward/up_front/etc. streams remain independent.
    public_run.rng.seed = conditioning_seed(public_gold)?;
    preview_option_effect(&public_run, index)
        .ok_or_else(|| format!("original_this_or_that_preview_unavailable:{option_key}"))
}

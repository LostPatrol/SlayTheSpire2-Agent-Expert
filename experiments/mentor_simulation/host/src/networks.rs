//! Runtime network selection over the original upstream search setup and trained assets.
//! Legacy value weights retain their native 88-column prefix instead of fabricated inputs.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;
use sts2_core::run_manager::RunManager;
use sts2_core::runstate::{Room, RoomType};
use sts2_core::{CharacterId, CombatState};
use sts2_mcts::{LeafEval, LearnedValueNet, MctsConfig, Policy, RolloutPolicyKind, SharedPolicy};
use sts2_runai::policy::{CombatBudget, HeuristicPolicy, MctsCombatPolicy};

// Embed the genuine trained legacy artifact so deployed hosts do not need a source-tree path.
const VALUE_BYTES: &[u8] = include_bytes!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../vendor/spire-mentor/sts2-rs/data/value_policy_net/h16.json"
));
// The trainer's original feature prefix ends where the append-only lookahead block starts.
const LEGACY_VALUE_DIM: usize = 88;

/// One prepared tree's actual policy, evaluator and reproducible component identity.
pub struct Setup {
    pub config: MctsConfig,
    pub leaf: LeafEval,
    pub prior: SharedPolicy,
    pub identity: Value,
}

/// Read the four independent runtime switches without silently correcting unknown values.
fn option<'a>(
    request: &'a Value,
    key: &str,
    default: &'a str,
    allowed: &[&str],
) -> Result<&'a str, String> {
    let value = request.get(key).map_or(Ok(default), |value| {
        value.as_str().ok_or_else(|| format!("invalid_{key}"))
    })?;
    if allowed.contains(&value) {
        Ok(value)
    } else {
        Err(format!("invalid_{key}:{value}"))
    }
}

/// Hash the actual embedded bytes, not an asset filename or historical label.
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// Return the shipped rollout text corresponding to the upstream character dispatch.
fn rollout_bytes(character: Option<CharacterId>) -> Option<&'static [u8]> {
    macro_rules! asset {
        ($name:literal) => {
            include_bytes!(concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/../../../vendor/spire-mentor/sts2-rs/data/rollout_nets/",
                $name,
                ".json"
            ))
            .as_slice()
        };
    }
    match character {
        Some(CharacterId::Ironclad) => Some(asset!("ironclad")),
        Some(CharacterId::Silent) => Some(asset!("silent")),
        Some(CharacterId::Defect) => Some(asset!("defect")),
        Some(CharacterId::Regent) => Some(asset!("regent")),
        Some(CharacterId::Necrobinder) => Some(asset!("necrobinder")),
        _ => None,
    }
}

/// Describe actual asset dimensions and training markers without exposing weight tensors.
fn asset_identity(bytes: &[u8], name: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|error| format!("network_asset_json:{name}:{error}"))?;
    Ok(
        json!({"asset":name,"sha256":sha(bytes),"bytes":bytes.len(),"arch":value["arch"],
        "feature_version":value.get("feature_version").cloned().unwrap_or(json!(1)),
        "state_dim":value["state_dim"],"action_dim":value["action_dim"],"hidden":value["hidden"],
        "embedding_dim":value["emb_dim"],"action_keys":value["keys"].as_array().map(Vec::len),
        "n_train":value["n_train"],"n_val":value["n_val"],"epochs":value["epochs_run"],
        "heads_trained":value["heads_trained"]}),
    )
}

/// Cache immutable embedded metadata by byte identity and label; dynamic files stay uncached.
fn embedded_identity(bytes: &'static [u8], name: &str) -> Result<Value, String> {
    static CACHE: OnceLock<Mutex<HashMap<(usize, usize, String), Result<Value, String>>>> =
        OnceLock::new();
    let mut cache = CACHE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap();
    // Embedded bytes keep their address and content for the process lifetime.
    cache
        .entry((bytes.as_ptr() as usize, bytes.len(), name.to_owned()))
        .or_insert_with(|| asset_identity(bytes, name))
        .clone()
}

/// Feature names are build-time constants, so their serialized hashes never depend on a root.
fn feature_identity() -> Value {
    static IDENTITY: OnceLock<Value> = OnceLock::new();
    IDENTITY.get_or_init(|| {
        let features=sts2_core::EVAL_FEATURE_NAMES_WIDE;
        json!({"wide_dim":sts2_core::WIDE_FEATURE_DIM,
            "wide_names_sha256":sha(serde_json::to_string(&features.to_vec()).unwrap().as_bytes()),
            "legacy88_names_sha256":sha(serde_json::to_string(&features[..LEGACY_VALUE_DIM]).unwrap().as_bytes())})
    }).clone()
}

/// Enumerate shipped network roles, including the deliberately non-default legacy value model.
pub fn inventory() -> Value {
    static INVENTORY: OnceLock<Value> = OnceLock::new();
    INVENTORY.get_or_init(|| {
    let rollout= [CharacterId::Ironclad,CharacterId::Silent,CharacterId::Defect,CharacterId::Regent,CharacterId::Necrobinder]
        .into_iter().filter_map(|character|rollout_bytes(Some(character)).map(|bytes|
            embedded_identity(bytes,&format!("rollout:{character:?}")).unwrap_or_else(|error|json!({"error":error})))).collect::<Vec<_>>();
    let prior=sts2_mcts::rollout_net::builtin_prior_json(CharacterId::Ironclad)
        .map(|text|embedded_identity(text.as_bytes(),"prior:Ironclad").unwrap_or_else(|error|json!({"error":error})));
    json!({"rollout":rollout,"action_prior":prior,
        "value":embedded_identity(VALUE_BYTES,"value:h16:legacy88").unwrap_or_else(|error|json!({"error":error})),
        "value_default":"TexelWide","current_wide_features":sts2_core::WIDE_FEATURE_DIM,
        "legacy_value_compatibility":"full_current_features_then_native_first_88_only"})
    }).clone()
}

/// Load the genuine legacy weights once; explicit learned selection fails if loading is invalid.
fn value_net() -> Result<Arc<LearnedValueNet>, String> {
    static VALUE: OnceLock<Result<Arc<LearnedValueNet>, String>> = OnceLock::new();
    VALUE
        .get_or_init(|| {
            LearnedValueNet::from_legacy_prefix_weights_json(VALUE_BYTES)
                .map(Arc::new)
                .map_err(|error| format!("learned_value_asset:{error:?}"))
        })
        .clone()
}

/// Prefer explicit public run context; missing final-boss facts never become assumed knowledge.
fn context(request: &Value, state: &CombatState) -> Value {
    let provided = &request["network_context"];
    let run = &request["public_state"]["run"];
    let room = provided["room_type"]
        .as_str()
        .or_else(|| run["room_type"].as_str());
    let boss = room.is_some_and(|room| room.eq_ignore_ascii_case("boss"))
        || state.is_boss
        || crate::mcts::boss_combat(state);
    let elite = room.is_some_and(|room| room.eq_ignore_ascii_case("elite")) || state.is_elite;
    let act = provided["act_index"]
        .as_u64()
        .or_else(|| run["act_index"].as_u64())
        .or_else(|| run["act"].as_u64())
        .unwrap_or(u64::from(state.act_index));
    let last = provided["is_last_act"]
        .as_bool()
        .or_else(|| run["is_last_act"].as_bool())
        .or_else(|| {
            run["act_count"]
                .as_u64()
                .map(|count| count > 0 && act >= count)
        });
    let second = provided["awaiting_second_boss"]
        .as_bool()
        .or_else(|| run["awaiting_second_boss"].as_bool());
    json!({"room_type":if boss {"boss"}else if elite {"elite"}else{"common"},"act_index":act,
        "is_last_act":last,"awaiting_second_boss":second,"ascension":state.ascension,
        "floor":provided["floor"].as_i64().or_else(||run["floor"].as_i64()),
        "context_source":"explicit_network_context_or_public_run_only"})
}

/// Build only the run fields used by the upstream search wrapper, without a real game seed.
fn run_context(state: &CombatState, context: &Value) -> RunManager {
    let character = state.player.character.unwrap_or(CharacterId::Ironclad);
    let mut run = RunManager::new(0, character);
    run.run_state.act_index = context["act_index"].as_u64().unwrap_or(1).clamp(1, 255) as u8;
    run.run_state.ascension_level = state.ascension;
    run.run_state.total_floor = context["floor"].as_i64().unwrap_or(0) as i32;
    run.run_state.current_room = Some(Room::new(match context["room_type"].as_str() {
        Some("boss") => RoomType::Boss,
        Some("elite") => RoomType::Elite,
        _ => RoomType::Monster,
    }));
    run
}

/// Resolve the rollout's actual first-use environment override exactly as upstream caches it.
fn rollout_identity(state: &CombatState) -> Result<Value, String> {
    static OVERRIDE: OnceLock<Option<Result<Value, String>>> = OnceLock::new();
    let custom = OVERRIDE.get_or_init(|| {
        std::env::var("STS2_ROLLOUT_NET").ok().map(|path| {
            let bytes = std::fs::read(&path).map_err(|error| format!("rollout_asset:{error}"))?;
            sts2_mcts::rollout_net::RolloutNet::from_json(
                std::str::from_utf8(&bytes).map_err(|error| error.to_string())?,
            )?;
            let mut identity = asset_identity(&bytes, &path)?;
            identity["source"] = json!("upstream_environment_override_cached_first_use");
            Ok(identity)
        })
    });
    let mut identity = match custom {
        Some(result) => result.clone()?,
        None => match rollout_bytes(state.player.character) {
            Some(bytes) => {
                embedded_identity(bytes, &format!("rollout:{:?}", state.player.character))?
            }
            None => {
                return Ok(
                    json!({"active":false,"kind":"heuristic","reason":"no_upstream_character_rollout"}),
                )
            }
        },
    };
    let net = sts2_mcts::rollout_net::net_for(state).ok_or("upstream_rollout_unavailable")?;
    identity["feature_spec"] = json!({"version":net.feature_spec().version.number(),
        "state_dim":net.feature_spec().version.state_dim(),"action_dim":net.feature_spec().version.action_dim()});
    identity["active"] = json!(true);
    identity["kind"] = json!("upstream_trained_rollout");
    Ok(identity)
}

/// Describe the actual prior source selected by the original upstream character/room gates.
fn prior_identity(active: bool) -> Result<Value, String> {
    if !active {
        return Ok(
            json!({"active":false,"kind":"uniform","reason":"disabled_or_outside_upstream_gate"}),
        );
    }
    let value_prior = std::env::var("STS2_RUNAI_COMBAT_PRIOR").ok().as_deref() == Some("1");
    let custom = if value_prior {
        std::env::var("STS2_RUNAI_COMBAT_NET").ok()
    } else {
        std::env::var("STS2_RUNAI_PRIOR_NET")
            .ok()
            .filter(|path| path != "off")
    };
    let mut identity = if let Some(path) = custom {
        asset_identity(
            &std::fs::read(&path).map_err(|error| format!("prior_asset:{error}"))?,
            &path,
        )?
    } else {
        embedded_identity(
            sts2_mcts::rollout_net::builtin_prior_json(CharacterId::Ironclad)
                .ok_or("upstream_ironclad_prior_missing")?
                .as_bytes(),
            "prior:Ironclad",
        )?
    };
    identity["active"] = json!(true);
    identity["kind"] = json!(if value_prior {
        "upstream_value_policy_head"
    } else {
        "upstream_move_prior"
    });
    identity["mix"] = if value_prior {
        Value::Null
    } else {
        json!(std::env::var("STS2_RUNAI_PRIOR_MIX")
            .ok()
            .and_then(|value| value.parse::<f32>().ok())
            .unwrap_or(0.5))
    };
    identity["temperature"] = if value_prior {
        Value::Null
    } else {
        json!(std::env::var("STS2_RUNAI_PRIOR_TEMP")
            .ok()
            .and_then(|value| value.parse::<f32>().ok())
            .unwrap_or(1.0))
    };
    Ok(identity)
}

/// Prepare actual upstream components, then apply independent user switches and host correctness fixes.
pub fn prepare(request: &Value, state: &CombatState, base: MctsConfig) -> Result<Setup, String> {
    let context = context(request, state);
    // Optional OC tables/critics need a complete deck/run context, not a synthetic empty run.
    if std::env::var_os("STS2_RUNAI_POTION_OC").is_some() {
        return Err("potion_opportunity_cost_override_requires_complete_run_context".into());
    }
    for variable in ["STS2_RUNAI_DRINK_JITTER", "STS2_RUNAI_POTION_ABLATION"] {
        if std::env::var_os(variable).is_some() {
            return Err(format!("full_run_only_experimental_override:{variable}"));
        }
    }
    let run = run_context(state, &context);
    let mut budget = CombatBudget::eval_cheap();
    budget.sims = base.max_simulations;
    budget.boss_sims = base.max_simulations;
    let boss = context["room_type"] == "boss";
    let upstream = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut policy = MctsCombatPolicy::new(HeuristicPolicy::new(), budget, 0);
        policy.pick_search_setup(&run, state, boss, base.max_simulations, base.seed)
    }))
    .map_err(|reason| {
        format!(
            "upstream_network_setup:{}",
            reason
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| reason.downcast_ref::<&str>().copied())
                .unwrap_or("panic")
        )
    })?;
    apply_controls(request, state, base, upstream, context)
}

/// Execute the original combat wrapper, retaining complete tree statistics and public-only context.
pub fn serve(
    request: &Value,
    state: &CombatState,
    mut setup: Setup,
    deadline: Instant,
    batch_size: u32,
) -> Result<(sts2_runai::policy::CombatSearchDecision, Value), String> {
    let counter = request.get("decision_counter").map_or(Ok(0), |value| {
        value
            .as_u64()
            .filter(|counter| *counter < u64::MAX)
            .ok_or("invalid_decision_counter")
    })?;
    let policy_seed = request.get("benchmark_run_seed").map_or(Ok(0), |value| {
        value
            .as_i64()
            .and_then(|seed| i32::try_from(seed).ok())
            .ok_or("invalid_benchmark_run_seed")
    })?;
    let override_seed = if request.get("scenario_seed").is_some() {
        Some(setup.config.seed)
    } else {
        None
    };
    let actual_seed = override_seed
        .unwrap_or_else(|| sts2_runai::policy::combat_search_seed(policy_seed, counter));
    setup.config.seed = actual_seed;
    let mut budget = CombatBudget::eval_cheap();
    budget.sims = setup.config.max_simulations;
    budget.boss_sims = setup.config.max_simulations;
    let run = run_context(state, &context(request, state));
    let config = setup.config;
    let leaf = setup.leaf;
    let prior = setup.prior;
    let mut policy = MctsCombatPolicy::new(HeuristicPolicy::new(), budget, policy_seed)
        .with_search_transforms(
            move |_, _, _| (config, leaf.clone(), prior.clone()),
            crate::mcts::correct_execution,
        );
    let decision = policy.search_combat(
        &run,
        state,
        sts2_runai::policy::CombatSearchExecution {
            counter,
            seed_override: override_seed,
            deadline: Some(deadline),
            batch_size,
            search_forced_roots: request["search_forced_roots"].as_bool().unwrap_or(false),
        },
    )?;
    let seed_kind = if override_seed.is_some() {
        "explicit_scenario_override"
    } else if request.get("benchmark_run_seed").is_some() {
        "explicit_offline_run_seed_and_wrapper_counter"
    } else {
        "public_hypothetical_policy_seed_and_wrapper_counter"
    };
    let filter=policy.last_root_filter().map(|filter|json!({"applied":filter.applied,"fallback":filter.fallback,
        "legal_n":filter.legal_n,"plans_n":filter.plans_n,"kept_n":filter.kept_n,"allowed_n":filter.allowed_n,
        "chosen_via":filter.chosen_via}));
    setup.identity["decision_identity"] = json!({"counter":decision.counter,"next_counter":decision.counter+1,
        "counter_source":request["decision_counter_source"].as_str().unwrap_or(
            if request.get("decision_counter").is_some(){"explicit_public_episode_assignment"}else{"default_zero_counter_not_observed"}),
        "search_seed":decision.search_seed,"seed_kind":seed_kind,"private_game_seed_read":false,
        "wrapper_api":"MctsCombatPolicy::search_combat->mcts_pick",
        "simulations_requested":if decision.forced {0}else{config.max_simulations},"simulations_completed":decision.simulations,
        "forced_public_action":decision.forced,"forced_root_searched":!decision.forced&&sts2_core::legal_actions(state).len()==1,
        "extra_statistics_work":request["search_forced_roots"]==true&&sts2_core::legal_actions(state).len()==1,
        "root_guards":decision.tree.config.root_guards,"root_filter":filter,
        "potion_timing":std::env::var("STS2_RUNAI_POTION_TIMING").ok()});
    setup.identity["seed_identity"]["kind"] = json!(seed_kind);
    setup.identity["seed_identity"]["search_seed"] = json!(decision.search_seed);
    setup.identity["seed_identity"]["upstream_fullrun_seed_alignment"] =
        json!(if request.get("benchmark_run_seed").is_some() {
            "explicit_offline_seed_counter_alignment"
        } else {
            "same_wrapper_formula_with_public_hypothetical_seed_not_private_game_seed"
        });
    setup.identity["resolved_config"] = json!(format!("{:?}", decision.tree.config));
    setup
        .identity
        .as_object_mut()
        .unwrap()
        .remove("identity_sha256");
    setup.identity["identity_sha256"] = json!(sha(serde_json::to_string(&setup.identity)
        .unwrap()
        .as_bytes()));
    Ok((decision, setup.identity))
}

/// Adapt components inside the real full-run wrapper, keeping its seed counter and run context.
pub fn transform_upstream(
    request: &Value,
    run: &RunManager,
    state: &CombatState,
    upstream: sts2_runai::policy::CombatSearchSetup,
) -> Result<Setup, String> {
    let mut base = crate::mcts::config(request, state, upstream.0.seed)?;
    base.max_simulations = upstream.0.max_simulations;
    let context = json!({"room_type":match run.run_state.current_room.as_ref().map(|room|room.room_type) {
        Some(RoomType::Boss)=>"boss",Some(RoomType::Elite)=>"elite",_=>"common"},
        "act_index":run.run_state.act_index,"is_last_act":run.run_state.is_last_act(),
        "awaiting_second_boss":run.run_state.awaiting_second_boss(),"ascension":run.run_state.ascension_level,
        "floor":run.run_state.total_floor,"context_source":"explicit_offline_full_run_reference"});
    apply_controls(request, state, base, upstream, context)
}

/// Apply independent controls to genuine upstream components without replacing the wrapper.
fn apply_controls(
    request: &Value,
    state: &CombatState,
    base: MctsConfig,
    upstream: sts2_runai::policy::CombatSearchSetup,
    context: Value,
) -> Result<Setup, String> {
    let rollout = option(
        request,
        "rollout_policy",
        "upstream",
        &["upstream", "heuristic"],
    )?;
    let action_prior = option(request, "action_prior", "upstream", &["upstream", "off"])?;
    let value = option(request, "value_model", "texel", &["texel", "learned"])?;
    let profile = option(request, "search_profile", "upstream", &["upstream", "base"])?;
    let (upstream, _, upstream_prior) = upstream;
    let mut config = if profile == "upstream" {
        upstream
    } else {
        base
    };
    config.hidden_info = base.hidden_info;
    config.max_simulations = base.max_simulations;
    config.max_rollout_depth = base.max_rollout_depth;
    config.tt_hash_v2 = true;
    if config.hidden_info == sts2_mcts::HiddenInfoMode::Clairvoyant {
        // Exact private-RNG paths cannot merge cursor-blind keys or reseed extra leaf worlds.
        config.use_transposition_table = false;
        config.leaf_playouts = 1;
    }
    let mut warnings = Vec::<String>::new();
    let explicit_carry = std::env::var("STS2_RUNAI_MCTS_OVERRIDES")
        .ok()
        .is_some_and(|bundle| {
            bundle.split(',').any(|entry| {
                entry.split_once('=').is_some_and(|(key, _)| {
                    matches!(key.trim(), "win_hp_carry" | "win_potion_carry")
                })
            })
        });
    if profile == "upstream"
        && context["room_type"] == "boss"
        && !explicit_carry
        && context["context_source"] != "explicit_offline_full_run_reference"
    {
        let carry = match (
            context["is_last_act"].as_bool(),
            context["awaiting_second_boss"].as_bool(),
        ) {
            (Some(last), Some(second)) => {
                let raw_mode = std::env::var("STS2_RUNAI_BOSS_CARRY").unwrap_or_default();
                let mode = raw_mode.trim();
                if mode == "0" {
                    None
                } else if last || mode == "1" {
                    sts2_mcts::boss_win_carry(true, last, second, state.ascension)
                } else {
                    None
                }
            }
            _ => {
                warnings
                    .push("final_act_or_second_boss_not_observed_boss_carry_not_assumed".into());
                None
            }
        };
        config.win_hp_carry = carry.map(|(hp, _)| hp);
        config.win_potion_carry = carry.map(|(_, potion)| potion);
    }
    config.rollout_policy = if rollout == "upstream" {
        RolloutPolicyKind::Net
    } else {
        RolloutPolicyKind::Heuristic
    };
    let prior = if action_prior == "upstream" && upstream.use_puct_prior {
        config.use_puct_prior = true;
        upstream_prior
    } else {
        config.use_puct_prior = false;
        SharedPolicy::uniform()
    };
    let (leaf, value_identity) = if value == "learned" {
        let net = value_net()?;
        let mut identity = embedded_identity(VALUE_BYTES, "value:h16:legacy88")?;
        identity["active"] = json!(true);
        identity["kind"] = json!("LearnedValueNet");
        identity["native_input_dim"] = json!(net.input_dim());
        identity["compatibility"] =
            json!("explicit_legacy88_current_append_only_prefix_not_current_champion");
        (LeafEval::Learned(net), identity)
    } else {
        (
            LeafEval::TexelWide,
            json!({"active":false,"kind":"TexelWide","feature_dim":sts2_core::WIDE_FEATURE_DIM,
        "fallback":"original_upstream_default","reason":"user_selected_texel"}),
        )
    };
    let mut identity = json!({"requested":{"rollout_policy":rollout,"action_prior":action_prior,
        "value_model":value,"search_profile":profile},"components":{
            "rollout":if rollout=="upstream" {rollout_identity(state)?}else{json!({"active":false,"kind":"Heuristic","fallback":"original_upstream_heuristic","reason":"user_disabled_network"})},
            "action_prior":prior_identity(config.use_puct_prior)?,"value_model":value_identity,
            "search_profile":{"kind":profile,"upstream_api":"MctsCombatPolicy::pick_search_setup",
                "ironclad_bundle":sts2_runai::policy::IRONCLAD_SEARCH_KNOBS,
                "removed_counts_forms":config.removed_counts_forms,"draw_widening_c":config.draw_widening_c,
                "endturn_widening_c":config.endturn_widening_c,"endturn_widening_alpha":config.endturn_widening_alpha,
                "root_guards":config.root_guards,"win_hp_carry":config.win_hp_carry,"win_potion_carry":config.win_potion_carry,
                "potion_opportunity_cost":config.potion_charge_q,"root_drink_mask":config.root_drink_mask,
                "root_filter":std::env::var("STS2_RUNAI_TURNPLAN_ROOT_FILTER").unwrap_or_else(|_|"upstream_default_off".into()),
                "potion_timing":std::env::var("STS2_RUNAI_POTION_TIMING").ok(),
                "terminal_eval":format!("{:?}",config.terminal_eval_mode)}},
        "context":context,"warnings":warnings,"feature_identity":feature_identity(),
        "source_hashes":{"mcts":sts2_mcts::SRC_HASH,"runai":sts2_runai::SRC_HASH},
        "seed_identity":{"kind":"hypothetical_combat_search_seed","search_seed":base.seed,
            "private_run_seed_read":false,"upstream_fullrun_seed_alignment":"requires_explicit_benchmark_seed"},
        "execution_corrections":{"terminal_rollouts":true,"exact_rng":base.hidden_info==sts2_mcts::HiddenInfoMode::Clairvoyant,
            "endturn_prefix_cache":crate::mcts::endturn_prefix_cache_enabled(state),"tt_hash_v2":true,
            "transposition_table_active":config.use_transposition_table,"leaf_playouts":config.leaf_playouts,
            "difference_from_pristine_wrapper":"actual_terminal_observations_and_explicit_rng_quote_semantics"},
        "resolved_config":format!("{config:?}")});
    if context["context_source"] == "explicit_offline_full_run_reference" {
        identity["seed_identity"]["kind"] = json!("original_full_run_wrapper_decision_seed");
        identity["seed_identity"]["upstream_fullrun_seed_alignment"] =
            json!("original_wrapper_seed_counter_retained");
    }
    if action_prior == "off" {
        identity["components"]["action_prior"]["reason"] = json!("user_disabled_network");
    }
    identity["components"]["action_prior"]["gate"] = json!({"character":format!("{:?}",state.player.character),
        "room_type":context["room_type"],"builtin_scope":"Ironclad_elite_or_boss","disabled_fallback":"uniform"});
    identity["identity_sha256"] = json!(sha(serde_json::to_string(&identity).unwrap().as_bytes()));
    if request["network_diagnostics"].as_bool() == Some(true) {
        let legal = sts2_core::legal_actions(state);
        identity["diagnostics"] = json!({"wide_features":sts2_core::eval_features_wide(state).to_vec(),
            "root_leaf_value":leaf.evaluate(state),"root_prior":prior.prior(state,&legal).to_vec()});
        if rollout == "upstream" {
            if let Some(net) = sts2_mcts::rollout_net::net_for(state) {
                let explanation = net.explain(state);
                identity["diagnostics"]["rollout"] = json!({"state_features":explanation.state,
                    "monster_rows":explanation.monsters,"actions":explanation.actions.iter().map(|(action,row,features,logit)|
                        json!({"action":format!("{action:?}"),"row":row,"features":features,"logit":logit})).collect::<Vec<_>>()});
            }
        }
    }
    Ok(Setup {
        config,
        leaf,
        prior,
        identity,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use sts2_core::full_run::RunPolicy;
    use sts2_core::run_manager::Phase;
    use sts2_runai::policy::DecisionTelemetry;

    /// Reach an actual upstream opening combat instead of mocking a policy input.
    fn opening_run() -> RunManager {
        sts2_runai::rollout::ensure_content_installed();
        let mut run = sts2_content::run_callbacks::new_run_manager_for_character_with_ascension(
            3,
            CharacterId::Ironclad,
            0,
        )
        .with_neow_start();
        sts2_content::run_callbacks::initialize_run_setup(&mut run);
        for _ in 0..64 {
            if run.phase() == Phase::Combat {
                return run;
            }
            let actions = run.get_available_actions();
            let _ = run.take_action(actions[0]);
        }
        panic!("opening combat not reached");
    }

    /// Every independent switch changes its component while preserving the original gates.
    #[test]
    fn independent_components_and_character_room_gates() {
        let mut run = opening_run();
        let state = run.combat.as_ref().unwrap().clone();
        run.run_state.current_room = Some(Room::new(RoomType::Elite));
        let mut wrapper =
            MctsCombatPolicy::new(HeuristicPolicy::new(), CombatBudget::eval_cheap(), 3);
        for rollout in ["upstream", "heuristic"] {
            for prior in ["upstream", "off"] {
                for value in ["texel", "learned"] {
                    for profile in ["upstream", "base"] {
                        let request = json!({"rollout_policy":rollout,"action_prior":prior,"value_model":value,
                            "search_profile":profile,"simulations":32});
                        let original = wrapper.pick_search_setup(&run, &state, false, 32, 41);
                        let setup = transform_upstream(&request, &run, &state, original).unwrap();
                        assert_eq!(setup.config.use_puct_prior, prior == "upstream");
                        assert_eq!(setup.leaf.is_learned(), value == "learned");
                        assert_eq!(setup.config.removed_counts_forms, profile == "upstream");
                        assert_eq!(
                            matches!(setup.config.rollout_policy, RolloutPolicyKind::Net),
                            rollout == "upstream"
                        );
                        assert_eq!(
                            setup.identity["components"]["action_prior"]["active"],
                            json!(prior == "upstream")
                        );
                    }
                }
            }
        }
        for character in [
            CharacterId::Ironclad,
            CharacterId::Silent,
            CharacterId::Defect,
            CharacterId::Regent,
            CharacterId::Necrobinder,
        ] {
            let mut state = state.clone();
            state.player.character = Some(character);
            let original = wrapper.pick_search_setup(&run, &state, false, 32, 41);
            let setup = transform_upstream(&json!({}), &run, &state, original).unwrap();
            assert_eq!(
                setup.config.use_puct_prior,
                character == CharacterId::Ironclad
            );
            assert!(rollout_identity(&state).unwrap()["sha256"]
                .as_str()
                .is_some());
        }
        run.run_state.current_room = Some(Room::new(RoomType::Monster));
        let mut ordered_state = state.clone();
        ordered_state.player.pinned_top.push(ordered_state.cards[0].uid);
        let original = wrapper.pick_search_setup(&run, &ordered_state, false, 32, 41);
        let setup = transform_upstream(&json!({}), &run, &ordered_state, original).unwrap();
        assert!(!crate::mcts::endturn_prefix_cache_enabled(&ordered_state));
        assert_eq!(setup.identity["execution_corrections"]["endturn_prefix_cache"], json!(false));
        assert!(
            !wrapper
                .pick_search_setup(&run, &state, false, 32, 41)
                .0
                .use_puct_prior
        );
    }

    /// An absent hook and an identity hook produce exactly the same original search statistics.
    #[test]
    fn absent_and_identity_wrapper_hooks_match_exactly() {
        let run = opening_run();
        let state = run.combat.as_ref().unwrap();
        let mut original =
            MctsCombatPolicy::new(HeuristicPolicy::new(), CombatBudget::eval_cheap(), 3);
        let mut hooked =
            MctsCombatPolicy::new(HeuristicPolicy::new(), CombatBudget::eval_cheap(), 3)
                .with_search_transforms(|_, _, setup| setup, |tree, _| tree);
        let mut left = original.build_search(&run, state, 32, 41);
        let mut right = hooked.build_search(&run, state, 32, 41);
        assert_eq!(format!("{:?}", left.config), format!("{:?}", right.config));
        assert_eq!(
            left.try_search_n(32).unwrap(),
            right.try_search_n(32).unwrap()
        );
        assert_eq!(
            format!("{:?}", left.root_outcomes()),
            format!("{:?}", right.root_outcomes())
        );
    }

    /// Production uses the actual wrapper's counter, seed formula and final root action.
    #[test]
    fn serving_matches_native_wrapper_counter_seed_and_statistics() {
        let run = opening_run();
        let state = run.combat.as_ref().unwrap();
        let controls = json!({"simulations":32,"network_context":{"room_type":"common","act_index":1,
            "is_last_act":false,"awaiting_second_boss":false,"floor":run.run_state.total_floor}});
        let transformed = controls.clone();
        let mut budget = CombatBudget::eval_cheap();
        budget.sims = 32;
        budget.boss_sims = 32;
        let mut native = MctsCombatPolicy::new(HeuristicPolicy::new(), budget, 3)
            .with_search_transforms(
                move |rm, state, original| {
                    let setup = transform_upstream(&transformed, rm, state, original).unwrap();
                    (setup.config, setup.leaf, setup.prior)
                },
                crate::mcts::correct_execution,
            );
        let actions = run.get_available_actions();
        for counter in 0..3 {
            assert!(native.choose(&run, &actions).is_some());
            let original = native.combat_search().unwrap();
            let mut request = controls.clone();
            request["decision_counter"] = json!(counter);
            request["benchmark_run_seed"] = json!(3);
            let cfg = crate::mcts::config(&request, state, 0).unwrap();
            let (tree, action, count, identity) = crate::mcts::run_with_request(
                state,
                cfg,
                Instant::now() + std::time::Duration::from_secs(30),
                &request,
            )
            .unwrap();
            assert_eq!(action, original.chosen);
            assert_eq!(count, 32);
            assert_eq!(
                format!("{:?}", tree.root_outcomes()),
                format!("{:?}", original.outcomes)
            );
            assert_eq!(
                identity["decision_identity"]["search_seed"],
                json!(sts2_runai::policy::combat_search_seed(3, counter))
            );
        }
    }

    /// The wrapper's post-search potion override reaches production rather than stopping at tree Q.
    #[test]
    fn serving_reuses_upstream_potion_timing_postprocessor() {
        // The upstream seam is process-wide; isolate it from parallel default-policy tests.
        if std::env::var("STS2_RUNAI_POTION_TIMING").as_deref() != Ok("defer:1") {
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "networks::tests::serving_reuses_upstream_potion_timing_postprocessor", "--nocapture"])
                .env("STS2_RUNAI_POTION_TIMING", "defer:1")
                .status()
                .unwrap();
            assert!(status.success(), "isolated upstream potion-timing test failed");
            return;
        }
        let run = opening_run();
        let mut state = run.combat.as_ref().unwrap().clone();
        state.player.energy = 0;
        state.player.hand.clear();
        state.player.draw.clear();
        state.player.discard.clear();
        state
            .player
            .potions
            .push(sts2_core::potions::PotionKind::FirePotion);
        state
            .creatures
            .iter_mut()
            .find(|creature| creature.id == state.enemy_uids[0].0)
            .unwrap()
            .current_hp = 20;
        let request = json!({"simulations":64,"scenario_seed":41,"network_context":{"floor":1}});
        let cfg = crate::mcts::config(&request, &state, 41).unwrap();
        let setup = prepare(&request, &state, cfg).unwrap();
        let mut bare = crate::mcts::correct_execution(
            sts2_mcts::Mcts::with_policy(state.clone(), setup.config, setup.prior)
                .with_leaf_eval(setup.leaf),
            &state,
        );
        assert!(matches!(
            bare.try_search_n(64).unwrap(),
            sts2_core::Action::DrinkPotion { .. }
        ));
        let (_, chosen, _, identity) = crate::mcts::run_with_request(
            &state,
            cfg,
            Instant::now() + std::time::Duration::from_secs(30),
            &request,
        )
        .unwrap();
        assert_eq!(chosen, sts2_core::Action::EndTurn);
        assert_eq!(
            identity["decision_identity"]["potion_timing"],
            json!("defer:1")
        );
    }

    /// Forced decisions consume the wrapper counter, while extra terminal statistics are explicit.
    #[test]
    fn serving_forced_root_preserves_native_zero_work() {
        let run = opening_run();
        let mut state = run.combat.as_ref().unwrap().clone();
        state.player.energy = 0;
        state.player.hand.clear();
        state.player.potions.clear();
        assert_eq!(
            sts2_core::legal_actions(&state).as_slice(),
            &[sts2_core::Action::EndTurn]
        );
        let mut request = json!({"simulations":32,"benchmark_run_seed":3,"decision_counter":7});
        let cfg = crate::mcts::config(&request, &state, 0).unwrap();
        let (tree, action, count, identity) = crate::mcts::run_with_request(
            &state,
            cfg,
            Instant::now() + std::time::Duration::from_secs(30),
            &request,
        )
        .unwrap();
        assert_eq!(action, sts2_core::Action::EndTurn);
        assert_eq!(count, 0);
        assert!(tree.root_outcomes().is_empty());
        assert_eq!(identity["decision_identity"]["next_counter"], json!(8));
        assert_eq!(
            identity["decision_identity"]["forced_public_action"],
            json!(true)
        );
        request["search_forced_roots"] = json!(true);
        let (tree, _, count, identity) = crate::mcts::run_with_request(
            &state,
            cfg,
            Instant::now() + std::time::Duration::from_secs(30),
            &request,
        )
        .unwrap();
        assert_eq!(count, 32);
        assert!(!tree.root_outcomes().is_empty());
        assert_eq!(
            identity["decision_identity"]["extra_statistics_work"],
            json!(true)
        );
    }
}

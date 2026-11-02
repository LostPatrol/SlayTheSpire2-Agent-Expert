//! Upstream combat MCTS, shared by generated candidates and fixed-plan continuations.
//! Search values, terminal counts and depth cutoffs retain their separate meanings.
use crate::public::PublicPosition;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::time::{Duration, Instant};
use sts2_core::{Action, CombatState, StepResult};
use sts2_mcts::{HiddenInfoMode, Mcts, MctsConfig, RootOutcome, SharedPolicy};

/// Runtime-dispatched original upstream move priors share the same tree implementation.
pub type SearchTree = Mcts<SharedPolicy>;

// Match the deployed eval-cheap lane rather than the legacy library depth of ten.
const DEFAULT_SIMS: u32 = 1_000;
// Boss combat receives the upstream eval-cheap fourfold simulation budget.
const DEFAULT_BOSS_SIMS: u32 = 4_000;
// Keep long combat continuations at every strength setting.
const DEFAULT_DEPTH: u32 = 200;
// Check the soft deadline between small batches without changing the search algorithm.
const SIM_BATCH: u32 = 32;
// Broader fixed plans are independent of the retired 4 x 12 x 8 evaluator limits.
const MAX_PLANS: usize = 64;
// Prevent malformed unbounded input while permitting realistic multi-turn plans.
const MAX_PREFIX_ACTIONS: usize = 64;
// Independent prefix worlds share their seeds across all declared candidates.
const WORLD_STRIDE: u64 = 104_729;

/// Advertise the pinned upstream serving defaults and the meaning of terminal statistics.
pub fn defaults() -> Value {
    json!({"implementation":"sts2-mcts", "simulations":DEFAULT_SIMS,
        "boss_simulations":DEFAULT_BOSS_SIMS,"max_rollout_depth":DEFAULT_DEPTH,
        "public_hidden_info":"BeliefDraw", "dialect":"upstream_deployed_belief_draw",
        "terminal_eval":"RichBoth", "cutoff_leaf_eval":"TexelWide", "rollout_policy":"upstream",
        "action_prior":"upstream","value_model":"texel","search_profile":"upstream",
        "terminal_rollouts":true,"transposition_hash":"v2_includes_player_powers_and_potion_belt",
        "win_probability":"search: wins/visits; fixed prefixes: equal-weight conditional world fractions; unresolved retained"})
}

/// Read bounded integer options without coercing floats, strings, or negative values.
fn integer(request: &Value, key: &str, default: u64, maximum: u64) -> Result<u64, String> {
    request.get(key).map_or(Ok(default), |value| value.as_u64()
        .filter(|value| *value > 0 && *value <= maximum)
        .ok_or_else(|| format!("{key}_out_of_range")))
}

/// Carry witnessed public history alongside the explicitly selected observation contract.
fn source(request: &Value) -> Result<Value, String> {
    let mut source = request.get("public_state").filter(|value| value.is_object())
        .cloned().ok_or("missing_public_state")?;
    if let Some(history) = request.get("public_history").filter(|value| value.is_object()) {
        source["public_history"] = history.clone();
    }
    Ok(source)
}

/// Import one hypothetical world with the requested reading permission.
fn position(request: &Value, source: &Value, seed: u64) -> Result<PublicPosition, Vec<String>> {
    let internal = match request.get("read_access").and_then(Value::as_str).unwrap_or("public") {
        "public" => None,
        "internal" => Some(request.get("internal_state")
            .filter(|value| value.is_object()).ok_or_else(|| vec!["internal_state_unavailable".into()])?),
        _ => return Err(vec!["invalid_read_access".into()]),
    };
    let position = PublicPosition::for_search(source, seed, internal)?;
    let support = position.search_support();
    let blockers: Vec<String> = support["blocking_reasons"].as_array().into_iter().flatten()
        .filter_map(Value::as_str).map(str::to_owned).collect();
    if blockers.is_empty() { Ok(position) } else { Err(blockers) }
}

/// Preserve unsupported imports as explicit failures, including an empty recommendation.
fn unsupported(reasons: Vec<String>) -> Value {
    json!({"ok":false,"skipped":true,"reason":"search_state_not_supported","best":null,
        "alternatives":[],"support":{"level":"unsupported","reasons":reasons,
        "blocking_reasons":reasons},"errors":["search_state_not_supported"]})
}

/// Detect boss occupants from the pinned encounter registries without reading the run's future boss.
pub(crate) fn boss_combat(state: &CombatState) -> bool {
    use sts2_content::encounters::{act1_overgrowth,act1_underdocks,act2_hive,act3_glory,EncounterTier};
    let boss_members = act1_overgrowth::ACT1_ENCOUNTERS.iter()
        .chain(act1_underdocks::ACT1_UNDERDOCKS_ENCOUNTERS)
        .chain(act2_hive::ACT2_ENCOUNTERS).chain(act3_glory::ACT3_ENCOUNTERS)
        .filter(|encounter|encounter.tier==EncounterTier::Boss)
        .flat_map(|encounter|encounter.monsters.iter().map(|(kind,_)|*kind)).collect::<Vec<_>>();
    state.enemy_uids.iter().any(|uid|state.creatures.iter().find(|creature|creature.id==uid.0)
        .and_then(|creature|creature.species).is_some_and(|kind|boss_members.contains(&kind)
            || matches!(kind,sts2_core::MonsterKind::TheAdversaryMkOne|sts2_core::MonsterKind::TheAdversaryMkTwo
                |sts2_core::MonsterKind::TheAdversaryMkThree)))
}

/// Build the same pure MCTS dialect used by the upstream run policy.
pub fn config(request: &Value, state: &CombatState, seed: u64) -> Result<MctsConfig, String> {
    let boss = request["public_state"]["run"]["room_type"].as_str()
        .is_some_and(|kind| kind.eq_ignore_ascii_case("boss"))
        || request.get("is_boss").and_then(Value::as_bool).unwrap_or(false) || state.is_boss || boss_combat(state);
    let lane_key=if boss {"boss_simulations"}else{"normal_simulations"};
    let simulation_key = if request.get(lane_key).is_some() {lane_key}
        else if request.get("simulations").is_some() {"simulations"} else {"max_simulations"};
    let mut cfg = MctsConfig {
        max_simulations: integer(request, simulation_key, if boss {DEFAULT_BOSS_SIMS} else {DEFAULT_SIMS} as u64, 1_000_000)? as u32,
        max_rollout_depth: integer(request, "max_rollout_depth", DEFAULT_DEPTH as u64, 10_000)? as u32,
        seed,
        hidden_info: if request["read_access"] == "internal" { HiddenInfoMode::Clairvoyant } else { HiddenInfoMode::BeliefDraw },
        // The legacy TT key aliases actual potion belts and creature-backed player powers.
        tt_hash_v2: true,
        ..MctsConfig::default()
    };
    // Keep both reading-permission arms on the deployed policy and exploration dialect.
    cfg.apply_belief_draw_dialect();
    Ok(cfg)
}

/// Count only actual resolved terminals; cutoffs never become victories by rescaling Q.
fn outcome(outcome: &RootOutcome) -> Value {
    let denominator = f64::from(outcome.visits.max(1));
    json!({"visits":outcome.visits,"wins":outcome.wins,"losses":outcome.losses,
        "cutoffs":outcome.ties,"ties":outcome.ties,"p_win":f64::from(outcome.wins)/denominator,
        "p_loss":f64::from(outcome.losses)/denominator,"p_unresolved":f64::from(outcome.ties)/denominator,
        "mean":outcome.mean_value,"mean_value":outcome.mean_value,"value_sum":outcome.value_sum,
        "value_sq_sum":outcome.value_sq_sum,"value_variance":outcome.value_variance(),
        "mean_stderr":outcome.mean_stderr(),"mean_final_hp":outcome.mean_final_hp(),
        "final_hp_sd":outcome.final_hp_sd(),"hp_sum":outcome.hp_sum,"hp_sq_sum":outcome.hp_sq_sum,
        "resolved_hp_samples":outcome.hp_n,"estimates_complete":outcome.visits>0&&outcome.ties==0,
        "probability_meaning":"empirical_terminal_mass_per_adaptive_visit_unresolved_mass_is_censored_not_a_loss_or_calibrated_policy_probability",
        "final_hp_meaning":"conditional_on_actually_resolved_terminal_samples_excludes_all_unresolved_cutoffs",
        "meaning":"adaptive_search_rollouts_not_calibrated_policy_win_rate"})
}

/// Aggregate additive root counters without averaging per-action means or excluding cutoffs.
fn totals(outcomes: &[RootOutcome]) -> Value {
    let mut total = RootOutcome { action:Action::EndTurn, visits:0, wins:0, losses:0, ties:0,
        value_sum:0.0, value_sq_sum:0.0, hp_sum:0.0, hp_sq_sum:0.0, hp_n:0, mean_value:0.0 };
    for row in outcomes {
        total.visits += row.visits; total.wins += row.wins; total.losses += row.losses; total.ties += row.ties;
        total.value_sum += row.value_sum; total.value_sq_sum += row.value_sq_sum;
        total.hp_sum += row.hp_sum; total.hp_sq_sum += row.hp_sq_sum; total.hp_n += row.hp_n;
    }
    total.mean_value = total.value_sum / total.visits.max(1) as f32;
    outcome(&total)
}

/// Preserve actual rollout work and distinguish finite-belief cuts from depth caps.
fn search_statistics(tree: &SearchTree, rows: &[RootOutcome]) -> Value {
    let mut statistics = totals(rows);
    statistics["rollouts_total"] = json!(tree.rollout_stats().0);
    statistics["depth_cap_hits"] = json!(tree.rollout_stats().1);
    statistics["belief_cut_reasons"] = json!(tree.belief_cut_stats());
    statistics["belief_unsupported_cuts"] = json!(tree.belief_cut_stats().values().sum::<u64>());
    statistics["belief_cut_meaning"] = json!("nonterminal_order_limit_not_native_terminal_or_depth_cap");
    statistics["belief_cut_value"] = json!("preaction_leaf_value_or_explicit_neutral_zero_if_snapshot_unavailable");
    statistics
}

/// Sum execution diagnostics without changing the declared equal-world comparison weights.
fn merge_execution(statistics: &mut Value, reports: &[Value]) {
    let mut reasons = std::collections::BTreeMap::<String,u64>::new();
    for field in ["rollouts_total","depth_cap_hits","belief_unsupported_cuts"] {
        statistics[field] = json!(reports.iter().map(|r|r["mcts"][field].as_u64().unwrap_or(0)).sum::<u64>());
    }
    for report in reports {
        if let Some(rows) = report["mcts"]["belief_cut_reasons"].as_object() {
            for (reason,count) in rows { *reasons.entry(reason.clone()).or_default() += count.as_u64().unwrap_or(0); }
        }
    }
    statistics["belief_cut_reasons"] = json!(reasons);
}

/// Compare predeclared prefix worlds equally, preserving actual visit counts for work auditing.
fn world_statistics(outcomes: &[RootOutcome], reports: &[Value]) -> Value {
    let mut statistics=totals(outcomes);
    merge_execution(&mut statistics,reports);
    for field in ["p_win","p_loss","p_unresolved","mean","mean_value","mean_final_hp",
        "final_hp_sd","value_variance","mean_stderr"] {
        statistics[format!("visit_weighted_{field}")]=statistics[field].clone();
    }
    let count=reports.len().max(1) as f64;
    let average=|field:&str| reports.iter().map(|report|report["mcts"][field].as_f64()
        .unwrap_or(0.0)).sum::<f64>()/count;
    for field in ["p_win","p_loss","p_unresolved","mean","mean_value"] {
        statistics[field]=json!(average(field));
    }
    let resolved_mass=reports.iter().map(|report| {
        let row=&report["mcts"];
        row["resolved_hp_samples"].as_f64().unwrap_or(0.0)/row["visits"].as_f64().unwrap_or(1.0).max(1.0)
    }).sum::<f64>()/count;
    let hp_moment=|field:&str| reports.iter().map(|report| {
        let row=&report["mcts"];
        row[field].as_f64().unwrap_or(0.0)/row["visits"].as_f64().unwrap_or(1.0).max(1.0)
    }).sum::<f64>()/count;
    let mean_hp=(resolved_mass>0.0).then(||hp_moment("hp_sum")/resolved_mass);
    statistics["mean_final_hp"]=json!(mean_hp);
    statistics["final_hp_sd"]=json!(mean_hp.map(|mean|
        (hp_moment("hp_sq_sum")/resolved_mass-mean*mean).max(0.0).sqrt()));
    let mean=average("mean_value");
    let second_moment=hp_moment("value_sq_sum");
    statistics["value_variance"]=json!((second_moment-mean*mean).max(0.0));
    // Within-world adaptive visits are correlated; uncertainty uses independent world means.
    statistics["mean_stderr"]=if reports.len()>1 {
        json!((reports.iter().map(|report| {
            let delta=report["mcts"]["mean_value"].as_f64().unwrap_or(0.0)-mean;
            delta*delta
        }).sum::<f64>()/(count-1.0)/count).sqrt())
    }else{Value::Null};
    statistics["prefix_worlds"]=json!(reports.len());
    statistics["resolved_probability_mass"]=json!(resolved_mass);
    statistics["probability_denominator"]=json!("equal_weight_predeclared_prefix_worlds");
    statistics["counts_meaning"]=json!("wins_losses_visits_and_hp_sums_are_actual_visit_totals");
    statistics["meaning"]=json!("equal_weight_prefix_worlds_with_adaptive_mcts_continuations_not_calibrated_policy_win_rate");
    statistics
}

/// Select original upstream networks and report their actual identities alongside the search.
pub fn run_with_request(state: &CombatState, cfg: MctsConfig, deadline: Instant, request: &Value)
    -> Result<(SearchTree, Action, u32, Value), String> {
    run_prepared(state, crate::networks::prepare(request,state,cfg)?, deadline,request)
}

/// Preserve exact-RNG and terminal-observation fixes across every runtime policy choice.
fn run_prepared(state: &CombatState, setup: crate::networks::Setup, deadline: Instant,request:&Value)
    -> Result<(SearchTree, Action, u32, Value), String> {
    let (decision,identity)=crate::networks::serve(request,state,setup,deadline,SIM_BATCH)?;
    Ok((decision.tree,decision.action,decision.simulations,identity))
}

/// Share explicit production corrections with corrected-reference wrapper trees.
pub fn correct_execution(tree: SearchTree, state: &CombatState) -> SearchTree {
    let exact=tree.config.hidden_info == HiddenInfoMode::Clairvoyant;
    tree.with_exact_rng(exact)
        .with_endturn_prefix_cache(endturn_prefix_cache_enabled(state))
        .with_terminal_rollouts(true)
}

/// Quotes and public draw-order constraints require the complete action wrapper.
pub(crate) fn endturn_prefix_cache_enabled(state: &CombatState) -> bool {
    !has_observed_quotes(state) && !has_draw_order_model(state)
}

/// Root native quotes expire through the core action wrapper, which phase-prefix caching skips.
pub(crate) fn has_observed_quotes(state: &CombatState) -> bool {
    use sts2_core::cards::VarKey;
    state.cards.iter().any(|card|card.combat_var(VarKey::ObservedEnergyCost)!=0
        || card.combat_var(VarKey::ObservedStarCost)!=0)
}

/// Priority-band rollback needs the complete core action rather than a cached phase prefix.
fn has_perfect_fit(state: &CombatState) -> bool {
    state.cards.iter().any(|card|card.has_enchantment(sts2_core::cards::EnchantmentId::PerfectFit))
}

/// Generic certified top/bottom bands remain meaningful even after all PF cards leave draw.
fn has_draw_order_model(state:&CombatState)->bool {
    state.player.draw_knowledge.is_some() || !state.player.pinned_top.is_empty()
        || !state.player.pinned_bottom.is_empty() || has_perfect_fit(state)
}

/// Render one root action with its conditional statistics and immediate successor snapshot.
fn recommendation(root: &PublicPosition, row: &RootOutcome, best: bool, public:bool, seed:u64) -> Value {
    let action = root.encode_search_action(&row.action);
    let mut endpoint = root.clone();
    if public && has_draw_order_model(&endpoint.state) {
        sts2_core::blindfold::belief_root(&mut endpoint.state, seed);
    }
    let result = sts2_mcts::apply_search_action(&mut endpoint.state, row.action.clone());
    let unsupported = endpoint.state.player.deferred_draw_order
        .then(|| sts2_core::draw_knowledge::unsupported_reason(&endpoint.state.player)).flatten();
    let mut evaluation = outcome(row);
    evaluation["outcome"] = json!(if unsupported.is_some() {"unsupported_draw_belief"}
        else if endpoint.state.player.current_hp <= 0 {"confirmed_death"}
        else if endpoint.state.is_over {"combat_victory"} else {"sampled_combat_continuation"});
    json!({"actions":[action],"score":row.mean_value,"evaluation":evaluation,"mcts":outcome(row),
        "state":endpoint.snapshot(),"is_best":best,"termination":format!("{result:?}"),
        "projection_stop_reason":unsupported,
        "score_support":{"meaning":"upstream_terminal_and_selected_cutoff_evaluator"}})
}

/// No search ran on a forced root: counters are zero and estimates remain unknown.
fn unsearched_statistics()->Value {
    json!({"visits":0,"wins":0,"losses":0,"cutoffs":0,"ties":0,"p_win":null,"p_loss":null,
        "p_unresolved":null,"mean":null,"mean_value":null,"value_sum":0.0,"value_sq_sum":0.0,
        "value_variance":null,"mean_stderr":null,"mean_final_hp":null,"final_hp_sd":null,
        "hp_sum":0.0,"hp_sq_sum":0.0,"resolved_hp_samples":0,"meaning":"forced_legal_action_no_search_estimate"})
}

/// Return the sole real action without manufacturing a scored root outcome.
fn forced_recommendation(root:&PublicPosition,action:&Action,public:bool,seed:u64)->Value {
    let mut endpoint=root.clone();
    if public && has_draw_order_model(&endpoint.state) {
        sts2_core::blindfold::belief_root(&mut endpoint.state, seed);
    }
    let result=sts2_mcts::apply_search_action(&mut endpoint.state,action.clone());
    let unsupported=endpoint.state.player.deferred_draw_order
        .then(||sts2_core::draw_knowledge::unsupported_reason(&endpoint.state.player)).flatten();
    let mut evaluation=unsearched_statistics();
    evaluation["outcome"]=json!("forced_public_action");
    json!({"actions":[root.encode_search_action(action)],"score":null,"evaluation":evaluation,
        "mcts":unsearched_statistics(),"state":endpoint.snapshot(),"is_best":true,
        "termination":format!("{result:?}"),"forced_public_action":true,
        "projection_stop_reason":unsupported,
        "score_support":{"meaning":"only_legal_action_no_simulation_value"}})
}

/// Fixed complete multi-selections are atomic inputs; MCTS enumerates their incremental form.
fn prefix_action_is_legal(state: &CombatState, action: &Action) -> bool {
    if sts2_core::legal_actions(state).contains(action) {return true;}
    let Action::ResolveChoice {selection}=action else {return false;};
    let Some(choice)=state.pending_choice.as_ref() else {return false;};
    selection.len()>1 && selection.len()>=choice.min_select as usize
        && selection.len()<=choice.max_select as usize
        && selection.iter().all(|index|(*index as usize)<choice.options.len())
        && selection.iter().collect::<BTreeSet<_>>().len()==selection.len()
}

/// Generate the complete upstream legal-action search, including potions and pending choices.
pub fn search(request: &Value) -> Result<Value, String> {
    let source = source(request)?;
    let seed = request.get("scenario_seed").map_or(Ok(0), |value| value.as_u64().ok_or("invalid_scenario_seed"))?;
    let root = match position(request, &source, seed) {Ok(root)=>root, Err(reasons)=>return Ok(unsupported(reasons))};
    search_position(request, root)
}

/// Search the complete upstream mirror clone, retaining its pending continuations.
pub fn search_position(request: &Value, mut root: PublicPosition) -> Result<Value, String> {
    let started = Instant::now();
    let seed = request["scenario_seed"].as_u64().unwrap_or(0);
    // Blindfold before setup, priors, root filters, or successor previews can inspect the copy.
    if request["read_access"] != "internal" {
        sts2_core::blindfold::belief_root(&mut root.state, seed);
    }
    let cfg = config(request, &root.state, seed)?;
    let wall_ms = integer(request,"timeout_ms",8_000,600_000)?;
    if root.state.is_over {
        return Ok(json!({"ok":true,"strategy":"mcts","best":null,"alternatives":[],
            "skipped":true,"reason":"combat_terminal","work":{"simulations":0,"paths":0,"elapsed_ms":0}}));
    }
    let (decision, network_identity) = crate::networks::serve(
        request, &root.state, crate::networks::prepare(request, &root.state, cfg)?,
        started + Duration::from_millis(wall_ms), SIM_BATCH,
    )?;
    let (tree, chosen, consumed) = (decision.tree, decision.action, decision.simulations);
    let mut rows = tree.root_outcomes();
    let legal_count=sts2_core::legal_actions(&root.state).len();
    let forced=network_identity["decision_identity"]["forced_public_action"]==true;
    rows.sort_by(|left,right| right.mean_value.total_cmp(&left.mean_value).then(right.visits.cmp(&left.visits)));
    let public=request["read_access"]!="internal";
    let best = if forced {Some(forced_recommendation(&root,&chosen,public,seed))}
        else{rows.iter().find(|row| row.action == chosen).map(|row| recommendation(&root,row,true,public,seed))};
    if best.as_ref().is_some_and(|best|best["actions"][0].get("error").is_some()) {
        return Ok(unsupported(vec!["mcts_chosen_action_cannot_be_encoded".into()]));
    }
    let alternatives: Vec<Value> = rows.iter().filter(|row| row.action != chosen)
        .map(|row| json!({"actions":[root.encode_search_action(&row.action)],
            "score":row.mean_value,"evaluation":outcome(row),"mcts":outcome(row)})).collect();
    let mut statistics = if forced {unsearched_statistics()}else{search_statistics(&tree,&rows)};
    statistics["proven_win"] = json!(tree.root_proven_win());
    statistics["proven_loss"] = json!(tree.root_proven_loss());
    statistics["proof_meaning"] = json!("deterministic_solver_proofs_only_sampled_chance_nodes_never_proven");
    statistics["simulations_completed"] = json!(consumed);
    statistics["simulations_requested"] = json!(if forced {0}else{cfg.max_simulations});
    statistics["simulations_budget"] = json!(cfg.max_simulations);
    statistics["rollouts_total"] = json!(tree.rollout_stats().0);
    statistics["depth_cap_hits"] = json!(tree.rollout_stats().1);
    statistics["belief_cut_reasons"] = json!(tree.belief_cut_stats());
    statistics["belief_unsupported_cuts"] = json!(tree.belief_cut_stats().values().sum::<u64>());
    statistics["nodes"] = json!(tree.nodes_allocated());
    statistics["transposition_hits"] = json!(tree.tt_hits());
    statistics["endturn_prefix_cache"] = json!(endturn_prefix_cache_enabled(&root.state));
    if has_draw_order_model(&root.state) {
        statistics["priority_order_model"] = json!("native_receiver_order_and_explicit_public_bands");
        statistics["unknown_band_model"] = json!("uniform_epistemic_prior_not_native_rng");
        statistics["cache_reason"] = json!("priority_belief_cutoffs_require_complete_action_rollback");
    }
    if has_observed_quotes(&root.state) {
        statistics["cache_reason"] = json!("root_native_cost_quotes_expire_in_core_action_wrapper");
    }
    Ok(json!({"ok":best.is_some(),"strategy":"mcts","skipped":false,"best":best,
        "complete":forced||consumed==cfg.max_simulations,"forced_public_action":forced,
        "reason":if !forced&&consumed<cfg.max_simulations {json!("search_wall_time_budget_exhausted")}else{Value::Null},
        "alternatives":alternatives,"comparison_available":rows.len()>1,"support":root.search_support(),
        "mcts":statistics,"network_identity":network_identity,"components":network_identity["components"],
        "decision_identity":network_identity["decision_identity"],
        "full_legal_coverage":forced||rows.len()>=legal_count,
        "legal_root_actions":legal_count,"sampled_root_actions":rows.len(),"optimal_within_supported_horizon":false,
        "exact_within_horizon":false,"heuristic_score_only":false,
        "metrics":{"nodes":tree.nodes_allocated(),"simulations":consumed},
        "work":{"simulations":consumed,"paths":1,"elapsed_ms":started.elapsed().as_secs_f64()*1000.0},
        "limits":{"max_simulations":cfg.max_simulations,"max_rollout_depth":cfg.max_rollout_depth,
            "wall_ms":wall_ms,"action_domain":"cards_potions_choices_end_turn",
            "horizon":"combat_terminal_or_rollout_depth_cutoff"},
        "scope":"upstream_mcts_multiturn_combat",
        "decision_owner":if request["combat_decision_mode"]=="upstream" {"upstream_wrapper"}else{"model"},
        "model_retains_final_choice":request["combat_decision_mode"]!="upstream"}))
}

/// Apply a declared prefix literally, then let the upstream tree search its combat continuation.
pub fn evaluate(request: &Value) -> Result<Value,String> {
    let source = source(request)?;
    let seed = request["scenario_seed"].as_u64().unwrap_or(0);
    let root = position(request, &source, seed).map_err(|e|e.join(";"))?;
    evaluate_position(request, root)
}

/// Evaluate common hypothetical worlds from the complete mirror, never by reimporting a wire.
pub fn evaluate_position(request: &Value, template: PublicPosition) -> Result<Value,String> {
    let plans = request.get("candidate_plans").and_then(Value::as_array).ok_or("missing_candidate_plans")?;
    if plans.is_empty() || plans.len()>MAX_PLANS {return Err("candidate_count_out_of_range".into());}
    let samples = integer(request,"samples",1,256)? as usize;
    let seed = request.get("scenario_seed").map_or(Ok(0),|value|value.as_u64().ok_or("invalid_scenario_seed"))?;
    let wall_ms = integer(request,"timeout_ms",8_000,600_000)?;
    let started = Instant::now();
    let deadline = started+Duration::from_millis(wall_ms);
    let mut ids = BTreeSet::new();
    let mut candidates = Vec::new();
    let mut simulations = 0u64;
    let mut paths = 0usize;
    let mut all_outcomes = Vec::new();
    let mut complete = true;
    let mut network_identity = Value::Null;
    for plan in plans {
        let id = plan["id"].as_str().filter(|id|!id.is_empty()).ok_or("invalid_candidate_id")?;
        if !ids.insert(id) {return Err("duplicate_candidate_id".into());}
        let actions = plan["actions"].as_array().ok_or("missing_candidate_actions")?;
        if actions.len()>MAX_PREFIX_ACTIONS {return Err("candidate_action_count_out_of_range".into());}
        let mut reports = Vec::new();
        let mut outcomes = Vec::new();
        let mut failed = false;
        let mut budget_exhausted = false;
        let mut candidate_simulations = 0u64;
        for sample in 0..samples {
            if Instant::now()>=deadline {
                failed=true; reports.push(json!({"status":"timeout","scenario_index":sample,
                    "stop_reason":"request_wall_time_budget"})); continue;
            }
            let world_seed = seed.wrapping_add((sample as u64).wrapping_mul(WORLD_STRIDE));
            paths += 1;
            let mut root = template.clone();
            // Every candidate uses the same public world seed before its literal prefix.
            if request["read_access"]!="internal" {
                sts2_core::blindfold::belief_root(&mut root.state,world_seed);
            }
            let mut failure = None;
            for action in actions {
                if root.state.is_over || root.state.player.current_hp<=0 {break;}
                match root.decode_search_action(action) {
                    Ok(core_action)=>{
                        if !prefix_action_is_legal(&root.state,&core_action) {
                            failure=Some("declared_prefix_action_not_legal".into());break;
                        }
                        // Core handles len >= 2 in one atomic resolution, preserving the declared set.
                        if sts2_mcts::apply_search_action(&mut root.state,core_action)==StepResult::InvalidTarget {
                            failure=Some("invalid_target".into());break;
                        }
                        if root.state.player.deferred_draw_order {
                            if let Some(reason)=sts2_core::draw_knowledge::unsupported_reason(&root.state.player) {
                                failure=Some(format!("unsupported_draw_belief:{reason}"));break;
                            }
                        }
                    },
                    Err(reason)=>{failure=Some(reason);break;}
                }
            }
            if let Some(reason)=failure {
                let unsupported=reason.starts_with("unsupported_draw_belief:");
                failed=true;reports.push(json!({"status":if unsupported {"unsupported"}else{"needs_decision"},"scenario_index":sample,
                    "stop_reason":reason,"state":root.snapshot()}));continue;
            }
            let mut cfg=config(request,&root.state,world_seed)?;
            // Split each candidate's budget across common prefix worlds, preserving total work.
            cfg.max_simulations=(cfg.max_simulations/samples as u32).max(1);
            // Exploration is budget-dependent in the deployed upstream dialect.
            cfg.apply_belief_draw_dialect();
            let setup=crate::networks::prepare(request,&root.state,cfg)?;
            network_identity=setup.identity.clone();
            if root.state.is_over || root.state.player.current_hp<=0 {
                let won=root.state.player.current_hp>0;
                let hp=if won {root.state.player.current_hp.max(0) as f32}else{0.0};
                let value=sts2_mcts::terminal_state_value_with(&root.state,&setup.config,&setup.leaf);
                let terminal=RootOutcome{action:Action::EndTurn,visits:1,wins:u32::from(won),losses:u32::from(!won),ties:0,
                    value_sum:value,value_sq_sum:value*value,hp_sum:hp,hp_sq_sum:hp*hp,hp_n:1,mean_value:value};
                reports.push(json!({"status":"completed","scenario_index":sample,"state":root.snapshot(),
                    "network_identity":network_identity,"mcts":outcome(&terminal)}));
                outcomes.push(terminal);continue;
            }
            // Give each declared candidate/world an equal clock allowance.
            let slice=Duration::from_millis((wall_ms/(plans.len()*samples) as u64).max(1));
            let local_deadline=(Instant::now()+slice).min(deadline);
            let mut continuation_request=request.clone();
            // Prefix worlds keep their declared common random seeds instead of a live episode counter.
            continuation_request["scenario_seed"]=json!(world_seed);
            continuation_request["search_forced_roots"]=json!(true);
            match run_prepared(&root.state,setup,local_deadline,&continuation_request) {
                Ok((tree,_,count,identity))=>{
                    simulations+=u64::from(count);
                    candidate_simulations+=u64::from(count);
                    budget_exhausted |= count<cfg.max_simulations;
                    let rows=tree.root_outcomes();
                    reports.push(json!({"status":"completed","scenario_index":sample,"state":root.snapshot(),
                        "mcts":search_statistics(&tree,&rows),"simulations_completed":count,"network_identity":identity,
                        "budget_exhausted":count<cfg.max_simulations}));
                    outcomes.extend(rows);
                },
                Err(reason)=>{failed=true;reports.push(json!({"status":"unsupported","scenario_index":sample,"stop_reason":reason}));}
            }
        }
        all_outcomes.extend(outcomes.iter().cloned());
        complete &= !failed && !budget_exhausted;
        let metrics=if failed {Value::Null}else{world_statistics(&outcomes,&reports)};
        candidates.push(json!({"id":id,"status":if failed {"incomplete"}else{"estimated"},
            "complete":!failed && !budget_exhausted,"budget_exhausted":budget_exhausted,
            "simulations_completed":candidate_simulations,
            "sample_count":samples,"completed_samples":reports.iter().filter(|row|row["status"]=="completed").count(),
            "samples":reports,"metrics":metrics,"mcts":metrics}));
    }
    let mut all_statistics=totals(&all_outcomes);
    let reports:Vec<Value> = candidates.iter().flat_map(|candidate|
        candidate["samples"].as_array().into_iter().flatten().cloned()).collect();
    merge_execution(&mut all_statistics,&reports);
    all_statistics["meaning"]=json!("all_candidate_visit_totals_for_work_audit_not_candidate_comparison");
    Ok(json!({"ok":true,"complete":complete,"candidates":candidates,"scope":"fixed_prefix_then_upstream_mcts_combat_continuation",
        "mcts":all_statistics,"network_identity":network_identity,"components":network_identity["components"],
        "work":{"simulations":simulations,"paths":paths,"elapsed_ms":started.elapsed().as_secs_f64()*1000.0},
        "decision_owner":if request["combat_decision_mode"]=="upstream" {"upstream_wrapper"}else{"model"},
        "model_retains_final_choice":request["combat_decision_mode"]!="upstream","budget":{"paths_consumed":paths,
            "simulations_consumed":simulations,"request_wall_ms":wall_ms},
        "assumptions":["declared_prefix_actions_are_never_replaced",
            "continuation_is_upstream_mcts_including_potions_and_choices",
            "cutoffs_and_failed_prefix_worlds_are_never_reported_as_wins"]}))
}

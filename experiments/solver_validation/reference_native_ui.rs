//! Native-UI full-run reference over untouched pinned upstream crates.
//! Only initialization flags differ from the served arena; policy and driver are upstream.

use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, Write};
use std::path::Path;
use sts2_core::act_selection::ActVariant;
use sts2_core::full_run::{drive_full_run, FullRunConfig, RunOutcome};
use sts2_runai::arena::parse_character;
use sts2_runai::policy::{CombatBudget, MctsCombatPolicy};
use sts2_runai::ppo::features::Features;
use sts2_runai::ppo::policy::PpoPolicy;
use sts2_runai::trajectory::{Detail, Meta, RecordingPolicy};

// The standalone build preparation requires this exact clean upstream revision.
const UPSTREAM_COMMIT: &str = "28429066d91ed05c10b7b57597fc624196d197a4";

/// Preserve the upstream FullRun outcome instead of treating any game-over flag as victory.
fn outcome_token(outcome: &RunOutcome) -> &'static str {
    match outcome {
        RunOutcome::Won => "won",
        RunOutcome::Lost => "lost",
        RunOutcome::MaxSteps => "max_steps",
        RunOutcome::Livelock { .. } => "livelock",
        RunOutcome::EmptyActions { .. } => "empty_actions",
        RunOutcome::NotImplemented { .. } => "not_implemented",
        RunOutcome::Rejected { .. } => "rejected",
        RunOutcome::PolicyHalt { .. } => "policy_halt",
    }
}

/// Initialize native-UI flags and delegate every decision and transition to upstream.
fn handle(request: &Value) -> Result<Value, String> {
    let source_hashes = json!({"core":sts2_core::SRC_HASH,"content":sts2_content::SRC_HASH,
        "runai":sts2_runai::SRC_HASH,"mcts":sts2_mcts::SRC_HASH});
    if request["op"] == "metadata" {
        return Ok(json!({"ok":true,"upstream_commit":UPSTREAM_COMMIT,
            "backend":"pristine_upstream_native_ui","source_hashes":source_hashes}));
    }
    if request["op"] != "alignment_run" || request["engine_stage_profile"] != "native_ui" {
        return Err("expected_native_ui_alignment_run".into());
    }
    let seed = request["seed"].as_i64().ok_or("missing_seed")? as i32;
    let character_name = request["character"].as_str().ok_or("missing_character")?;
    let character = parse_character(character_name)?;
    let ascension = request["ascension"].as_u64().ok_or("missing_ascension")? as u8;
    let model_path = request["model_path"].as_str().ok_or("missing_model_path")?;
    let features = Features::try_parse(request["features"].as_str().unwrap_or(""))?;
    let budget = CombatBudget::eval_cheap();
    let cfg = FullRunConfig {
        max_steps: request["max_steps"].as_u64().ok_or("missing_max_steps")? as usize,
        livelock_stall_limit: request["livelock_stall_limit"].as_u64().ok_or("missing_stall_limit")? as usize,
        invincible: false,
        record_trace: false,
    };
    sts2_runai::rollout::ensure_content_installed();
    let mut rm = sts2_content::run_callbacks::new_run_manager_for_character_with_ascension(
        seed, character, ascension).with_neow_start();
    rm.run_state.interactive_card_removal = true;
    rm.run_state.interactive_card_upgrade = true;
    rm.run_state.interactive_card_transform = true;
    rm.run_state.direct_event_relic_grant = false;
    rm.run_state.interactive_potion_swap = true;
    rm = rm.with_act1(ActVariant::Overgrowth);
    sts2_content::run_callbacks::initialize_run_setup(&mut rm);
    let stage_flags = json!({"direct_event_relic_grant":rm.run_state.direct_event_relic_grant,
        "interactive_potion_swap":rm.run_state.interactive_potion_swap});
    let construction_flags = json!({"interactive_card_removal":rm.run_state.interactive_card_removal,
        "interactive_card_upgrade":rm.run_state.interactive_card_upgrade,
        "interactive_card_transform":rm.run_state.interactive_card_transform});
    let meta_policy = PpoPolicy::from_path_with(Path::new(model_path), features);
    let combat_policy = MctsCombatPolicy::new(meta_policy, budget, seed);
    // Read resolved defaults from the actual upstream policy; the read does not search or mutate it.
    let (ordinary_config, leaf) = combat_policy.search_config(budget.sims, 0, Some(character));
    let (boss_config, _) = combat_policy.search_config(budget.boss_sims, 0, Some(character));
    let actual_config = json!({"ordinary":format!("{ordinary_config:?}"),
        "boss":format!("{boss_config:?}"),"leaf":format!("{leaf:?}"),
        "config_scope":"original search_config base; wrapper applies root prior, boss carry and potion seams",
        "transposition_table_effective":true,
        "ordinary_simulations":ordinary_config.max_simulations,
        "boss_simulations":boss_config.max_simulations,
        "max_rollout_depth":ordinary_config.max_rollout_depth,
        "hidden_info":format!("{:?}",ordinary_config.hidden_info),
        "rollout_policy":format!("{:?}",ordinary_config.rollout_policy),
        "terminal_eval_mode":format!("{:?}",ordinary_config.terminal_eval_mode),
        "ironclad_layer_on":combat_policy.ironclad_layer_on(),
        "prior_policy":"upstream built-in Ironclad elite/boss gate, mix 0.5, temperature 1",
        "builtin_rollout_available":sts2_mcts::rollout_net::builtin_net(character).is_some(),
        "builtin_prior_available":sts2_mcts::rollout_net::builtin_prior_json(character).is_some(),
        "max_steps":cfg.max_steps,"stall_limit":cfg.livelock_stall_limit});
    // The upstream recorder forwards prepare/choose unchanged and only consumes telemetry.
    let mut policy = RecordingPolicy::new(combat_policy, Detail::Full);
    let report = drive_full_run(&mut rm, &mut policy, &cfg);
    let outcome = outcome_token(&report.outcome);
    let counts = policy.inner().fallbacks();
    let (elite, boss, motif, simulations) = policy.inner().combat_budget_spent();
    let (peak_actions, peak_tokens) = policy.inner().meta().peak();
    let prior = sts2_mcts::prior_counts::snapshot();
    let combat = json!({"decisions":policy.inner().combat_decisions(),
        "combat_decisions_elite":elite,"combat_decisions_boss":boss,
        "combat_decisions_motif":motif,"combat_sims_spent":simulations,
        "combat_fallback_untranslatable":counts.untranslatable,
        "combat_fallback_no_outcome":counts.no_outcome,
        "combat_fallback_not_surfaced":counts.not_surfaced,
        "fallback_total":counts.total(),"prior_lookups":prior.lookups,
        "prior_evals":prior.evals,"prior_uniform_fallbacks":prior.uniform_fallbacks});
    let meta = Meta {
        seed_int:seed, character:character_name.into(), ascension, policy:"ppo".into(),
        policy_label:"ppo+eval-cheap-native-ui".into(), combat_budget:"eval-cheap".into(),
        sims:budget.sims, boss_sims:budget.boss_sims, model_path:Some(model_path.into()),
        model_sha256:request["model_sha256"].as_str().map(str::to_owned),
        engine_git_sha:Some(UPSTREAM_COMMIT.into()), env:BTreeMap::new(), detail:"full".into(),
        outcome:outcome.into(), final_hp:report.final_hp, final_gold:report.final_gold,
        steps:report.steps,
    };
    let run_file = policy.into_run_file(&rm, &report, meta);
    let heuristic_fallback_records = run_file.sts2sim.decisions.iter().filter(|decision|
        decision.meta_eval.as_ref().is_some_and(|item| item.source == "heuristic_fallback")).count();
    Ok(json!({"ok":true,"backend":"pristine_upstream_native_ui","upstream_commit":UPSTREAM_COMMIT,
        "seed":seed,"character":character_name,"ascension":ascension,"act1":"overgrowth",
        "engine_stage_profile":"native_ui","stage_flags":stage_flags,
        "construction_flags":construction_flags,"actual_config":actual_config,
        "source_hashes":source_hashes,"model_path":model_path,"model_sha256":request["model_sha256"],
        "features":request["features"],"outcome":outcome,"won":matches!(report.outcome,RunOutcome::Won),
        "full_run_outcome":format!("{:?}",report.outcome),"player_won":report.player_won,
        "upstream_steps":report.steps,"final_hp":report.final_hp,"final_gold":report.final_gold,
        "act_index":report.act_index,"final_phase":format!("{:?}",report.final_phase),
        "combat":combat,"ppo_peak_act_slots":peak_actions,"ppo_peak_tokens":peak_tokens,
        "heuristic_fallback_records":heuristic_fallback_records,
        "decisions":run_file.sts2sim.decisions,"trajectory":run_file} ))
}

/// Keep upstream panics and malformed input visible as failed responses in the isolated child.
fn main() {
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = match line { Ok(line) => line, Err(_) => break };
        let result = std::panic::catch_unwind(|| {
            let request: Value = serde_json::from_str(&line).map_err(|error| error.to_string())?;
            handle(&request)
        });
        let response = match result {
            Ok(Ok(response)) => response,
            Ok(Err(error)) => json!({"ok":false,"error":error}),
            Err(payload) => {
                let message = payload.downcast_ref::<String>().cloned()
                    .or_else(|| payload.downcast_ref::<&str>().map(|item| (*item).to_owned()));
                json!({"ok":false,"outcome":"panic","won":false,"error":"pristine_upstream_panic",
                    "panic_message":message,"upstream_commit":UPSTREAM_COMMIT})
            }
        };
        if writeln!(stdout,"{response}").and_then(|_| stdout.flush()).is_err() { break; }
    }
}

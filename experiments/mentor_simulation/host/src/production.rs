//! Shared protocol over upstream state, search, networks, and run-policy services.
use crate::public::PublicPosition;
use serde_json::{json, Value};
use sts2_core::CombatState;
use std::time::Instant;

// The source pin identifies the reusable engine independently of local integration changes.
const UPSTREAM_COMMIT: &str = "28429066d91ed05c10b7b57597fc624196d197a4";

/// Serve metadata and offline complete-wire operations without reconstructing public history.
pub fn handle(request: &Value) -> Value {
    let started = Instant::now();
    if request["op"] == "search" && request["strategy"].as_str().is_some_and(|s| s != "mcts") {
        return finish(Err("retired_mentor_search_strategy_use_mcts".into()), started);
    }
    let result = match request["op"].as_str().unwrap_or("inspect") {
        "metadata" => Ok(json!({"ok":true,"protocol_version":3,
            "scope":"upstream_persistent_mirror_and_honest_search", "mcts":crate::mcts::defaults(),
            "network_protocol_version":1,"ppo_models":crate::ppo::model_manifest(),
            "combat_networks":crate::networks::inventory()})),
        "policy_evaluate" => crate::ppo::policy_evaluate(request),
        "network_diagnostics" => crate::ppo::network_diagnostics(request),
        "alignment_run" => crate::alignment::full_run(request),
        "combat_trial" => crate::validation::combat_trial(request),
        "search" => crate::mcts::search(request),
        "evaluate" => crate::mcts::evaluate(request),
        "inspect" | "simulate" => {
            let source = &request["public_state"];
            PublicPosition::for_search(source,0,None).map_err(|e|e.join(";")).and_then(|p| simulate(request,p))
        }
        _ => Err("unknown_op".into()),
    };
    finish(result, started)
}

/// Borrow no game state during search: the extension supplies a clone of the reconciled mirror.
pub fn handle_state(request: &Value, state: CombatState) -> Value {
    let started = Instant::now();
    if request["op"] == "search" && request["strategy"].as_str().is_some_and(|s| s != "mcts") {
        return finish(Err("retired_mentor_search_strategy_use_mcts".into()), started);
    }
    let root = PublicPosition::from_core(state, &request["public_state"], request["read_access"] == "internal");
    let result = match request["op"].as_str().unwrap_or("inspect") {
        "search" => crate::mcts::search_position(request, root),
        "evaluate" => crate::mcts::evaluate_position(request, root),
        "inspect" | "simulate" => simulate(request, root),
        _ => return handle(request),
    };
    finish(result, started)
}

/// Simulate literal actions on an honest private copy; never advance the calibration mirror.
fn simulate(request: &Value, mut root: PublicPosition) -> Result<Value,String> {
    let mut events = Vec::new();
    let mut errors = Vec::new();
    let mut sampled = false;
    if request["op"] == "simulate" {
        if request["read_access"] != "internal" {
            sts2_core::blindfold::belief_root(&mut root.state, request["scenario_seed"].as_u64().unwrap_or(0));
        }
        for (index, action) in request["actions"].as_array().ok_or("missing_actions")?.iter().enumerate() {
            let streams = root.state.run_rng.combat_counter_fingerprint();
            let legacy = root.state.rng.clone();
            match root.apply(action) {
                Ok(()) => {
                    let stochastic = streams != root.state.run_rng.combat_counter_fingerprint() || legacy != root.state.rng;
                    sampled |= stochastic;
                    events.push(json!({"action_index":index,"action":action,"applied":true,"stochastic":stochastic}));
                },
                Err(error) => { errors.push(json!({"action_index":index,"error":error})); break; }
            }
        }
    }
    Ok(json!({"ok":errors.is_empty(),"state":root.snapshot(),"events":events,"errors":errors,
        "hypothetical":sampled && request["read_access"] != "internal",
        "support":root.search_support(),"scope":"upstream_literal_prefix_sample",
        "legal_actions":sts2_core::legal_actions(&root.state).iter().map(|a|root.encode_search_action(a)).collect::<Vec<_>>()}))
}

/// Attach the same semantic identity to native-extension and standalone responses.
fn finish(result: Result<Value,String>, started: Instant) -> Value {
    let mut response = result.unwrap_or_else(|reason|json!({"ok":false,"errors":[reason]}));
    response["backend"] = json!("mentor");
    response["rule_snapshot"] = json!(format!("mentor-v0.107.1-mirror-v1:{}:{}:{}:{}",
        sts2_core::SRC_HASH,sts2_content::SRC_HASH,sts2_mcts::SRC_HASH,sts2_runai::SRC_HASH));
    response["upstream_commit"] = json!(UPSTREAM_COMMIT);
    response["game_version"] = json!("v0.107.1");
    response["elapsed_ms"] = json!(started.elapsed().as_secs_f64()*1000.0);
    response
}

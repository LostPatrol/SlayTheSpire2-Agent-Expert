"""Verify actual combat network identities, independent switches and legacy numerical parity."""
import json
import math
from pathlib import Path
import struct

import pytest

from test_public_adapter import client, frame, public_card
from test_mcts import request_search


# The compatibility evaluator keeps this real trained artifact at its native schema width.
VALUE_ASSET = Path(__file__).resolve().parents[2] / "vendor/spire-mentor/sts2-rs/data/value_policy_net/h16.json"


def f32(value):
    """Round every operation exactly as the original scalar f32 inference path does."""
    return struct.unpack("f", struct.pack("f", value))[0]


def native_legacy_value(raw):
    """Run the original 88-column tensors directly, without the Rust compatibility loader."""
    asset = json.loads(VALUE_ASSET.read_text())
    width = asset["arch"]["input_dim"]
    hidden = [f32(f32(f32(raw[i]) - f32(asset["feature_mean"][i])) / f32(asset["feature_std"][i]))
              for i in range(width)]
    weights = asset["weights"]
    for layer in range(1, asset["arch"]["trunk_depth"] + 1):
        output = [f32(value) for value in weights[f"b{layer}"]]
        for i, value in enumerate(hidden):
            for j in range(len(output)):
                output[j] = f32(output[j] + f32(value * f32(weights[f"W{layer}"][i][j])))
        hidden = [max(value, 0.0) for value in output]
    logit = f32(weights["b_v"][0])
    for i, value in enumerate(hidden):
        logit = f32(logit + f32(value * f32(weights["W_v"][i][0])))
    probability = f32(1.0 / f32(1.0 + f32(math.exp(-logit))))
    return f32(f32(2.0 * f32(probability - 0.5)) * f32(0.49))


def combat_state():
    """Give the original Ironclad elite gate real legal card choices."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=1)])
    state["run"].update(room_type="elite", act_index=1, act_count=3,
                        is_last_act=False, awaiting_second_boss=False)
    return state


def test_endpoint_uses_upstream_bundle_and_independent_disabled_components(client):
    """A real search exposes the trained prior and returns each disabled role to its basic API."""
    state = combat_state()
    upstream = request_search(client, state, simulations=32, network_diagnostics=True)
    assert upstream["ok"], upstream
    identity = upstream["network_identity"]
    assert identity["components"]["action_prior"]["active"], identity
    assert identity["components"]["action_prior"]["mix"] == 0.5
    assert identity["components"]["search_profile"]["removed_counts_forms"]
    assert identity["components"]["value_model"]["kind"] == "TexelWide"
    assert len(set(identity["diagnostics"]["root_prior"])) > 1
    disabled = request_search(client, state, simulations=32, network_diagnostics=True,
                              rollout_policy="heuristic", action_prior="off", search_profile="base")
    assert disabled["ok"], disabled
    identity = disabled["network_identity"]
    assert identity["components"]["rollout"]["kind"] == "Heuristic"
    assert not identity["components"]["action_prior"]["active"]
    assert not identity["components"]["search_profile"]["removed_counts_forms"]
    assert len(set(identity["diagnostics"]["root_prior"])) == 1
    assert identity["execution_corrections"]["terminal_rollouts"]


def test_learned_endpoint_matches_independent_native_88_inference(client):
    """The explicitly selected model uses the original weights and feature prefix numerically."""
    result = request_search(client, combat_state(), simulations=32,
                            value_model="learned", network_diagnostics=True)
    assert result["ok"], result
    identity = result["network_identity"]
    value = identity["components"]["value_model"]
    assert value["sha256"] == "322f30a633f76ebcd127a0b14796f4410c32fa27385f38a57cc7e7f983714102"
    assert value["native_input_dim"] == 88
    raw = identity["diagnostics"]["wide_features"]
    assert len(raw) == 216
    assert identity["diagnostics"]["root_leaf_value"] == pytest.approx(native_legacy_value(raw), abs=1e-6)


def test_unknown_network_choice_is_an_explicit_engine_error(client):
    """An unsupported component never silently becomes a different policy."""
    result = request_search(client, combat_state(), simulations=32, value_model="untrained")
    assert not result["ok"], result
    assert "invalid_value_model" in json.dumps(result), result


def test_actual_production_wrapper_counter_and_decision_ownership(client):
    """Direct requests traverse the wrapper and retries retain the original episode seed."""
    request = {"op": "search", "strategy": "mcts", "public_state": combat_state(),
               "simulations": 32, "max_rollout_depth": 200, "benchmark_run_seed": 3,
               "decision_counter": 0, "decision_counter_source": "offline_test_counter",
               "combat_decision_mode": "upstream"}
    first = client.request(request)
    repeated = client.request(request)
    next_episode = client.request({**request, "decision_counter": 1})
    assert first["ok"] and repeated["ok"] and next_episode["ok"]
    identity = first["decision_identity"]
    assert identity["wrapper_api"] == "MctsCombatPolicy::search_combat->mcts_pick"
    assert identity["counter_source"] == "offline_test_counter"
    assert identity["counter"] == 0 and identity["next_counter"] == 1
    assert identity["seed_kind"] == "explicit_offline_run_seed_and_wrapper_counter"
    assert repeated["decision_identity"] == identity
    assert repeated["best"]["actions"] == first["best"]["actions"]
    assert repeated["mcts"] == first["mcts"]
    assert next_episode["decision_identity"]["search_seed"] != identity["search_seed"]
    assert first["decision_owner"] == "upstream_wrapper"
    assert first["model_retains_final_choice"] is False
    advisory = request_search(client, combat_state(), simulations=32)
    assert advisory["decision_owner"] == "model" and advisory["model_retains_final_choice"] is True


def test_forced_production_action_has_no_fabricated_search_value(client):
    """A forced action spends no search budget; fixed-prefix statistics remain explicitly opt-in."""
    state = combat_state()
    state["combat"]["player"]["energy"] = 0
    result = request_search(client, state, simulations=32, combat_decision_mode="upstream")
    assert result["ok"] and result["complete"] and result["forced_public_action"], result
    assert result["best"]["actions"] == [{"type": "end_turn"}]
    assert result["work"]["simulations"] == 0
    assert result["best"]["score"] is None and result["best"]["mcts"]["mean"] is None
    assert result["mcts"]["p_win"] is None and result["mcts"]["visits"] == 0
    assert result["decision_identity"]["forced_root_searched"] is False
    evaluation = client.request({"op": "evaluate", "rollout": True, "public_state": state,
                                 "samples": 1, "simulations": 32, "scenario_seed": 41,
                                 "candidate_plans": [{"id": "terminal-stats", "actions": []}]})
    assert evaluation["work"]["simulations"] == 32, evaluation
    sample = evaluation["candidates"][0]["samples"][0]
    assert sample["network_identity"]["decision_identity"]["extra_statistics_work"] is True

"""Public-only simulator IPC, identity handling and non-fatal failure checks."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from sts2_agent.public import ActionError, observation, validate_action
from sts2_agent.prompt_context import current_action_reference
from sts2_agent.config import ConfigError, load_config
from sts2_agent.simulation import (CombatSimulator, HOST_DLL, HOST_MENTOR, SIMULATED_ACTION_FIELDS,
                                 STDERR_TAIL_LINES)


# Fixtures and the optional compiled host live in the repository, not an installed game.
ROOT = Path(__file__).resolve().parents[1]
# An independent JSONL process exercises real pipe buffering and process replacement.
FAKE_HOST = r'''
import json
import os
from pathlib import Path
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
log, mode, marker = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
first_generation = not marker.exists()
marker.write_text("started", encoding="utf-8")
for line in sys.stdin:
    request = json.loads(line)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "request": request}) + "\n")
    if mode == "timeout" and first_generation:
        time.sleep(1.2)
    if mode == "invalid":
        print("this is not JSON", flush=True)
        continue
    if mode == "exit":
        sys.exit(4)
    if mode == "stderr":
        for i in range(4000):
            print(str(i) + " diagnostic " + "x" * 100, file=sys.stderr)
        sys.stderr.flush()
    support = {"level": "exact", "reasons": [], "unknown_ids": []}
    score_support = {"level": "partial", "reasons": ["turn_end_effects_not_resolved"]}
    state = {"player": {"energy": 2}, "enemies": [{"i": 11}], "local_audit_only": True}
    if mode == "unsupported":
        response = {"ok": False, "reason": "unsupported_public_combat", "best": None,
                    "support": {"level": "unsupported"}, "state": state, "events": []}
    elif request["op"] == "evaluate":
        candidates = []
        for plan in request["candidate_plans"]:
            samples = [{"status": "completed", "state": state, "events": [], "stop_reason": "plan_completed"}
                       for _ in range(request["samples"])]
            candidates.append({"id": plan["id"], "status": "estimated", "samples": samples,
                               "sample_count": len(samples), "completed_samples": len(samples),
                               "metrics": {"mean_hp_loss": 0}})
        response = {"ok": True, "candidates": candidates, "budget": {}, "scope": "fixed_plans_current_turn"}
        if mode == "needs_decision":
            for candidate in candidates:
                candidate.update(status="needs_decision", completed_samples=0, metrics=None)
                for sample in candidate["samples"]:
                    sample.update(status="needs_decision", stop_reason="pending_choice_without_declared_action")
        if mode == "bad_evaluation":
            response["candidates"] = [{"id": "wrong-plan"}]
        if mode in {"unknown_draw_partial", "unknown_draw_prefix_partial"}:
            for candidate in candidates:
                candidate.update(status="needs_decision", completed_samples=len(candidate["samples"]) - 1,
                                 metrics=None)
                candidate["samples"][-1].update(status="needs_decision", stop_reason="pending_choice_without_declared_action")
        if mode == "unknown_draw_empty_metrics":
            for candidate in candidates:
                candidate["metrics"] = {}
    elif request["op"] == "policy_evaluate":
        response = {"ok": True, "best_action": {"type": "choose_event_option", "option_index": 7},
                    "candidates": [{"action": {"type": "choose_event_option", "option_index": 7}, "probability": 0.7}],
                    "model_identity": {"source": "offline-test-double"}, "support": {"level": "supported"},
                    "decision_pipeline": "offline-test-wire-advisor", "value": 0.2,
                    "next_context": {"intent": "shop_remove"}, "work": {"simulations": 0, "paths": 1}}
    elif request["op"] == "simulate":
        response = {"ok": True, "support": support, "state": state,
                    "events": [{"action": request["actions"][0], "applied": True}]}
        if mode == "bad_events":
            response["events"] = None
        if mode.startswith("unknown_draw"):
            response.update(ok=False, state={"strict_failure_only": True}, events=[],
                            errors=[{"error": "unknown_draw_boundary"}])
            if mode.startswith("unknown_draw_prefix"):
                response["turn_stages"] = {
                    "status": "partial",
                    "checkpoints": [
                        {"stage": "player_turn_end", "state": {"player": {"hp": 40}}},
                        {"stage": "enemy_turn_end", "state": {"player": {"hp": 33}}},
                        {"stage": "before_next_player_draw", "state": {"player": {"hp": 35}}},
                    ],
                    "boundary": {"stage": "next_player_draw", "reason": "unknown_draw_boundary"},
                }
        if mode == "early_turn_failure":
            response.update(ok=False, events=[], errors=[{"error": "unsupported_end_turn_power"}],
                            turn_stages={"status": "unsupported", "checkpoints": [],
                                         "boundary": {"stage": "player_turn_end",
                                                      "reason": "unsupported_end_turn_power"}})
    else:
        actions = [
            {"type": "play_card", "card_handle": "h1", "card_index": 29, "target_index": 11},
            {"type": "play_card", "card_handle": "h0", "card_index": 7, "target_index": 11},
            {"type": "end_turn", "card_handle": None, "card_index": None, "target_index": None},
        ]
        if mode == "bad_handle":
            actions[0]["card_index"] = 7
        if mode in {"generated_later", "generated_first"}:
            actions[1 if mode == "generated_later" else 0] = {
                "type": "play_card", "card_handle": "s0", "card_index": None,
                "card_id": "STRIKE_IRONCLAD", "target_index": 11,
            }
        if mode == "stop_only":
            actions = [{"type": "end_turn"}]
        if mode == "threshold" and request["strategy"] == "exhaustive":
            response = {"ok": True, "skipped": True, "best": None,
                        "reason": "exhaustive_terminal_limit_exceeded"}
        else:
            response = {"ok": True, "skipped": False, "strategy": request["strategy"],
                        "support": support, "score_support": score_support,
                        "search_support_reasons": [], "full_legal_coverage": True,
                        "best": {"actions": actions, "score": 9.0, "state": state,
                                 "score_breakdown": {"projected_hp_loss": 0},
                                 "score_support": score_support, "termination": "stop"},
                        "optimal_within_supported_horizon": request["strategy"] == "exhaustive"}
            if mode == "stop_only":
                response.update(comparison_available=False, supported_initial_card_actions=0)
            if mode == "bad_actions":
                response["best"]["actions"] = ["not an action object"]
            if mode == "nan_score":
                response["best"]["score"] = float("nan")
            if mode == "late_bad_plan" and request["strategy"] == "exhaustive":
                response["best"] = ["not a plan object"]
    if request["op"] == "search" and request["strategy"] == "mcts":
        response["decision_identity"] = {"counter": request.get("decision_counter"),
            "next_counter": request.get("decision_counter", 0) + 1}
        response["model_retains_final_choice"] = request.get("combat_decision_mode") != "upstream"
        if mode == "forced":
            response.update(forced_public_action=True, complete=True,
                            work={"simulations": 0, "paths": 0})
            response["best"].update(actions=[{"type": "end_turn"}], score=None,
                mcts={"visits": 0, "wins": 0, "losses": 0, "cutoffs": 0,
                      "mean": None, "p_win": None, "p_loss": None, "p_unresolved": None})
    response["pid"] = os.getpid()
    print(json.dumps(response), flush=True)
'''


def public_frame():
    """Keep duplicate card IDs separate using nonsequential public hand/enemy slots."""
    fixture = json.loads((ROOT / "tests/fixtures/public/combat.json").read_text(encoding="utf-8"))
    state = fixture["state"]
    card = state["combat"]["hand"][1]
    state["combat"]["hand"] = [deepcopy(card), deepcopy(card)]
    for row, index in enumerate((7, 29)):
        state["combat"]["hand"][row].update(i=index, targets=[11], upgraded=bool(row))
    state["combat"]["enemies"][0]["i"] = 11
    result = observation(state, "public-run", "public-decision")
    result["raw"] = {"seed": "never-send-this", "hidden_draw_order": ["SECRET_CARD"]}
    result["seed"] = "wrapper-seed-must-not-be-sent"
    return result


def test_native_generated_choice_skip_remains_distinct_from_confirmation():
    """The compact bridge choice exposes a real index-free decline, never a fabricated confirm."""
    frame = observation({"screen": "CARD_SELECTION", "available_actions": ["select_deck_card", "skip_selection"],
        "selection": {"kind": "choose_card_select", "min": 1, "max": 1, "selected": 0,
                      "confirm": False, "can_skip": True, "cards": [{"i": 11, "card_id": "ANGER"}]}},
        "skip-run", "skip-episode")
    action = {"type": "skip_selection"}
    validate_action(frame, action)
    assert frame["action_parameters"]["skip_selection"] == {}
    assert CombatSimulator._first_action(action, []) == action
    assert CombatSimulator._sequence_step(action, []) == action
    plans = [{"id": "decline", "actions": [action]}]
    assert CombatSimulator._evaluation_plans(plans, 1) == plans
    reference = current_action_reference(frame)
    assert reference["selection"]["can_skip"] is True
    assert "skip_selection" in reference["available_actions"]
    with pytest.raises(ActionError):
        validate_action(frame, {"type": "confirm_selection"})
    with pytest.raises(ActionError):
        validate_action(frame, {"type": "skip_selection", "option_index": 11})


def test_mcts_default_effort_private_access_and_root_binding(tmp_path):
    """Public mode ignores secrets; internal data is usable only for its exact public root."""
    client, log = make_client(tmp_path, strategy="mcts")
    frame = public_frame()
    frame["simulation_internal"] = {"rng": "private-stream"}
    try:
        client.advise(frame)
        public_request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert public_request["strategy"] == "mcts"
        assert (public_request["simulations"], public_request["max_rollout_depth"]) == (1000, 200)
        assert (public_request["normal_simulations"], public_request["boss_simulations"]) == (1000, 4000)
        assert "internal_state" not in public_request
        assert "simulation_internal" not in public_request["public_state"]
        client.configure({"read_access": "internal", "candidate_source": "mcts", "search_strength": 3})
        private = frame.pop("simulation_internal")
        client.set_internal_state(frame["state"], private)
        client.advise(frame)
        private_request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert private_request["internal_state"] == private
        frame["state"]["combat"]["player"]["energy"] = 0
        client.advise(frame)
        stale_request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert "internal_state" not in stale_request
    finally:
        client.close()


def test_model_candidates_skip_search_and_allow_large_rollout_evaluation(tmp_path):
    """Model mode spends no automatic search budget and evaluates prefixes beyond old limits."""
    client, log = make_client(tmp_path, strategy="mcts")
    frame = public_frame()
    client.configure({"read_access": "public", "candidate_source": "model", "search_strength": 3})
    try:
        assert client.advise(frame)["status"] == "skipped"
        assert not log.exists()
        plans = [{"id": str(i), "actions": [{"type": "end_turn"}] * 9} for i in range(5)]
        result = client.evaluate(frame, plans, samples=13)
        assert result["status"] == "evaluated"
        request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert request["rollout"] is True and len(request["candidate_plans"]) == 5
        ledger = json.loads(next((tmp_path / "local/simulation-budgets").glob("*.json")).read_text())
        assert ledger["search_requests_used"] == 1
        assert ledger["paths_used"] == 0
        old_identity = client.rule_identity()
        client.configure({"read_access": "public", "candidate_source": "mcts", "search_strength": 5})
        assert client.rule_identity() != old_identity
        client.advise(frame)
        ledger_after = json.loads(next((tmp_path / "local/simulation-budgets").glob("*.json")).read_text())
        assert ledger_after["search_requests_used"] == 2
    finally:
        client.close()


def test_rule_identity_preserves_host_protocol_two_provenance(tmp_path):
    """A protocol upgrade changes proposal identity instead of being reported as protocol one."""
    artifact = tmp_path / HOST_MENTOR
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"offline-rule-artifact")
    artifact.with_name("source-provenance.json").write_text(json.dumps({
        "protocol_version": 2, "rule_snapshot": "mentor-mcts-v2", "upstream_commit": "pinned"}))
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    assert client.rule_identity()["protocol_version"] == 2
    assert client.rule_identity()["rule_snapshot"] == "mentor-mcts-v2"


@pytest.mark.parametrize("field", ["network_assets", "source_hashes"])
def test_model_assets_and_source_hashes_change_semantic_rule_identity(tmp_path, field):
    """Changing packaged network weights or rule-source hashes retires proposals without relying on timestamps."""
    artifact = tmp_path / HOST_MENTOR
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"same-host-artifact")
    provenance = artifact.with_name("source-provenance.json")
    provenance.write_text(json.dumps({"rule_snapshot": "fixed-host", field: {"trained-model": "first-sha"}}))
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    previous = client.rule_identity()
    provenance.write_text(json.dumps({"rule_snapshot": "fixed-host", field: {"trained-model": "second-sha"}}))
    assert client.rule_identity() != previous
    assert client.rule_identity()["artifact_sha256"] == previous["artifact_sha256"]


@pytest.mark.parametrize("strength,normal", [(1.25, 313), (2.125, 563), (3.3333333, 1500), (4.5, 3750), (5, 5000)])
def test_continuous_strength_dispatches_interpolated_work_without_quantizing_identity(tmp_path, strength, normal):
    """Search and model evaluation send interpolated integer budgets but preserve continuous identity."""
    client, log = make_client(tmp_path, strategy="mcts")
    settings = {"read_access": "public", "candidate_source": "mcts", "search_strength": strength}
    try:
        client.configure(settings)
        identity = client.rule_identity()
        assert identity["settings"]["search_strength"] == strength
        frame = public_frame()
        client.advise(frame)
        request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert (request["simulations"], request["normal_simulations"], request["boss_simulations"]) == (normal, normal, normal * 4)
        frame["state"]["run"]["room_type"] = "Boss"
        client.evaluate(frame, [{"id": "pass", "actions": [{"type": "end_turn"}]}], samples=1)
        request = json.loads(log.read_text().splitlines()[-1])["request"]
        assert (request["simulations"], request["normal_simulations"], request["boss_simulations"]) == (normal * 4, normal, normal * 4)
        nearby = strength - 0.000001 if strength == 5 else strength + 0.000001
        client.configure({**settings, "search_strength": nearby})
        assert client.rule_identity() != identity
    finally:
        client.close()


@pytest.mark.parametrize("strength", [True, False, float("nan"), float("inf"), float("-inf"), "3.5", 0.99, 5.01])
def test_continuous_strength_rejects_invalid_config_and_restore_values(tmp_path, strength):
    """Direct configuration uses the same finite numeric range as restored dashboard settings."""
    with pytest.raises(ValueError, match="Invalid simulation settings"):
        CombatSimulator({"simulation": {"enabled": True, "search_strength": strength}}, tmp_path)


def test_continuous_strength_configuration_schema_preserves_value_and_rejects_nan(tmp_path):
    """Published configuration loading accepts fractions and rejects nonfinite JSON numbers."""
    config = json.loads((ROOT / "configs/a0-simulation.json").read_text())
    path = tmp_path / "agent.json"
    for strength in (1, 3, 5, 2.431234567):
        config["simulation"]["search_strength"] = strength
        path.write_text(json.dumps(config))
        assert load_config(path, ROOT)["simulation"]["search_strength"] == strength
    for strength in (True, float("nan"), float("inf"), float("-inf")):
        config["simulation"]["search_strength"] = strength
        path.write_text(json.dumps(config))
        with pytest.raises(ConfigError, match="simulation.search_strength"):
            load_config(path, ROOT)


def test_mcts_single_end_turn_candidate_is_valid_advice(tmp_path):
    """A sole legal end-turn rollout remains usable without claiming a choice comparison."""
    client, _ = make_client(tmp_path, mode="stop_only", strategy="mcts")
    try:
        advice = client.advise(public_frame())
        summary = advice["simulation_advice"]["strategies"]["mcts"]
        assert summary["first_action"] == {"type": "end_turn"}
        assert summary["sequence"] == [{"type": "end_turn"}]
        assert advice["simulation_advice"]["comparison_available"] is False
    finally:
        client.close()


def make_client(tmp_path, *, mode="normal", timeout=3, strategy="compare"):
    """Build a real subprocess client whose host never touches game files."""
    script = tmp_path / "host.py"
    script.write_text(FAKE_HOST, encoding="utf-8")
    log, marker = tmp_path / "requests.jsonl", tmp_path / "started"
    command = [sys.executable, "-u", str(script), str(log), mode, str(marker)]
    client = CombatSimulator({"simulation": {"enabled": True, "timeout_seconds": timeout, "search_strategy": strategy}},
                             tmp_path, command=command)
    return client, log

def test_advice_is_public_only_and_predicts_the_actual_chosen_action(tmp_path):
    """Retain full local evidence but make only first_action executable in the prompt."""
    client, log = make_client(tmp_path)
    observed = public_frame()
    original = deepcopy(observed)
    try:
        advice = client.advise(observed)
        assert advice["status"] == "partial"
        assert advice["observation_id"] == "public-decision"
        assert len(advice["public_state_hash"]) == 64
        assert set(advice["results"]) == {"beam", "greedy", "exhaustive"}
        prompt = advice["simulation_advice"]
        assert prompt["backend"] == "mentor" and advice["rule_identity"]["backend"] == "mentor"
        for strategy in prompt["strategies"].values():
            assert strategy["first_action"] == {"type": "play_card", "card_index": 29, "target_index": 11}
            sequence = strategy["sequence"]
            assert [step.get("card_handle") for step in sequence] == ["h1", "h0", None]
            assert [step["initial_hand_row"] for step in sequence[:2]] == [1, 0]
            assert sequence[0]["card_id"] == sequence[1]["card_id"]
            assert sequence[0]["upgraded"] is True and sequence[1]["upgraded"] is False
            assert all("card_index" not in step for step in sequence)
            assert sequence[-1] == {"type": "stop_projection"}
            assert strategy["endpoint"] == {
                "meaning": "after_supported_card_plays_before_turn_end_or_enemy_actions",
                "horizon": "current_player_turn_before_end_turn_effects",
                "player": {"energy": 2}, "enemies": [{"i": 11}],
            }
        assert "local_audit_only" not in json.dumps(prompt)
        assert advice["results"]["beam"]["best"]["state"]["local_audit_only"] is True
        assert "re-observe" in prompt["usage"] and "final chooser" in prompt["usage"]
        # Deliberately select a different duplicate from the planner's first suggestion.
        actual = {"type": "play_card", "card_index": 7, "target_index": 11}
        prediction = client.predict(observed, actual)
        assert prediction["status"] == "predicted"
        assert prediction["action"] == actual
        assert prediction["result"]["events"][0]["action"] == actual
        assert prediction["state"]["enemies"][0]["i"] == 11
        assert prediction["public_state_hash"] == advice["public_state_hash"]
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 4 and len({row["pid"] for row in rows}) == 1
        for row in rows:
            request = row["request"]
            assert request["public_state"] == observed["state"]
            assert "seed" not in request and "raw" not in request
            assert "never-send-this" not in json.dumps(request)
        searches = [row["request"] for row in rows[:3]]
        assert [request["strategy"] for request in searches] == ["beam", "greedy", "exhaustive"]
        assert all((r["max_depth"], r["beam_width"], r["exhaustive_limit"]) == (6, 64, 20_000) for r in searches)
        assert rows[-1]["request"]["actions"] == [actual]
        assert observed == original
    finally:
        client.close()


def test_noncombat_unsettled_and_disabled_never_start_a_process(monkeypatch):
    """Skip work outside reviewed stable combat rather than slowing the model."""
    def forbidden_start(*args, **kwargs):
        """Fail if any ineligible observation reaches process creation."""
        raise AssertionError("No process may start")

    monkeypatch.setattr("sts2_agent.simulation.subprocess.Popen", forbidden_start)
    cases = []
    for field, value in (("screen", "MAP"), ("supported", False)):
        frame = public_frame()
        frame["state"][field] = value
        cases.append(frame)
    for field, value in (("actions_settled", False), ("snapshot_stable", False),
                         ("can_use_combat_actions", False), ("modal_open", True), ("is_paused", True)):
        frame = public_frame()
        frame["state"]["combat"]["action_readiness"][field] = value
        cases.append(frame)
    for frame in cases:
        client = CombatSimulator({"simulation": {"enabled": True}}, ROOT)
        assert client.advise(frame)["status"] == "skipped"
        assert client.predict(frame, {"type": "end_turn"})["status"] == "skipped"
        client.close()
    client = CombatSimulator({}, ROOT)
    assert client.advise(public_frame())["diagnostics"] == [{"code": "simulation_disabled"}]


def test_missing_host_closed_client_and_out_of_scope_action_degrade(tmp_path):
    """Missing binaries and unsupported selections provide diagnostics, not exceptions."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    frame = public_frame()
    advice = client.advise(frame)
    assert advice["status"] == "unavailable" and advice["simulation_advice"] is None
    assert advice["diagnostics"][0]["code"] == "simulation_host_missing"
    assert client.predict(frame, {"type": "use_potion", "option_index": 0})["status"] == "unsupported"
    assert client.predict(frame, {"type": "play_card", "card_index": 999})["status"] == "unsupported"
    stopped = client.predict(frame, {"type": "end_turn"})
    assert stopped["status"] == "unavailable"
    assert stopped["diagnostics"][0]["code"] == "simulation_host_missing"
    client.close()
    assert client.advise(frame)["diagnostics"] == [{"code": "simulation_closed"}]


def test_default_backend_does_not_silently_use_an_available_legacy_host(tmp_path):
    """A missing Mentor deployment cannot turn into unlabelled results from another rule engine."""
    legacy = tmp_path / HOST_DLL
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"not executed")
    runtime = tmp_path / ".tools/dotnet/dotnet.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"not executed")
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    try:
        audit = client.advise(public_frame())
        assert audit["backend"] == "mentor" and audit["status"] == "unavailable"
        assert audit["diagnostics"][0]["code"] == "simulation_host_missing"
        assert client._process is None
    finally:
        client.close()


def test_calculation_only_never_starts_rules_or_spends_simulation_allowance(tmp_path):
    """The experiment mode overrides an accidentally enabled simulator setting."""
    config = {"simulation": {"enabled": True}, "policy": {"deliberation": "calculation_only"}}
    client = CombatSimulator(config, tmp_path, command=[sys.executable, "-c", "raise RuntimeError('must not run')"])
    try:
        frame = public_frame()
        assert client.advise(frame)["status"] == "skipped"
        assert client.evaluate(frame, [{"id": "none", "actions": []}])["status"] == "skipped"
        assert client.predict(frame, {"type": "end_turn"})["status"] == "skipped"
        assert client.rule_identity() == {"backend": "disabled", "protocol_version": 1}
        assert client._process is None and not (tmp_path / "local").exists()
    finally:
        client.close()


def test_rule_identity_uses_artifacts_and_semantic_provenance_not_build_time(tmp_path):
    """A formatting or timestamp edit is stable, while a different executable invalidates caches."""
    artifact = tmp_path / HOST_MENTOR
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rules-one")
    manifest = artifact.with_name("source-provenance.json")
    manifest.write_text(json.dumps({"upstream_commit": "pinned", "built_at": "yesterday"}), encoding="utf-8")
    first = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    first_identity = first.rule_identity()
    manifest.write_text(json.dumps({"built_at": "today", "upstream_commit": "pinned"}, indent=4), encoding="utf-8")
    second = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    assert first_identity == second.rule_identity()
    artifact.write_bytes(b"rules-two")
    changed = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    assert changed.rule_identity() != first_identity
    assert first.rule_identity() == changed.rule_identity()
    first.close()
    second.close()
    changed.close()


def test_search_and_samples_remain_charged_across_backend_revisions(tmp_path):
    """One shared episode has distinct finite counters and changing the backend refunds neither."""
    client, log = make_client(tmp_path)
    frame = public_frame()
    command = client._command
    try:
        assert client.advise(frame)["search_budget"]["requests_used"] == 3
        assert client.evaluate(frame, [{"id": "first", "actions": []}], samples=3, rollout=False)["budget"]["paths_used"] == 3
    finally:
        client.close()
    restored = CombatSimulator({"simulation": {"enabled": True, "backend": "evangambit"}},
                               tmp_path, command=command)
    try:
        assert restored.advise(frame)["search_budget"]["requests_used"] == 6
        assert restored.evaluate(frame, [{"id": "second", "actions": []}], samples=4, rollout=False)["budget"]["paths_used"] == 7
        exhausted = restored.advise(frame)
        assert exhausted["simulation_advice"] is None
        assert any(item["code"] == "search_episode_budget_exhausted" for item in exhausted["diagnostics"])
        assert len(log.read_text(encoding="utf-8").splitlines()) == 8
        ledger = json.loads(next((tmp_path / "local/simulation-budgets").glob("*.json")).read_text())
        assert ledger["paths_used"] == 7 and ledger["search_requests_used"] == 6
        assert len(ledger["rule_fingerprints"]) == 2
        assert ledger["last_rule_identity"]["backend"] == "evangambit"
    finally:
        restored.close()


@pytest.mark.parametrize("mode", ["unsupported", "bad_handle", "generated_first"])
def test_unsupported_or_inconsistent_plans_never_become_advice(tmp_path, mode):
    """A same-ID card cannot replace the instance identified by the host handle."""
    client, _ = make_client(tmp_path, mode=mode)
    try:
        result = client.advise(public_frame())
        assert result["status"] == "unavailable" and result["simulation_advice"] is None
        assert len(result["results"]) == 3 and result["diagnostics"]
        if mode == "unsupported":
            prediction = client.predict(public_frame(), {"type": "play_card", "card_index": 7, "target_index": 11})
            assert prediction["status"] == "unsupported" and prediction["result"]["ok"] is False
    finally:
        client.close()


@pytest.mark.parametrize("error_kind", ["AttributeError", "ValueError", "secret_seed_12345", {"seed": 12345}])
def test_runtime_failure_summary_retains_safe_code_and_exception_kind(tmp_path, error_kind):
    """An upstream runtime failure stays distinct from unsupported plans without copying truth text."""
    client, _ = make_client(tmp_path, strategy="mcts")
    result = {"ok": False, "errors": ["upstream_mirror_request_failed", "secret_seed_12345"],
              "error_kind": error_kind, "reason": "secret_seed_12345", "message": "hidden draw order"}
    audit = {"results": {"mcts": result}, "diagnostics": []}
    prompt, usable = client._prompt(public_frame(), audit)
    assert prompt is None and usable == 0
    expected = {"code": "upstream_mirror_request_failed", "strategy": "mcts",
                "reason": "upstream_mirror_request_failed", "errors": ["upstream_mirror_request_failed"],
                "stderr_tail": [], "stderr_log": str(client._stderr_log)}
    if error_kind in ("AttributeError", "ValueError"):
        expected["error_kind"] = error_kind
    assert audit["diagnostics"] == [expected]
    assert "secret_seed" not in json.dumps(audit["diagnostics"])
    assert "hidden draw order" not in json.dumps(audit["diagnostics"])
    assert audit["results"]["mcts"] == result


def test_native_terminal_root_diagnostic_is_preserved_without_arbitrary_errors(tmp_path):
    """An empty native root stays identifiable without disclosing state-bearing error messages."""
    client, _ = make_client(tmp_path, strategy="mcts")
    audit = {"results": {"mcts": {"ok": False, "errors": [
        "mcts_search:TerminalRoot", "mcts_search:hidden_draw_order", {"seed": 12345}]}}, "diagnostics": []}
    prompt, usable = client._prompt(public_frame(), audit)
    assert prompt is None and usable == 0
    assert audit["diagnostics"] == [{"code": "strategy_without_plan", "strategy": "mcts",
        "reason": "mcts_search:TerminalRoot", "errors": ["mcts_search:TerminalRoot"]}]


def test_generated_later_card_is_descriptive_without_becoming_an_executable_slot(tmp_path):
    """A deterministic generated continuation keeps the current first action usable."""
    client, _ = make_client(tmp_path, mode="generated_later")
    try:
        audit = client.advise(public_frame())
        primary = audit["simulation_advice"]["strategies"]["beam"]
        assert primary["first_action"] == {"type": "play_card", "card_index": 29, "target_index": 11}
        assert primary["sequence"][1] == {
            "type": "play_card", "card_handle": "s0", "card_id": "STRIKE_IRONCLAD",
            "origin": "simulated_generated_card", "target_index": 11,
        }
    finally:
        client.close()


def test_exhaustive_threshold_keeps_primary_advice(tmp_path):
    """An intentionally skipped control group does not remove completed beam advice."""
    client, _ = make_client(tmp_path, mode="threshold")
    try:
        result = client.advise(public_frame())
        assert result["status"] == "partial"
        exhaustive = result["simulation_advice"]["strategies"]["exhaustive"]
        assert exhaustive == {"status": "skipped", "reason": "exhaustive_terminal_limit_exceeded"}
        assert result["simulation_advice"]["strategies"]["beam"]["first_action"]["card_index"] == 29
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["bad_actions", "nan_score"])
def test_malformed_success_plan_degrades_without_raising_into_controller(tmp_path, mode):
    """Valid JSON cannot turn malformed actions or non-finite scores into model advice."""
    client, _ = make_client(tmp_path, mode=mode)
    try:
        result = client.advise(public_frame())
        assert result["status"] == "unavailable" and result["simulation_advice"] is None
        assert result["diagnostics"][0]["code"] == "simulation_host_invalid_response"
        assert client._process is None
    finally:
        client.close()


def test_later_malformed_comparison_preserves_completed_primary_advice(tmp_path):
    """An invalid control result cannot discard beam and greedy results already received."""
    client, _ = make_client(tmp_path, mode="late_bad_plan")
    try:
        result = client.advise(public_frame())
        assert result["status"] == "partial"
        assert result["simulation_advice"]["strategies"]["beam"]["first_action"]["card_index"] == 29
        assert result["simulation_advice"]["strategies"]["exhaustive"]["status"] == "unavailable"
        assert result["diagnostics"][0]["code"] == "simulation_host_invalid_response"
    finally:
        client.close()


def test_malformed_prediction_events_degrade_without_inventing_prediction(tmp_path):
    """A success envelope without event records must not be accepted or crash the model loop."""
    client, _ = make_client(tmp_path, mode="bad_events")
    try:
        result = client.predict(public_frame(), {"type": "play_card", "card_index": 7, "target_index": 11})
        assert result["status"] == "unavailable" and result["state"] is None
        assert result["diagnostics"][0]["code"] == "simulation_host_invalid_response"
        assert client._process is None
    finally:
        client.close()


def test_stop_only_search_explains_missing_comparison_without_recommending_end_turn(tmp_path):
    """A single projection endpoint cannot validate or choose among public card plays."""
    client, _ = make_client(tmp_path, mode="stop_only")
    try:
        result = client.advise(public_frame())
        assert result["status"] == "unavailable"
        prompt = result["simulation_advice"]
        assert prompt["comparison_available"] is False
        assert "Unable to compare card plays" in prompt["notice"]
        for strategy in prompt["strategies"].values():
            assert strategy["reason"] == "no_supported_card_play_comparison"
            assert "first_action" not in strategy and "score" not in strategy
            assert "optimal_within_supported_horizon" not in strategy
    finally:
        client.close()


def test_timeout_reaps_host_and_next_generation_cannot_consume_old_response(tmp_path, monkeypatch):
    """A late reply belongs to a killed process and cannot be matched to a new query."""
    processes = []
    original_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        """Retain actual child handles to verify the failed generation was reaped."""
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("sts2_agent.simulation.subprocess.Popen", capture_process)
    client, log = make_client(tmp_path, mode="timeout", timeout=0.4)
    try:
        failed = client.advise(public_frame())
        assert failed["status"] == "unavailable"
        assert failed["diagnostics"][0]["code"] == "simulation_host_timeout"
        assert processes[0].poll() is not None and client._process is None
        client.timeout_seconds = 3
        recovered = client.advise(public_frame())
        assert recovered["simulation_advice"] is not None
        assert len(processes) == 2
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        # Windows venv redirectors can have a different PID from the running interpreter.
        old_pid, new_pid = rows[0]["pid"], rows[1]["pid"]
        assert old_pid != new_pid
        assert {result["pid"] for result in recovered["results"].values()} == {new_pid}
        assert [row["pid"] for row in rows] == [old_pid] + [new_pid] * 3
    finally:
        client.close()
    assert processes[-1].poll() is not None


def test_stderr_larger_than_pipe_buffer_is_drained_and_bounded(tmp_path):
    """Verbose host diagnostics cannot deadlock a successful JSON response."""
    client, _ = make_client(tmp_path, mode="stderr")
    try:
        result = client.advise(public_frame())
        assert result["simulation_advice"] is not None
        assert len(client._stderr) <= STDERR_TAIL_LINES
    finally:
        client.close()


@pytest.mark.parametrize("mode, code", [("invalid", "simulation_host_invalid_json"), ("exit", "simulation_host_exited")])
def test_protocol_failures_reap_the_process(tmp_path, mode, code):
    """Malformed output and EOF stop this generation and return safe diagnostics."""
    client, _ = make_client(tmp_path, mode=mode)
    try:
        result = client.advise(public_frame())
        assert result["status"] == "unavailable"
        assert result["diagnostics"][0]["code"] == code
        assert client._process is None
    finally:
        client.close()


@pytest.mark.skipif(not (ROOT / HOST_DLL).is_file(), reason="Isolated host must be built separately")
def test_compiled_host_smoke_uses_public_indices_for_one_selected_action(tmp_path):
    """Verify production IPC against the already validated managed rule core."""
    frame = public_frame()
    command = [str(ROOT / ".tools/dotnet/dotnet.exe"), str(ROOT / HOST_DLL)]
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "evangambit"}},
                             tmp_path, command=command)
    try:
        advice = client.advise(frame)
        assert advice["simulation_advice"] is not None
        assert advice["results"]["exhaustive"]["optimal_within_supported_horizon"] is True
        raw = advice["results"]["beam"]["best"]["state"]
        endpoint = advice["simulation_advice"]["strategies"]["beam"]["endpoint"]
        assert endpoint["enemies"][0]["hp"] == raw["enemies"][0]["hp"]
        assert endpoint["player"]["energy"] == raw["player"]["energy"]
        assert endpoint["horizon"] == "current_player_turn_before_end_turn_effects"
        assert "hand" not in endpoint and "piles" not in endpoint
        actual = {"type": "play_card", "card_index": 7, "target_index": 11}
        result = client.predict(frame, actual)
        assert result["status"] == "predicted"
        assert result["state"]["player"]["energy"] == 2
        assert result["state"]["enemies"][0]["i"] == 11
        assert [(card["handle"], card["card_index"]) for card in result["state"]["hand"]] == [("h1", 29)]
    finally:
        client.close()


def test_end_turn_unknown_draw_uses_hypotheses_without_claiming_real_prediction(tmp_path):
    """Keep strict failure evidence and charge six worlds without replacing the selected action."""
    client, log = make_client(tmp_path, mode="unknown_draw")
    frame = public_frame()
    original = deepcopy(frame)
    action = {"type": "end_turn"}
    try:
        result = client.predict(frame, action)
        assert result["status"] == "hypothetical" and result["status"] != "predicted"
        assert result["action"] == action == {"type": "end_turn"}
        assert result["result"]["ok"] is False
        assert result["result"]["errors"] == [{"error": "unknown_draw_boundary"}]
        assert result["state"] == {"strict_failure_only": True}
        summary = result["uncertainty_evaluation"]
        assert summary["candidates"][0]["completed_samples"] == 6
        assert summary["candidates"][0]["metrics"] == {"mean_hp_loss": 0}
        assert summary["budget"]["paths_used"] == 6
        assert "not_known_next_hand" in result["prediction_kind"]
        assert "hypothetical" in summary["usage"]
        rows = [json.loads(line)["request"] for line in log.read_text(encoding="utf-8").splitlines()]
        assert [row["op"] for row in rows] == ["simulate", "evaluate"]
        assert rows[0]["actions"] == [action]
        assert rows[1]["candidate_plans"] == [{"id": "selected_end_turn", "actions": [action]}]
        assert rows[1]["samples"] == 6 and rows[1]["max_actions"] == 1
        assert all(row["public_state"] == original["state"] for row in rows)
        assert frame == original
        ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
        assert json.loads(ledger.read_text(encoding="utf-8"))["paths_used"] == 6
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["unknown_draw_prefix", "unknown_draw_prefix_partial"])
def test_end_turn_settled_prefix_survives_hypothetical_continuation(tmp_path, mode):
    """Keep exact stage states when subsequent hypothetical samples succeed or fail."""
    client, _ = make_client(tmp_path, mode=mode)
    try:
        result = client.predict(public_frame(), {"type": "end_turn"})
        assert result["status"] == "partial"
        prefix = result["deterministic_prefix"]
        assert prefix == result["result"]["turn_stages"]
        assert [point["stage"] for point in prefix["checkpoints"]] == [
            "player_turn_end", "enemy_turn_end", "before_next_player_draw"
        ]
        assert [point["state"]["player"]["hp"] for point in prefix["checkpoints"]] == [40, 33, 35]
        assert prefix["boundary"] == {"stage": "next_player_draw", "reason": "unknown_draw_boundary"}
        assert result["result"]["ok"] is False
        assert result["state"] == {"strict_failure_only": True}
        summary = result["uncertainty_evaluation"]["candidates"][0]
        assert summary["completed_samples"] == (5 if mode.endswith("partial") else 6)
        assert "deterministic_turn_prefix" in result["prediction_kind"]
    finally:
        client.close()


def test_end_turn_failure_before_first_checkpoint_has_no_prefix(tmp_path):
    """An unresolved first stage cannot advertise a deterministic result."""
    client, _ = make_client(tmp_path, mode="early_turn_failure")
    try:
        result = client.predict(public_frame(), {"type": "end_turn"})
        assert result["status"] == "unsupported"
        assert "deterministic_prefix" not in result
        assert "uncertainty_evaluation" not in result
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["partial", "empty_metrics", "budget"])
def test_end_turn_incomplete_hypotheses_or_insufficient_paths_stay_unsupported(tmp_path, failure):
    """Incomplete metrics and shared path exhaustion cannot become a successful prediction."""
    mode = "unknown_draw" if failure == "budget" else "unknown_draw_" + failure
    client, log = make_client(tmp_path, mode=mode)
    frame = public_frame()
    try:
        if failure == "budget":
            plans = [{"id": "spend", "actions": []}]
            for samples in (12, 12, 12, 7):
                client.evaluate(frame, plans, samples=samples, rollout=False)
        before = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
        result = client.predict(frame, {"type": "end_turn"})
        assert result["status"] == "unsupported"
        assert result["result"]["ok"] is False
        assert result["state"] == {"strict_failure_only": True}
        assert result["action"] == {"type": "end_turn"}
        assert any(item["code"] == "end_turn_hypotheses_incomplete_not_a_prediction"
                   for item in result["diagnostics"])
        summary = result["uncertainty_evaluation"]
        if failure == "budget":
            assert summary["status"] == "budget_exhausted"
            assert summary["budget"]["paths_used"] == 43
            assert summary["candidates"] == []
        else:
            assert summary["budget"]["paths_used"] == 6
            candidate = summary["candidates"][0]
            assert candidate["completed_samples"] == (5 if failure == "partial" else 6)
            if failure == "empty_metrics":
                assert candidate["metrics"] == {}
        after = len(log.read_text(encoding="utf-8").splitlines())
        assert after - before == (1 if failure == "budget" else 2)
    finally:
        client.close()


def test_fixed_plan_evaluation_uses_only_public_inputs_and_persists_episode_budget(tmp_path):
    """Changing observation IDs or recreating a client cannot refund a shared episode's paths."""
    frame = public_frame()
    plans = [{"id": "first", "actions": [{"type": "play_card", "card_index": 7, "target_index": 11}]},
             {"id": "later-choice", "actions": [{"type": "select_card", "choice_index": 0},
                                                 {"type": "play_card", "card_id": "STRIKE_IRONCLAD",
                                                  "target_index": 11}]}]
    client, log = make_client(tmp_path)
    client.root = tmp_path
    original = deepcopy(plans)
    try:
        first = client.evaluate(frame, plans, scenario_seed=19, rollout=False)
        assert first["status"] == "evaluated"
        assert first["budget"] == {"path_limit": 48, "requested_paths": 24,
                                   "paths_used": 24, "remaining_paths": 24}
        request = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["request"]
        assert request["candidate_plans"] == plans and plans == original
        assert request["public_state"] == frame["state"]
        assert request["scenario_seed"] == 19
        assert "never-send-this" not in json.dumps(request) and "raw" not in request and "seed" not in request
    finally:
        client.close()
    restored, log = make_client(tmp_path)
    restored.root = tmp_path
    frame["observation_id"] = "replanned-same-episode"
    try:
        second = restored.evaluate(frame, plans, rollout=False)
        assert second["budget"]["paths_used"] == 48
        stopped = restored.evaluate(frame, plans, samples=1, rollout=False)
        assert stopped["status"] == "budget_exhausted"
        assert len(log.read_text(encoding="utf-8").splitlines()) == 2
        frame["decision_episode_id"] = "a-new-public-decision"
        assert restored.evaluate(frame, plans, samples=1, rollout=False)["budget"]["paths_used"] == 2
    finally:
        restored.close()


def test_failed_evaluation_remains_charged_and_corrupt_budget_is_not_reset(tmp_path):
    """Malformed host replies and unreadable ledgers never open an uncharged retry path."""
    client, log = make_client(tmp_path, mode="bad_evaluation")
    client.root = tmp_path
    plans = [{"id": "declared", "actions": []}]
    try:
        failed = client.evaluate(public_frame(), plans, samples=3, rollout=False)
        assert failed["status"] == "unavailable"
        assert failed["budget"]["paths_used"] == 3
        assert failed["diagnostics"][0]["code"] == "simulation_host_invalid_response"
        path = next((tmp_path / "local/simulation-budgets").glob("*.json"))
        assert json.loads(path.read_text(encoding="utf-8"))["paths_used"] == 3
        path.write_text("broken ledger", encoding="utf-8")
        denied = client.evaluate(public_frame(), plans, samples=3, rollout=False)
        assert denied["status"] == "unavailable"
        assert denied["diagnostics"] == [{"code": "evaluation_budget_unavailable"}]
        assert path.read_text(encoding="utf-8") == "broken ledger"
        assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        client.close()


@pytest.mark.parametrize("plans,limits", [
    ([{"id": "a", "actions": []}] * 2, {}),
    ([{"id": str(index), "actions": []} for index in range(65)], {}),
    ([{"id": "a", "actions": [{"type": "end_turn"}] * 65}], {}),
    ([{"id": "a", "actions": [{"type": "play_card", "seed": "hidden"}]}], {}),
    ([{"id": "a", "actions": [{"type": "select_card", "choice_index": True}]}], {}),
    ([{"id": "a", "actions": []}], {"samples": 257}),
    ([{"id": "a", "actions": []}], {"scenario_seed": True}),
    ([{"id": "a", "actions": []}], {"max_events": 513}),
])
def test_invalid_evaluation_limits_and_private_action_fields_never_dispatch(tmp_path, plans, limits):
    """Reject excess work and unexpected action data without silently shortening declared plans."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    try:
        result = client.evaluate(public_frame(), plans, **limits)
        assert result["status"] == "unsupported"
        diagnostic = result["diagnostics"][0]
        assert diagnostic["code"] == "invalid_evaluation_plans_or_limits"
        assert isinstance(diagnostic.get("reason"), str) and diagnostic["reason"]
        assert not (tmp_path / "local").exists() and client._process is None
    finally:
        client.close()


def test_plan_rejection_names_the_specific_problem(tmp_path):
    """A rejected plan explains itself so the model can correct it instead of retrying blind."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    try:
        marker = client.evaluate(public_frame(), [{"id": "a", "actions": [
            {"type": "play_card", "card_index": 0}, {"type": "stop_projection"}]}])
        assert marker["diagnostics"][0] == {"code": "invalid_evaluation_plans_or_limits",
            "reason": "action_type_not_simulated", "plan_index": 0, "action_index": 1,
            "type": "stop_projection", "allowed_types": sorted(SIMULATED_ACTION_FIELDS)}
        missing = client.evaluate(public_frame(), [{"id": "a", "actions": [{"card_index": 0}]}])
        assert missing["diagnostics"][0]["reason"] == "action_type_missing"
        extra = client.evaluate(public_frame(), [{"id": "a", "actions": [
            {"type": "play_card", "seed": "hidden"}]}])
        assert extra["diagnostics"][0]["reason"] == "action_field_not_allowed"
        assert extra["diagnostics"][0]["fields"] == ["seed"]
    finally:
        client.close()


def test_potion_prediction_preserves_the_selected_public_slot(tmp_path):
    """The existing public legality check also governs one-step potion projections."""
    frame = public_frame()
    frame["available_actions"].append("use_potion")
    frame["state"]["run"]["potions"] = [{"i": 5, "potion_id": "FIRE_POTION", "usable": True,
                                          "targets": [11]}]
    selected = {"type": "use_potion", "option_index": 5, "target_index": 11}
    client, log = make_client(tmp_path)
    try:
        result = client.predict(frame, selected)
        assert result["status"] == "predicted"
        assert json.loads(log.read_text(encoding="utf-8"))["request"]["actions"] == [selected]
        frame["state"]["run"]["potions"][0]["usable"] = False
        assert client.predict(frame, selected)["status"] == "unsupported"
        assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        client.close()


def test_live_advice_adds_fixed_draw_preview_without_ranking_sampled_hands(tmp_path):
    """Only declared public one-step actions reach sampling, and the prompt remains concise."""
    frame = public_frame()
    frame["state"]["combat"]["hand"][0]["card_id"] = "POMMEL_STRIKE"
    client, log = make_client(tmp_path)
    client.root = tmp_path
    try:
        audit = client.advise(frame)
        requests = [json.loads(line)["request"] for line in log.read_text(encoding="utf-8").splitlines()]
        assert [request["op"] for request in requests] == ["search", "search", "search", "evaluate"]
        assert requests[-1]["candidate_plans"] == [{"id": "hand:0:target:11", "actions": [
            {"type": "play_card", "card_index": 7, "target_index": 11}]}]
        preview = audit["simulation_advice"]["bounded_action_previews"]
        assert len(preview["candidates"]) == 1
        candidate = preview["candidates"][0]
        assert candidate["sample_count"] == 6
        assert "samples" not in candidate and "local_audit_only" not in json.dumps(preview)
        assert "not ranked" in preview["usage"] and "hypothetical" in preview["usage"]
        assert len(audit["bounded_evaluation"]["result"]["candidates"][0]["samples"]) == 6
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["needs_decision", "bad_evaluation"])
def test_unfinished_or_failed_previews_preserve_search_and_do_not_invent_completed_metrics(tmp_path, mode):
    """A pending choice remains explicitly partial while transport failures keep completed advice."""
    frame = public_frame()
    frame["state"]["combat"]["hand"][0]["card_id"] = "BURNING_PACT"
    client, _ = make_client(tmp_path, mode=mode)
    client.root = tmp_path
    try:
        audit = client.advise(frame)
        prompt = audit["simulation_advice"]
        assert prompt["strategies"]["beam"]["first_action"]["card_index"] == 29
        preview = prompt["bounded_action_previews"]
        if mode == "bad_evaluation":
            assert preview["status"] == "unavailable" and preview["candidates"] == []
            assert preview["budget"]["paths_used"] == 6
        else:
            candidate = preview["candidates"][0]
            assert candidate["status"] == "needs_decision" and candidate["metrics"] is None
            assert candidate["completed_samples"] == 0
            endpoint = candidate["illustrative_sample"]["endpoint"]
            assert endpoint["meaning"] == "partial_resolution_at_reported_stop_not_a_completed_action"
    finally:
        client.close()


@pytest.mark.skipif(not (ROOT / HOST_DLL).is_file(), reason="Isolated host must be built separately")
def test_compiled_host_draw_preview_and_declared_revealed_card_plan(tmp_path):
    """Real production IPC samples a public pile and can execute a predeclared revealed-card action."""
    frame = public_frame()
    state = frame["state"]
    state["combat"]["hand"] = [state["combat"]["hand"][0]]
    state["combat"]["hand"][0].update(card_id="SHRUG_IT_OFF", line="Shrug It Off [1]: Gain 8 Block. Draw 1.",
                                         target=None, targets=[], upgraded=False)
    state["combat"].update(draw=[{"card_ids": ["STRIKE_IRONCLAD"], "line": "Strike*1 [1]",
                                  "keywords": [], "mods": [], "upgraded": False}], discard=[], exhaust=[])
    state["combat"]["enemies"][0].update(hp="100/100", block=0, powers=[])
    command = [str(ROOT / ".tools/dotnet/dotnet.exe"), str(ROOT / HOST_DLL)]
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "evangambit"}}, tmp_path, command=command)
    original = deepcopy(frame)
    try:
        audit = client.advise(frame)
        preview = audit["simulation_advice"]["bounded_action_previews"]
        candidate = preview["candidates"][0]
        assert candidate["status"] == "estimated" and candidate["completed_samples"] == 6
        assert candidate["metrics"]["block"] == {"min": 8, "max": 8, "mean": 8}
        assert candidate["metrics"]["hand_size"] == {"min": 1, "max": 1, "mean": 1}
        assert candidate["metrics"]["hand_card_counts"] == [{
            "card_id": "STRIKE_IRONCLAD", "upgraded": False, "count": {"min": 1, "max": 1, "mean": 1},
        }]
        assert preview["rule_snapshot"] and preview["assumptions"]
        assert "not real draw probabilities" in preview["usage"]
        assert candidate["illustrative_sample"]["status"] == "completed"
        assert candidate["illustrative_sample"]["endpoint"]["player"]["block"] == 8
        evaluated = client.evaluate(frame, [{"id": "draw_then_play", "actions": [
            {"type": "play_card", "card_index": 7},
            {"type": "play_card", "card_id": "STRIKE_IRONCLAD", "target_index": 11},
        ]}], samples=2, rollout=False)
        sample = evaluated["result"]["candidates"][0]["samples"][0]
        assert sample["status"] == "completed", sample
        assert sample["state"]["enemies"][0]["hp"]["current"] == 94
        assert frame == original
        budget = next((tmp_path / "local/simulation-budgets").glob("*.json"))
        assert json.loads(budget.read_text(encoding="utf-8"))["paths_used"] == 8
    finally:
        client.close()


@pytest.mark.skipif(not (ROOT / HOST_DLL).is_file(), reason="Isolated host must be built separately")
def test_compiled_host_choice_preview_and_public_potion_prediction(tmp_path):
    """Pending exhaust decisions stay explicit, and potion prediction consumes the exact visible slot."""
    frame = public_frame()
    state = frame["state"]
    state["combat"]["hand"][0].update(card_id="BURNING_PACT", line="Burning Pact [1]",
                                         target=None, targets=[], upgraded=False)
    state["combat"].update(draw=[{"card_ids": ["STRIKE_IRONCLAD"], "line": "Strike*3 [1]",
                                  "keywords": [], "mods": [], "upgraded": False}], discard=[], exhaust=[])
    state["combat"]["enemies"][0].update(hp="100/100", block=0, powers=[])
    state["available_actions"].append("use_potion")
    frame["available_actions"].append("use_potion")
    state["run"]["potions"] = [{"i": 5, "potion_id": "FIRE_POTION", "usable": True,
                                "occupied": True, "targets": [11], "description": "Deal 20 damage."}]
    command = [str(ROOT / ".tools/dotnet/dotnet.exe"), str(ROOT / HOST_DLL)]
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "evangambit"}}, tmp_path, command=command)
    try:
        audit = client.advise(frame)
        candidate = audit["simulation_advice"]["bounded_action_previews"]["candidates"][0]
        assert candidate["status"] == "needs_decision"
        assert candidate["metrics"] is None and candidate["completed_samples"] == 0
        sample = audit["bounded_evaluation"]["result"]["candidates"][0]["samples"][0]
        assert sample["state"]["pending_choice"]["candidates"][0]["card_id"] == "STRIKE_IRONCLAD"
        resolved = client.evaluate(frame, [{"id": "declare_exhaust", "actions": [
            {"type": "play_card", "card_index": 7}, {"type": "select_card", "choice_index": 0},
        ]}], samples=1, rollout=False)
        assert resolved["result"]["candidates"][0]["completed_samples"] == 1
        assert resolved["result"]["candidates"][0]["samples"][0]["state"]["pending_choice"] is None
        predicted = client.predict(frame, {"type": "use_potion", "option_index": 5, "target_index": 11})
        assert predicted["status"] == "predicted", predicted
        assert predicted["state"]["enemies"][0]["hp"]["current"] == 80
        assert predicted["state"]["potions"][0]["i"] == 5
        assert predicted["state"]["potions"][0]["occupied"] is False
        assert state["run"]["potions"][0]["occupied"] is True
    finally:
        client.close()



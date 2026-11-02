"""Verify phase ownership, component transport and zero-API upstream admission with offline hosts."""

from copy import deepcopy
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from sts2_agent.session import DashboardSession, SessionError
from sts2_agent.simulation import CombatSimulator
from test_session_controls import FakeProvider, make_session
from test_simulation import make_client, public_frame


# Actual trained assets and the built host are optional for ordinary offline checkout tests.
NETWORK_HOST = Path(__file__).resolve().parents[1] / "local/simulation/mentor/mentor-simulation.exe"


def owned_session(tmp_path, *, phase="combat", mode="normal"):
    """Bind the actual JSONL simulator to a controller whose bridge and language model are offline."""
    session, bridge, provider = make_session(tmp_path)
    client, log = make_client(tmp_path, strategy="mcts", mode=mode)
    session.simulator = client
    bridge.state = public_frame()["state"] if phase == "combat" else {
        "protocol_version": 1, "screen": "EVENT", "turn": 1, "supported": True,
        "available_actions": ["choose_event_option"], "run": {"act_id": "act1", "floor": 2},
        "event": {"options": [{"i": 7, "enabled": True}]}}
    session.configure_simulation({phase + "_decision_mode": "upstream"})
    return session, bridge, provider, client, log


def test_direct_combat_admits_mcts_action_without_api_or_spending_instructions(tmp_path):
    """An exhausted API allowance cannot block direct MCTS, and queued prose is never silently consumed."""
    session, bridge, provider, client, _ = owned_session(tmp_path)
    try:
        session.instructions.add("A queued language-model-only instruction", "run")
        session._state["metrics"]["requests_used"] = session._state["metrics"]["max_requests"]
        provider.decide = Mock(side_effect=AssertionError("Direct ownership must never invoke an API"))
        session.command("resume-direct", "resume")
        session._tick()
        provider.decide.assert_not_called()
        assert bridge.executions[0]["action"] == {"type": "play_card", "card_index": 29, "target_index": 11}
        assert session.snapshot()["metrics"]["actions_completed"] == 1
        assert session.snapshot()["metrics"]["requests_used"] == 30
        assert session.snapshot()["decision"]["decision_source"] == "upstream_mcts"
        assert session.instructions.snapshot()[0]["status"] == "queued"
    finally:
        client.close()


@pytest.mark.skipif(not NETWORK_HOST.is_file(), reason="The trained network host must be built separately")
def test_real_combat_model_identity_survives_decision_pending_and_execution(tmp_path):
    """Real learned88 metadata follows one direct action through a blocked native POST and the durable journal."""
    session, bridge, provider, fake_client, _ = owned_session(tmp_path)
    fake_client.close()
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path, command=[str(NETWORK_HOST)])
    session.simulator = client
    session.configure_simulation({"combat_decision_mode": "upstream", "search_strength": 1,
        "rollout_policy": "heuristic", "action_prior": "off", "value_model": "learned", "search_profile": "base"})
    provider.decide = Mock(side_effect=AssertionError("Real direct MCTS must never invoke a language model"))
    bridge.block_execute = True
    errors = []
    def tick():
        """Keep worker failures visible to the assertion thread while the pending envelope is inspected."""
        try:
            session._tick()
        except BaseException as error:
            errors.append(error)
    session.command("real-direct-identity", "resume")
    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert bridge.executing.wait(5), errors
        decision = session.snapshot()["decision"]
        identity = decision["model_identity"]
        assert identity["components"]["value_model"]["active"] is True
        assert identity["components"]["value_model"]["native_input_dim"] == 88
        assert identity["components"]["value_model"]["sha256"]
        assert decision["decision_counter"] == decision["decision_identity"]["counter"] == 0
        assert decision["decision_identity"]["next_counter"] == 1
        assert decision["decision_identity"]["counter_source"] == "session_public_decision_episodes"
        pending = json.loads((tmp_path / "local/pending-action.json").read_text(encoding="utf-8"))
        execution = json.loads((session._journal.directory / "execution-requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert pending["model_identity"] == execution["model_identity"] == identity
        assert pending["decision_id"] == execution["decision_id"] == decision["decision_id"]
        assert pending["decision_identity"] == execution["decision_identity"] == decision["decision_identity"]
        provider.decide.assert_not_called()
    finally:
        bridge.finish_execution.set()
        worker.join(5)
        client.close()
    assert not worker.is_alive() and errors == []
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    assert session.snapshot()["metrics"]["requests_used"] == 0


def test_direct_unsupported_has_no_language_model_fallback(tmp_path):
    """A failed upstream search is a visible controller error rather than permission for a paid fallback."""
    session, bridge, provider, client, _ = owned_session(tmp_path, mode="unsupported")
    try:
        provider.decide = Mock(side_effect=AssertionError("Unsupported direct mode must not use an API"))
        session.command("resume-direct", "resume")
        with pytest.raises(SessionError, match="未调用语言模型"):
            session._tick()
        provider.decide.assert_not_called()
        assert bridge.executions == []
        assert session.snapshot()["metrics"]["requests_used"] == 0
        assert session.snapshot()["decision_pending"] is False
    finally:
        client.close()


def test_combat_counter_reuses_retry_and_restart_then_advances_across_encounters(tmp_path):
    """Persist a reserved wrapper slot before inference and never refund or duplicate it on redecision."""
    session, bridge, provider, client, log = owned_session(tmp_path)
    try:
        session.command("counter-generation", "resume")
        obs, token = session._observe(settled=True)
        session._generate(obs, token)
        session._generate(obs, token)
        restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
        restored.simulator = client
        restored.command("counter-resume", "resume")
        restored._generate(*restored._observe(settled=True))
        bridge.state["run"]["floor"] += 1
        bridge.state["turn"] += 1
        bridge.token = "next-encounter"
        restored._generate(*restored._observe(settled=True))
        requests = [json.loads(line)["request"] for line in log.read_text().splitlines()
                    if json.loads(line)["request"]["op"] == "search"]
        assert [request["decision_counter"] for request in requests] == [0, 0, 0, 1]
        assert all(request["combat_decision_mode"] == "upstream" for request in requests)
        assert all("benchmark_run_seed" not in request and "scenario_seed" not in request for request in requests)
        assert restored.snapshot()["next_combat_decision_counter"] == 2
        assert restored.snapshot()["decision"]["decision_identity"]["counter"] == 1
        assert restored.snapshot()["metrics"]["requests_used"] == 0
    finally:
        client.close()


def test_direct_forced_root_uses_wrapper_counter_without_inventing_an_estimate(tmp_path):
    """A legal forced end-turn bypasses automatic execution but retains zero-work and null-value facts."""
    session, bridge, provider, client, log = owned_session(tmp_path, mode="forced")
    try:
        session.config["automatic"]["enabled"] = True
        bridge.state["combat"]["hand"] = []
        bridge.state["combat"]["player"]["cards_played_this_turn"] = 1
        bridge.state["run"]["potions"] = []
        bridge.state["available_actions"] = ["end_turn"]
        provider.decide = Mock(side_effect=AssertionError("Forced direct root must not call a language model"))
        session.command("forced-wrapper", "resume")
        session._tick()
        decision = session.snapshot()["decision"]
        assert decision["decision_source"] == "upstream_mcts" and decision["decision_counter"] == 0
        advice = session.snapshot()["simulation"]["simulation_advice"]["strategies"]["mcts"]
        assert advice["forced_public_action"] is True and advice["score"] is None
        assert advice["mcts"]["visits"] == 0 and advice["mcts"]["p_win"] is None
        assert advice["model_retains_final_choice"] is False
        assert bridge.executions[0]["action"] == {"type": "end_turn"}
        execution = json.loads((session._journal.directory / "execution-requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert execution["decision_counter"] == 0
        assert execution["decision_identity"]["counter"] == 0
        provider.decide.assert_not_called()
    finally:
        client.close()


def test_old_run_counter_recovers_only_observed_unique_episodes_when_needed(tmp_path):
    """Legacy recovery is lazy and identifies its partial public-history provenance rather than a native seed."""
    session, bridge, provider, client, _ = owned_session(tmp_path)
    try:
        session.command("counter-generation", "resume")
        obs, token = session._observe(settled=True)
        session._generate(obs, token)
        session._generate(obs, token)
        for key in ("next_combat_decision_counter", "combat_counter_episode", "combat_decision_counter_source"):
            session._state.pop(key)
        session._persist()
        restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
        restored.simulator = client
        assert "next_combat_decision_counter" not in restored.snapshot()
        restored.command("counter-resume", "resume")
        restored._generate(*restored._observe(settled=True))
        assert restored.snapshot()["next_combat_decision_counter"] == 1
        assert restored.snapshot()["decision"]["decision_counter"] == 0
        assert restored.snapshot()["decision"]["decision_counter_source"] == "historical_public_episode_recovery"
        assert restored.snapshot()["combat_counter_recovery"]["unobserved_native_or_manual_calls_known"] is False
        assert make_session(tmp_path / "new-run")[0].snapshot()["next_combat_decision_counter"] == 0
    finally:
        client.close()


def test_run_selection_context_commits_only_after_native_acceptance(tmp_path):
    """A proposed or unknown POST cannot overwrite the previously accepted multi-step policy intention."""
    session, bridge, provider, client, _ = owned_session(tmp_path, phase="run")
    try:
        session._observe(settled=True)
        old = {"scope": session._run_policy_scope(session._state["observation"]), "context": {"intent": "prior"}}
        session._state["upstream_selection_context"] = deepcopy(old)
        bridge.execute_status = "unknown"
        session.command("pending-policy-context", "resume")
        session._tick()
        assert session._state["upstream_selection_context"] == old
        bridge.receipt_status = "completed"
        session._tick()
        assert session._state["upstream_selection_context"]["context"] == {"intent": "shop_remove"}
        assert len(bridge.executions) == 1
    finally:
        client.close()


def test_accepted_potion_replacement_claim_preempts_generic_reward_housekeeping(tmp_path):
    """An accepted discard carries the exact target row through a fresh root instead of taking another potion."""
    session, bridge, provider, client, _ = owned_session(tmp_path, phase="run")
    try:
        session.config["automatic"]["enabled"] = True
        bridge.state.update(screen="REWARD", available_actions=["claim_reward", "proceed"],
            reward={"can_proceed": True, "rewards": [
                {"i": 3, "reward_type": "POTION", "potion_id": "STRENGTH_POTION", "claimable": True,
                 "potion_can_receive": True},
                {"i": 9, "reward_type": "POTION", "potion_id": "FIRE_POTION", "claimable": True,
                 "potion_can_receive": True}]})
        obs, _ = session._observe(settled=True)
        context = {"intent": "reward_sequence", "claim_after_discard": {"row_key": "POTION:FIRE_POTION"}}
        session._state["upstream_selection_context"] = {"scope": session._run_policy_scope(obs), "context": context}
        client._request = Mock(return_value={"ok": True, "status": "forced_public_action",
            "best_action": {"type": "claim_reward", "option_index": 9}, "next_context": {"intent": "reward_sequence"},
            "policy_steps": [{"source": "completed_discard_public_followup", "neural_inference": False,
                "native_action": {"type": "claim_reward", "option_index": 9}}]})
        provider.decide = Mock(side_effect=AssertionError("Direct replacement continuation must not call a language model"))
        session.command("finish-replacement", "resume")
        session._tick()
        assert client._request.call_args.args[0]["selection_context"] == context
        assert bridge.executions[0]["action"] == {"type": "claim_reward", "option_index": 9}
        assert session._state["upstream_selection_context"]["context"] == {"intent": "reward_sequence"}
        assert session.snapshot()["simulation"]["simulation_advice"]["policy_status"] == "forced_public_action"
        assert session.snapshot()["simulation"]["simulation_advice"]["policy_steps"][0]["source"] == "completed_discard_public_followup"
        provider.decide.assert_not_called()
    finally:
        client.close()


def test_assisted_combat_keeps_the_language_model_final_choice(tmp_path):
    """Assistance supplies an upstream proposal but submits the model's independently selected legal action."""
    session, bridge, provider, client, _ = owned_session(tmp_path)
    try:
        session.configure_simulation({"combat_decision_mode": "assist"})
        session.command("resume-assist", "resume")
        session._tick()
        assert len(provider.calls) == 1
        assert provider.calls[0]["observation"]["simulation_advice"]["strategies"]["mcts"]["first_action"]["type"] == "play_card"
        assert bridge.executions[0]["action"] == {"type": "end_turn"}
    finally:
        client.close()


def test_direct_run_policy_uses_public_action_and_scoped_selection_context(tmp_path):
    """The run advisor shares normal admission, preserves its public context and clears it at a new floor."""
    session, bridge, provider, client, log = owned_session(tmp_path, phase="run")
    try:
        provider.decide = Mock(side_effect=AssertionError("Direct run policy must never invoke an API"))
        session.command("resume-run", "resume")
        session._tick()
        provider.decide.assert_not_called()
        assert bridge.executions[0]["action"] == {"type": "choose_event_option", "option_index": 7}
        assert session.snapshot()["decision"]["decision_source"] == "upstream_ppo"
        assert session.snapshot()["network_identity"]["model_identity"] == {"source": "offline-test-double"}
        assert session._state["upstream_selection_context"]["context"] == {"intent": "shop_remove"}
        request = next(json.loads(line)["request"] for line in log.read_text().splitlines()
                       if json.loads(line)["request"]["op"] == "policy_evaluate")
        assert request["public_state"]["screen"] == "EVENT"
        assert request["selection_context"] is None and "internal_state" not in request
        assert request["public_uncertainty"] == {"worlds": 32, "full_unlock_catalogue": False}
        bridge.state["run"]["floor"] = 3
        session._observe(settled=True)
        assert "upstream_selection_context" not in session._state
    finally:
        client.close()


@pytest.mark.parametrize("owner", ["llm", "assist"])
def test_run_model_ownership_keeps_its_choice_and_discards_unselected_policy_context(tmp_path, owner):
    """Disabled advice makes no policy request; assistance cannot carry an intention the model declined."""
    session, bridge, provider, client, log = owned_session(tmp_path, phase="run")
    bridge.state["event"]["options"].append({"i": 8, "enabled": True})
    original_decide = provider.decide
    def decide(obs, memory, *, instructions=None, cancel_event=None):
        """Exercise the normal offline API accounting, then choose the other legal public event option."""
        result = original_decide(obs, memory, instructions=instructions, cancel_event=cancel_event)
        result["action"] = {"type": "choose_event_option", "option_index": 8}
        return result
    provider.decide = decide
    try:
        session.configure_simulation({"run_decision_mode": owner})
        session.command("resume-model-run", "resume")
        session._tick()
        assert len(provider.calls) == 1 and session.snapshot()["metrics"]["requests_used"] == 1
        assert bridge.executions[0]["action"] == {"type": "choose_event_option", "option_index": 8}
        assert session._state.get("upstream_selection_context", {}).get("context") is None
        if owner == "assist":
            assert provider.calls[0]["observation"]["simulation_advice"]["best_action"]["option_index"] == 7
            assert json.loads(log.read_text().splitlines()[0])["request"]["op"] == "policy_evaluate"
        else:
            assert not log.exists() and "simulation_advice" not in provider.calls[0]["observation"]
    finally:
        client.close()


@pytest.mark.parametrize("enabled,candidate,expected", [(True, "mcts", "assist"), (True, "model", "llm"),
                                                        (False, "mcts", "llm"), (False, "model", "llm")])
def test_legacy_settings_migrate_in_memory_without_rewriting_preferences(tmp_path, enabled, candidate, expected):
    """Old disabled configurations stay basic, while fractional legacy preferences survive a read-only migration."""
    session, bridge, _ = make_session(tmp_path)
    saved = {"read_access": "public", "candidate_source": candidate, "search_strength": 2.431234567}
    path = tmp_path / "local/simulation-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved))
    original = path.read_bytes()
    config = deepcopy(session.config)
    config["simulation"] = {"enabled": enabled}
    restored = DashboardSession(config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.simulator.settings["combat_decision_mode"] == expected
    assert restored.simulator.settings["run_decision_mode"] == "llm"
    assert restored.simulator.settings["search_strength"] == saved["search_strength"]
    assert path.read_bytes() == original
    restored.configure_simulation({"run_decision_mode": "upstream"})
    assert restored.simulator.enabled is True


def test_legacy_candidate_alias_cannot_replace_an_explicit_new_owner(tmp_path):
    """A newer explicit upstream config survives an older preference file containing only candidate_source."""
    session, bridge, _ = make_session(tmp_path)
    path = tmp_path / "local/simulation-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"read_access": "public", "candidate_source": "model", "search_strength": 2.431234567}))
    config = deepcopy(session.config)
    config["simulation"] = {"enabled": False, "combat_decision_mode": "upstream"}
    restored = DashboardSession(config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.simulator.settings["combat_decision_mode"] == "upstream"
    assert restored.simulator.settings["candidate_source"] == "mcts"
    assert restored.simulator.settings["search_strength"] == 2.431234567
    assert restored.simulator.enabled is True


@pytest.mark.parametrize("screen,extra,expected", [
    ("COMBAT", {}, "combat"), ("REWARD", {}, "run"),
    ("CARD_SELECTION", {"selection": {"kind": "hand_select"}}, "combat"),
    ("CARD_SELECTION", {"selection": {"kind": "deck_upgrade_select"}}, "run"),
    ("CARD_SELECTION", {"selection": {"kind": "combat_hand_select", "underlying_screen": "REWARD"}}, "run"),
    ("CARD_SELECTION", {}, "combat"),
    ("MODAL", {"modal": {"underlying_screen": "COMBAT"}}, "combat"),
    ("MODAL", {"modal": {"underlying_screen": "REWARD"}}, "run")])
def test_phase_ownership_respects_public_room_and_overlay_evidence(tmp_path, screen, extra, expected):
    """Residual live-enemy rows cannot turn rewards or known run selections into combat decisions."""
    session, _, _ = make_session(tmp_path)
    root = public_frame()
    session._state["decision_scene"] = {"scope": session._run_policy_scope(root), "screen": "COMBAT"}
    root["state"].update(screen=screen, **extra)
    assert session._decision_phase(root) == expected


def test_component_choices_are_sent_to_search_and_evaluation_and_bind_identity(tmp_path):
    """Disabling a prior or changing rollout/value components reaches the shared host rather than only the UI."""
    client, log = make_client(tmp_path, strategy="mcts")
    components = {"rollout_policy": "heuristic", "action_prior": "off", "value_model": "learned", "search_profile": "base"}
    try:
        previous = client.rule_identity()
        client.configure(components)
        assert client.rule_identity() != previous
        root = public_frame()
        client.advise(root)
        client.evaluate(root, [{"id": "pass", "actions": [{"type": "end_turn"}]}], samples=1)
        requests = [json.loads(line)["request"] for line in log.read_text().splitlines()]
        assert {request["op"] for request in requests} == {"search", "evaluate"}
        for request in requests:
            assert {key: request[key] for key in components} == components
    finally:
        client.close()


def test_reward_pool_declaration_migrates_nine_keys_and_reaches_policy_and_simulation(tmp_path):
    """Old preferences stay intact; only an explicit pool declaration enables the hypothetical full catalogue."""
    session, bridge, _ = make_session(tmp_path)
    old = {"read_access": "internal", "candidate_source": "mcts", "search_strength": 2.431234567,
        "combat_decision_mode": "upstream", "run_decision_mode": "assist", "rollout_policy": "heuristic",
        "action_prior": "off", "value_model": "learned", "search_profile": "base"}
    path = tmp_path / "local/simulation-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert {key: restored.simulator.settings[key] for key in old} == old
    assert restored.simulator.settings["reward_pool"] == "known" and path.read_bytes() == original
    client, log = make_client(tmp_path, strategy="mcts")
    try:
        previous = client.rule_identity()
        client.configure({"reward_pool": "full_unlock"})
        assert client.rule_identity() != previous
        root = public_frame()
        client.advise_run(root)
        client.evaluate(root, [{"id": "pass", "actions": [{"type": "end_turn"}]}], samples=1)
        requests = [json.loads(line)["request"] for line in log.read_text().splitlines()]
        policy = next(request for request in requests if request["op"] == "policy_evaluate")
        assert policy["public_uncertainty"] == {"worlds": 32, "full_unlock_catalogue": True}
        assert all(request["reward_pool"] == "full_unlock" for request in requests)
    finally:
        client.close()


def test_run_uncertainty_keeps_failed_worlds_and_never_replaces_an_unknown_value_with_zero(tmp_path):
    """Advisory transport preserves the entire declared public mixture when one reward pool is unknown."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    worlds = [{"ordinal": 0, "sample_seed": 101, "status": "scored", "obs": {"public_fact": 7}, "probs": [0.3, 0.7], "value": 0.2},
              {"ordinal": 1, "status": "unsupported", "obs": None, "probs": None, "value": None,
               "errors": ["reward_pool_unknown"]}]
    candidates = [{"action": {"type": "choose_event_option", "option_index": 7}, "probability": 0.7,
                   "probability_variance": 0.03, "value_variance": 0.02, "selected": True}]
    result = {"ok": False, "best_action": None, "value": None, "candidates": candidates,
        "support": {"level": "unsupported", "blocking_reasons": ["reward_pool_unknown"]},
        "decision_pipeline": "public_uncertainty_mixture", "uncertainty": {"sample_count": 2, "worlds": worlds}}
    client._request = Mock(return_value=result)
    audit = client.advise_run(public_frame())
    advice = audit["simulation_advice"]
    assert audit["result"]["uncertainty"]["worlds"] == worlds
    assert "worlds" not in advice["uncertainty"]
    assert advice["uncertainty"]["world_count"] == 2
    assert advice["uncertainty"]["completed"] == advice["uncertainty"]["failed"] == 1
    assert advice["uncertainty"]["status"] == "incomplete"
    assert advice["uncertainty"]["error_counts"] == {"reward_pool_unknown": 1}
    assert advice["candidates"] == candidates
    assert advice["value"] is None and advice["best_action"] is None
    assert advice["decision_pipeline"] == "public_uncertainty_mixture"


def test_reward_policy_steps_preserve_logical_declines_and_errors_in_advice_and_raw_audit(tmp_path):
    """Keep every proposed step and decoder metric without copying a neural observation into the model prompt."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    candidates = [{"action": None, "probability": 0.7, "selected": True,
        "upstream_action": "SkipRelic", "probability_variance": 0.03}]
    steps = [{"ok": True, "status": "estimated", "reward_row": "RELIC:VAJRA", "neural_inference": True,
              "reward_row_i": 3, "reward_type": "Relic", "relic_id": "VAJRA", "potion_id": None,
              "selected_upstream_action": "SkipRelic",
              "obs": {"full_neural_features": [1, 2]}, "actions": ["PickRelicReward", "SkipRelic"],
              "candidates": candidates, "value": 0.2, "grouped_argmax": 1, "executed": False,
              "logical_decline": "RELIC:VAJRA", "native_action": None},
             {"ok": False, "status": "unsupported", "source": "native_public_potion_procurement_veto",
              "neural_inference": False, "executed": False, "errors": ["native_gate_blocked"],
              "support": {"level": "unsupported", "blocking_reasons": ["native_gate_blocked"]}}]
    queued = {"scope": "hypothetical_until_native_confirm", "operation": "upgrade", "required_count": 2,
        "selected_native_indices": [11], "applied_original_deck_indices": [0],
        "projected_to_original_deck_indices": [0, 1], "native_observation_mutated": False}
    result = {"ok": False, "status": "unsupported", "best_action": None, "policy_steps": steps,
        "queued_selection_effects": queued,
        "logical_declines_pending": ["RELIC:VAJRA"], "errors": ["native_gate_blocked"]}
    client._request = Mock(return_value=result)
    audit = client.advise_run(public_frame())
    advice = audit["simulation_advice"]
    assert audit["result"]["policy_steps"] == steps
    assert "obs" not in advice["policy_steps"][0]
    assert advice["policy_steps"][0]["candidates"] == candidates
    assert advice["policy_steps"][0]["actions"] == steps[0]["actions"]
    assert advice["policy_steps"][0]["grouped_argmax"] == 1
    assert advice["policy_steps"][0]["reward_row_i"] == 3
    assert advice["policy_steps"][0]["selected_upstream_action"] == "SkipRelic"
    assert advice["policy_steps"][0]["executed"] is False
    assert advice["policy_steps"][1]["errors"] == ["native_gate_blocked"]
    assert advice["policy_steps"][1]["support"] == steps[1]["support"]
    assert advice["logical_declines_pending"] == ["RELIC:VAJRA"]
    assert advice["queued_selection_effects"] == queued
    assert advice["policy_status"] == "unsupported" and audit["status"] == "unavailable"
    assert "Logical declines remain pending" in advice["usage"]

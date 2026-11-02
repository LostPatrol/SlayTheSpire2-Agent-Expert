"""Check planner selection and proven import prechecks without losing episode accounting."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from sts2_agent.simulation import CombatSimulator
from test_simulation import public_frame, make_client


def test_global_import_rejection_skips_samples_but_new_public_history_invalidates_it(tmp_path):
    """Unexecuted samples cost no paths; changed witnessed counters require a fresh attempt."""
    client = CombatSimulator({"simulation": {"enabled": True}}, tmp_path)
    observed = public_frame()
    observed["state"]["combat"]["hand"][0]["card_id"] = "SHRUG_IT_OFF"
    client._request = Mock(return_value={"ok": False, "errors": ["public_state_not_supported"],
        "support": {"level": "unsupported", "reasons": ["unknown_public_relic:EXAMPLE"]}})
    advice = client.advise(observed)
    assert client._request.call_count == 1
    assert advice["simulation_advice"]["bounded_action_previews"]["status"] == "not_run"
    plans = [{"id": "draw", "actions": [{"type": "play_card", "card_index": 7, "target_index": 11}]}]
    result = client.evaluate(observed, plans, samples=6, rollout=False)
    assert result["status"] == "unsupported" and client._request.call_count == 1
    ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
    assert json.loads(ledger.read_text())["paths_used"] == 0
    changed = deepcopy(observed)
    changed["public_history"] = {"history_complete": True, "total_card_plays_this_turn": 2}
    client.evaluate(changed, plans, samples=6, rollout=False)
    assert client._request.call_count == 2
    assert json.loads(ledger.read_text())["paths_used"] == 6


def test_single_online_planner_preserves_primary_advice_and_persistent_budget(tmp_path):
    """Selecting beam avoids duplicate comparisons without changing its current first action."""
    client, log = make_client(tmp_path)
    client.strategies = ("beam",)
    try:
        advice = client.advise(public_frame())
        assert set(advice["results"]) == {"beam"}
        assert set(advice["simulation_advice"]["strategies"]) == {"beam"}
        assert advice["simulation_advice"]["strategies"]["beam"]["first_action"]["card_index"] == 29
        requests = [json.loads(line)["request"] for line in log.read_text().splitlines()]
        assert len(requests) == 1 and requests[0]["strategy"] == "beam"
        ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
        assert json.loads(ledger.read_text())["search_requests_used"] == 1
    finally:
        client.close()


def test_partial_global_action_blockers_skip_previews_and_repeated_nonempty_plans(tmp_path):
    """A successful import can still prove all changing actions unsupported at this root."""
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "mentor"}}, tmp_path)
    observed = public_frame()
    observed["state"]["combat"]["hand"][0]["card_id"] = "SHRUG_IT_OFF"
    support = {"level": "partial", "reasons": ["relic_rule_not_reviewed:CENTENNIAL_PUZZLE"],
               "blocking_reasons": ["relic_rule_not_reviewed:CENTENNIAL_PUZZLE"]}
    client._request = Mock(return_value={"ok": True, "support": support,
        "comparison_available": False, "best": {"actions": [], "state": {}}})

    advice = client.advise(observed)
    prompt = advice["simulation_advice"]
    assert client._request.call_count == 1
    assert prompt["support"] == support
    assert prompt["evaluation_available"] is False
    assert prompt["bounded_action_previews"] == {"status": "not_run", "reason": "public_root_actions_unsupported"}
    assert "Changing candidate" in prompt["notice"] and "Do not retry simulation_evaluate" in prompt["notice"]
    for identity, action in [("draw", {"type": "play_card", "card_index": 7, "target_index": 11}),
                             ("end", {"type": "end_turn"})]:
        result = client.evaluate(observed, [{"id": identity, "actions": [action]}], samples=6, rollout=False)
        assert result["status"] == "unsupported"
        assert result["diagnostics"] == [{"code": "public_root_actions_unsupported", "support": support}]
    assert client._request.call_count == 1
    ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
    assert json.loads(ledger.read_text())["paths_used"] == 0


@pytest.mark.parametrize("include_changing_plan", [False, True])
def test_action_blockers_preserve_empty_plan_root_inspection(tmp_path, include_changing_plan):
    """Keep a valid empty plan, including mixed batches, under normal budget semantics."""
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "mentor"}}, tmp_path)
    observed = public_frame()
    client._request = Mock(side_effect=[
        {"ok": True, "support": {"level": "partial", "blocking_reasons": ["relic_rule_not_reviewed:CENTENNIAL_PUZZLE"]}},
        {"ok": True, "candidates": []},
    ])
    client.advise(observed)
    plans = [{"id": "inspect", "actions": []}]
    if include_changing_plan:
        plans.append({"id": "change", "actions": [{"type": "end_turn"}]})
    result = client.evaluate(observed, plans, samples=2, rollout=False)
    assert result["status"] == "evaluated"
    assert client._request.call_count == 2
    assert client._request.call_args.args[0]["candidate_plans"] == plans
    ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
    assert json.loads(ledger.read_text())["paths_used"] == 2 * len(plans)


@pytest.mark.parametrize("limitation", ["local_card_not_supported", "unknown_draw_boundary"])
def test_partial_local_or_turn_gaps_do_not_disable_sampled_evaluation(tmp_path, limitation):
    """A stop-only search without global action blockers cannot veto sampled plans."""
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "mentor"}}, tmp_path)
    observed = public_frame()
    observed["state"]["combat"]["hand"][0]["card_id"] = "SHRUG_IT_OFF"
    search = {"ok": True, "support": {"level": "partial", "blocking_reasons": [], "reasons": [limitation]},
              "comparison_available": False, "search_support_reasons": [limitation],
              "turn_support": {"gaps": [limitation]}, "best": {"actions": [{"type": "end_turn"}], "state": {}}}
    client._request = Mock(side_effect=[search, {"ok": True, "candidates": []}])
    advice = client.advise(observed)
    assert client._request.call_count == 1
    assert advice["simulation_advice"].get("evaluation_available") is not False
    result = client.evaluate(observed, [{"id": "draw", "actions": [{"type": "play_card", "card_index": 7,
                                                                          "target_index": 11}]}], samples=3, rollout=False)
    assert result["status"] == "evaluated"
    assert [call.args[0]["op"] for call in client._request.call_args_list] == ["search", "evaluate"]
    ledger = next((tmp_path / "local/simulation-budgets").glob("*.json"))
    assert json.loads(ledger.read_text())["paths_used"] == 3

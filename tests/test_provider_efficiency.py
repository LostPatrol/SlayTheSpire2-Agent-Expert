"""Verify reusable transport and caches preserve live facts, budgets, and ownership."""

import copy
import json

import httpx

from sts2_agent.knowledge import KnowledgeBase
from sts2_agent.model_tools import ModelTools
from sts2_agent.prompt_context import deck_summary
from sts2_agent.provider import Provider
from test_provider import DECISION, ROOT, TEST_KEY, completion
from sts2_agent.config import load_config


def test_owned_transport_survives_decisions_and_failure_until_close(monkeypatch):
    """Repeated calls reuse one pool while closing it releases the owned transport once."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    responses = [httpx.Response(503), completion(json.dumps(DECISION)), completion(json.dumps(DECISION))]
    calls = []

    def handle(request):
        """Return one transient failure followed by two valid decisions."""
        calls.append(json.loads(request.content))
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    constructors = []

    def make_client(**options):
        """Record pool construction without using the network."""
        constructors.append(options)
        return client

    monkeypatch.setattr("sts2_agent.provider.httpx.Client", make_client)
    monkeypatch.setattr("sts2_agent.provider.time.sleep", lambda _delay: None)
    provider = Provider(load_config("configs/smoke.json", ROOT), ROOT)
    provider.decide({}, {})
    provider.decide({}, {})
    assert len(constructors) == 1 and len(calls) == provider.requests_used == 3
    assert not client.is_closed
    provider.close()
    provider.close()
    assert client.is_closed


def test_injected_transport_remains_caller_owned():
    """Provider shutdown never closes a shared externally supplied client."""
    with httpx.Client(transport=httpx.MockTransport(lambda _request: completion("{}"))) as client:
        provider = Provider(load_config("configs/smoke.json", ROOT), ROOT, client=client)
        provider.close()
        assert not client.is_closed


def test_query_cache_is_root_scoped_and_never_reuses_simulation(monkeypatch):
    """Static reads return isolated equal values; budgeted simulation always dispatches."""
    tools = ModelTools(KnowledgeBase(ROOT))
    dispatched = []

    def dispatch(name, args, observation, memory):
        """Expose local work count and mutable output for isolation checks."""
        dispatched.append(name)
        return {"entries": [{"value": len(dispatched)}]}

    monkeypatch.setattr(tools, "_dispatch", dispatch)
    call = {"id": "one", "function": {"name": "wiki_search", "arguments": '{"query":"Bash"}'}}
    first = tools.call(call, {"turn": 1}, {}, {})
    first["entries"][0]["value"] = -1
    assert tools.call(call, {"turn": 1}, {}, {})["entries"][0]["value"] == 1
    assert len(dispatched) == 1
    tools.call(call, {"turn": 2}, {}, {})
    assert len(dispatched) == 2
    tools.knowledge_status = "version_mismatch"
    tools.call(call, {"turn": 2}, {}, {})
    assert len(dispatched) == 3
    sim = {"id": "sim", "function": {"name": "simulation_evaluate", "arguments": json.dumps({
        "candidate_plans": [{"id": "a", "actions": []}], "samples": 1})}}
    tools.call(sim, {}, {}, {})
    tools.call(sim, {}, {}, {})
    assert dispatched.count("simulation_evaluate") == 2


def test_dependency_cache_never_caches_observed_amounts_or_mutable_results():
    """A warm frozen dependency graph still reports each new live Power amount."""
    knowledge = KnowledgeBase(ROOT)
    first = knowledge.context_for_model({"state": {"combat": {"player": {"powers": ["SANDPIT_POWER 1"]}}}})
    first["entries"].clear()
    second = knowledge.context_for_model({"state": {"combat": {"player": {"powers": ["SANDPIT_POWER 3"]}}}})
    power = next(entry for entry in second["entries"] if entry["entity_id"] == "SANDPIT")
    assert power["observed_amounts"] == [3]
    assert knowledge._dependency_cache


def test_deck_summary_requires_native_copy_counts_and_preserves_unknown_costs():
    """Grouped display rows never become one-card guesses or current combat cost quotes."""
    observation = {"state": {"run": {"deck_size": 4, "deck": [
        {"card_count": 3, "card_type": "Attack", "energy_cost": 1, "upgraded": False},
        {"card_count": 1, "card_type": "Skill", "energy_cost": 0, "costs_x": True, "upgraded": True}]}}}
    summary = deck_summary(observation)
    assert summary["cards"] == 4 and summary["upgraded_cards"] == 1
    assert summary["card_type_counts"] == {"Attack": 3, "Skill": 1}
    assert summary["run_display_energy_cost_counts"] == {"1": 3, "X": 1}
    changed = copy.deepcopy(observation)
    changed["state"]["run"]["deck"][0].pop("card_count")
    assert deck_summary(changed) == {}
    changed = copy.deepcopy(observation)
    changed["state"]["run"]["deck_size"] = 5
    assert deck_summary(changed)["status"] == "inconsistent_native_counts"

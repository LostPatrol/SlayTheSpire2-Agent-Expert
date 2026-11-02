"""Model-only payload projection keeps action facts and drops audit metadata."""

import copy

from sts2_agent.model_view import model_view


def sample_payload():
    card = {"i": 0, "card_id": "STRIKE_IRONCLAD", "line": "Strike", "energy_cost": 1,
            "playable": True, "targets": [], "dynamic_values": [{"name": "Damage", "current_value": 6}],
            "known_instance_ids": ["card-1"]}
    advice = {
        "status": "evaluated", "backend": "mentor", "rule_snapshot": "rules",
        "settings": {"read_access": "public"}, "usage": "Only first_action is executable.",
        "search_budget": {"remaining_requests": 5},
        "rule_identity": {"backend": "mentor", "artifact_sha256": "x", "networks": {"n": 1}},
        "source_provenance": {"path": "x"}, "public_state_hash": "h",
        "strategies": {"mcts": {
            "first_action": {"type": "play_card", "card_index": 0},
            "evaluation": {"p_win": 1.0},
            "mcts": {"p_win": 1.0},
            "alternatives": [{"first_action": {"type": "play_card", "card_index": 0},
                              "evaluation": {"p_win": 0.5}, "mcts": {"p_win": 0.5},
                              "sequence": []}],
        }},
    }
    return {
        "instructions": [{"text": "use potions", "scope": "run"}],
        "memory": {"plan": "plan", "events": [{"source": "experienced_action",
                                               "facts": {"hand": [card]}}]},
        "observation": {
            "available_actions": ["play_card"],
            "action_parameters": {"play_card": {"card_index": "combat.hand[].i"}},
            "state": {
                "screen": "COMBAT", "turn": 1,
                "available_actions": ["play_card"], "glossary": {}, "session": {"phase": "combat"},
                "combat": {"hand": [card], "draw": [card], "discard": [], "exhaust": [],
                           "action_readiness": {"actions_settled": True}},
                "run": {"hp": "50/80", "piles": {"draw": [card], "discard": [], "exhaust": []}},
            },
            "public_history": {"enemy_turns": [{"turn": 1, "enemies": []}]},
            "simulation_advice": advice,
        },
        "knowledge": {
            "snapshot_id": "snap", "status": "ready", "verification": "source_build_matched",
            "usage": "Static reference only.",
            "entries": [{
                "kind": "cards", "name": "Strike", "description": "Deal 6.",
                "source": {"path": "cards.json", "revision": "r", "sha256": "s"},
                "conditions": {"game_version": "v", "build_id": "b", "channel": "stable"},
                "selection": {"priority": 1}, "verification": "source_build_matched",
                "fact_id": "cards.STRIKE", "entity_id": "STRIKE",
            }],
        },
        "strategy": {
            "status": "ready", "source_revision": "r2", "usage": "Guide caveat.",
            "sections": [{"title": "T", "markdown": "M", "source": {"path": "g"}, "id": "g/1"}],
        },
    }


def test_safe_view_keeps_action_facts_and_removes_audit_metadata():
    payload = sample_payload()
    original = copy.deepcopy(payload)
    view = model_view(payload, "safe")

    assert payload == original
    state = view["observation"]["state"]
    assert "action_readiness" not in state["combat"]
    assert "session" not in state and "available_actions" not in state
    assert "glossary" not in state
    assert "piles" not in state["run"]
    assert state["combat"]["hand"][0] == payload["observation"]["state"]["combat"]["hand"][0]
    assert view["observation"]["public_history"] == payload["observation"]["public_history"]
    assert view["memory"] == payload["memory"]

    entry = view["knowledge"]["entries"][0]
    assert entry["description"] == "Deal 6."
    assert "source" not in entry and "conditions" not in entry and "fact_id" not in entry
    assert view["knowledge"]["scope"]["game_version"] == "v"
    assert view["knowledge"]["scope"]["verification"] == "source_build_matched"
    assert view["strategy"]["sections"][0]["markdown"] == "M"
    assert "source" not in view["strategy"]["sections"][0]

    advice = view["observation"]["simulation_advice"]
    assert "rule_identity" not in advice
    assert advice["settings"]["read_access"] == "public"
    assert advice["search_budget"]["remaining_requests"] == 5
    assert advice["usage"] == "Only first_action is executable."
    alternative = advice["strategies"]["mcts"]["alternatives"][0]
    assert "evaluation" not in alternative
    assert alternative["mcts"]["p_win"] == 0.5
    assert view["instructions"] == payload["instructions"]


def test_piles_are_kept_when_they_are_not_exact_duplicates():
    payload = sample_payload()
    payload["observation"]["state"]["run"]["piles"]["draw"] = [{"card_id": "OTHER"}]
    view = model_view(payload, "safe")
    assert "piles" in view["observation"]["state"]["run"]


def test_unequal_statistics_are_not_deduplicated():
    payload = sample_payload()
    block = payload["observation"]["simulation_advice"]["strategies"]["mcts"]
    block["evaluation"] = {"p_win": 0.25}
    view = model_view(payload, "safe")
    assert "evaluation" in view["observation"]["simulation_advice"]["strategies"]["mcts"]


def test_off_mode_returns_the_same_payload():
    payload = sample_payload()
    assert model_view(payload, "off") is payload

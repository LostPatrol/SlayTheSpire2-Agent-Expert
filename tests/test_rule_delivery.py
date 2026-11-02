"""Replay recorded public decisions through the actual provider message builder offline."""

import json
from pathlib import Path

import pytest

from test_provider import build, completion, public_payload


# This fixture preserves the terminal fight's public observation, without historical messages.
FIXTURE = Path(__file__).parent / "fixtures/public/queen-bound-decision.json"


def test_recorded_queen_decision_delivers_binding_dependency_in_actual_messages(monkeypatch):
    """The original missing rule must reach the outgoing request beside its source Power."""
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "afflictions.BOUND" not in recorded["original_knowledge"]["fact_ids"]
    decision = {"action": {"type": "play_card", "card_index": 1}, "reason": "Offline protocol test"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    observation = recorded["observation"]
    provider.decide(observation, {})
    actual = public_payload(requests[0]["messages"])
    facts = {entry["fact_id"]: entry for entry in actual["knowledge"]["entries"]}
    assert "powers.CHAINS_OF_BINDING" in facts
    assert "afflictions.BOUND" in facts
    assert "Only 1 Bound card can be played each turn" in facts["afflictions.BOUND"]["description"]
    assert actual["observation"] == observation
    assert provider.metadata[0]["knowledge_entries"] == len(facts)
    assert provider.metadata[0]["knowledge_fact_ids"] == list(facts)
    assert provider.metadata[0]["knowledge_omitted_entries"] == actual["knowledge"]["omitted_entries"]
    assert all(item["reason"] in {"entry_limit", "character_limit"}
               for item in provider.metadata[0]["knowledge_omitted_entries"])
    assert len(requests) == 1


@pytest.mark.parametrize("kind,identifier", [
    ("affliction", name) for name in ("BOUND", "ENTANGLED", "GALVANIZED", "HEXED", "RINGING", "SMOG", "TAINTED")
] + [("enchantment", name) for name in ("CORRUPTED", "ROYALLY_APPROVED", "SHARP")])
def test_structured_card_modifiers_reach_actual_messages(monkeypatch, kind, identifier):
    """Public modifier identifiers deliver rules for all afflictions and varied enchantments."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline protocol test"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    observation = {"state": {"combat": {"hand": [{"card_id": "ANGER", kind: {
        kind + "_id": identifier, "name": identifier, "amount": 1}}]}}}
    provider.decide(observation, {})
    payload = public_payload(requests[0]["messages"])
    assert any(entry["fact_id"] == kind + "s." + identifier for entry in payload["knowledge"]["entries"])
    assert payload["observation"] == observation


def test_live_power_details_reach_actual_messages_without_field_loss(monkeypatch):
    """Preserve native dynamic text and live counters in the message sent to the model."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline protocol test"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    observation = {
        "state": {
            "combat": {
                "player": {
                    "power_details": [{
                        "index": 0,
                        "power_id": "RUPTURE_POWER",
                        "name": "Rupture",
                        "description": "Whenever you lose HP on your turn, gain 2 Strength.",
                        "amount": 2,
                        "display_amount": 2,
                        "is_debuff": False,
                    }],
                },
                "enemies": [],
            },
        },
    }
    provider.decide(observation, {})
    actual = public_payload(requests[0]["messages"])
    power = actual["observation"]["state"]["combat"]["player"]["power_details"][0]
    assert power == observation["state"]["combat"]["player"]["power_details"][0]
    assert provider.metadata[0]["context_chars"] >= len(requests[0]["messages"][-1]["content"])


@pytest.mark.parametrize("surface", ["player", "enemy", "pet"])
def test_unresolved_power_parameters_reach_actual_messages(monkeypatch, surface):
    """Keep unknown live parameters separate from stack amounts on all creature surfaces."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline delivery check"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    power = {"power_id": "INFERNO_POWER", "amount": 18,
             "description": "Lose {SelfDamage} HP; deal 18 damage.",
             "description_complete": False, "unresolved_variables": ["SelfDamage"]}
    creature = {"power_details": [power]}
    combat = {"player": creature} if surface == "player" else (
        {"enemies": [creature]} if surface == "enemy" else {"player": {"pet_details": [creature]}})
    observation = {"state": {"combat": combat}}
    provider.decide(observation, {})
    assert public_payload(requests[0]["messages"])["observation"] == observation


def test_ordinary_bash_receives_hand_cleanup_rules_despite_an_incorrect_old_plan(monkeypatch):
    """Normal cards need default cleanup rules even when no observed card requests Retain."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline delivery check"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    observation = {"available_actions": ["end_turn"], "state": {"screen": "COMBAT",
        "combat": {"player": {"energy": 0, "powers": []}, "hand": [
            {"i": 0, "card_id": "BASH", "mods": [], "enchantment": None, "playable": False}]}}}
    memory = {"plan": "Mistaken old hypothesis: keep Bash in hand until next turn."}
    provider.decide(observation, memory)
    actual = public_payload(requests[0]["messages"])
    facts = {entry["fact_id"]: entry for entry in actual["knowledge"]["entries"]}
    assert {"keywords.RETAIN", "keywords.ETHEREAL", "keywords.EXHAUST"} <= facts.keys()
    assert actual["observation"] == observation
    assert actual["memory"]["plan"] == memory["plan"]
    system = requests[0]["messages"][0]["content"]
    assert "ordinary unplayed hand cards are discarded" in system
    assert "Leaving a card unplayed does NOT grant Retain" in system
    assert "Cards remaining at a stopped simulation endpoint are NOT thereby retained" in system
    assert provider.metadata[0]["prompt_policy_version"] == "public-balanced-strategy-tools-v8"


def test_latest_hand_handles_stay_inline_after_prior_indices_change(monkeypatch):
    """A reused old Strike index must visibly name the current Defend, without dereferencing."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline delivery check"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision)) for _ in range(2)])
    for identifier in ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD"):
        observation = {"available_actions": ["end_turn"], "state": {"screen": "COMBAT",
            "combat": {"hand": [{"i": 2, "card_id": identifier, "line": identifier,
                                 "energy_cost": 1, "playable": False, "targets": []}], "enemies": []}}}
        provider.decide(observation, {})
    projection = json.loads(requests[-1]["messages"][-1]["content"])
    reference = projection["current_action_reference"]
    assert reference["hand"][0] == observation["state"]["combat"]["hand"][0]
    assert reference["hand"][0]["card_id"] == "DEFEND_IRONCLAD"
    assert "$ref" not in json.dumps(reference)
    assert public_payload(requests[-1]["messages"])["observation"] == observation


def test_native_damage_previews_and_duration_rules_reach_outgoing_messages(monkeypatch):
    """Keep raw and modified damage distinct beside the rules needed by the last fight."""
    decision = {"action": {"type": "end_turn"}, "reason": "Offline delivery check"}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision))])
    observation = {"available_actions": ["end_turn"], "state": {"screen": "COMBAT", "turn": 4,
        "combat": {"player": {"hp": "21/87", "energy": 4, "powers": ["STRENGTH_POWER 10"]},
            "hand": [{"i": 1, "card_id": "EXTERMINATE", "line": "杀灭：造成13点伤害4次。",
                "energy_cost": 1, "playable": True, "targets": [], "dynamic_values": [
                    {"name": "Damage", "base_value": 3, "current_value": 13},
                    {"name": "Repeat", "base_value": 4, "current_value": 4}]}],
            "enemies": [{"i": 0, "enemy_id": "OWL_MAGISTRATE", "hp": "61/231", "block": 0,
                "powers": ["SOAR_POWER 1", "VULNERABLE_POWER 3 [debuff]"],
                "intents": [{"damage": 33, "hits": 1, "total_damage": 33}]}]}}}
    provider.decide(observation, {})
    payload = public_payload(requests[0]["messages"])
    assert payload["observation"] == observation
    system = requests[0]["messages"][0]["content"]
    assert "Do NOT add those modifiers again" in system
    assert "normally expires before your next player turn" in system
    assert "Do not reduce a Weak enemy's displayed attack a second time" in system
    assert "ordinary play without an Exhaust effect sends it to discard" in system

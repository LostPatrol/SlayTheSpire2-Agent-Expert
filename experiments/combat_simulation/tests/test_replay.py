"""Validate public replay admission and deterministic comparison semantics."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path


# Load the isolated module without changing the production package or pytest paths.
MODULE_PATH = Path(__file__).parents[1] / "replay.py"
SPEC = importlib.util.spec_from_file_location("combat_replay", MODULE_PATH)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def _state(revision: int, stable: bool = True) -> dict:
    """Build a minimal public frame with one deterministic playable card."""
    return {"screen": "COMBAT", "action_revision": revision, "turn": 1,
            "run": {"floor": 1}, "combat": {
                "action_readiness": {name: stable for name in replay.STABLE_GATE_FIELDS},
                "player": {"hp": "30/80", "block": 0, "energy": 3, "stars": 0,
                           "powers": ["STRENGTH_POWER 2"]},
                "hand": [{"i": 0, "card_id": "STRIKE_IRONCLAD"}],
                "enemies": [{"i": 0, "enemy_id": "NIBBIT", "hp": "20/40", "block": 0,
                             "powers": [], "alive": True}]}}


def _write_logs(tmp_path: Path, requests: list, receipts: list, events: list) -> None:
    """Write only the three allowed public journals into a temporary test directory."""
    for name, values in (("execution-requests.jsonl", requests),
                         ("execution-receipts.jsonl", receipts), ("public-events.jsonl", events)):
        (tmp_path / name).write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def _request(action_id: str, before: dict) -> dict:
    """Create an execution request bound to its original public observation."""
    return {"action_id": action_id, "timestamp_utc": "2026-10-26T10:00:00+00:00",
            "action": {"type": "play_card", "card_index": 0, "target_index": 0},
            "observation": {"state": before}}


def test_receipt_stable_does_not_replace_actionable_successor(tmp_path):
    """Select the later stable event and preserve the original before frame."""
    before = _state(1)
    transitional = _state(2, stable=False)
    after = _state(2)
    after["combat"]["player"]["energy"] = 2
    after["combat"]["hand"] = [{"i": 0, "card_id": "BASH"}]
    events = [{"timestamp_utc": "2026-10-26T10:00:01+00:00", "observation": {"state": transitional}},
              {"timestamp_utc": "2026-10-26T10:00:02+00:00", "observation": {"state": after}}]
    _write_logs(tmp_path, [_request("accepted", before)],
                [{"action_id": "accepted", "status": "completed", "stable": True,
                  "public_state": transitional}], events)
    result = replay.extract_cases(tmp_path)
    assert result["counts"]["eligible_cases"] == 1
    case = result["cases"][0]
    assert case["after"]["combat"]["player"]["energy"] == 2
    assert case["before"]["combat"]["hand"][0]["card_id"] == "STRIKE_IRONCLAD"
    assert case["metadata"]["public_event_line"] == 2
    assert case["metadata"]["observed_hand_additions"] == {"BASH": 1}


def test_rejected_request_cannot_borrow_another_actions_successor(tmp_path):
    """Reject an unreceipted stale proposal even if revision + 1 exists."""
    event = {"timestamp_utc": "2026-10-26T10:00:02+00:00", "observation": {"state": _state(2)}}
    _write_logs(tmp_path, [_request("rejected", _state(1))], [], [event])
    result = replay.extract_cases(tmp_path)
    assert result["cases"] == []
    assert result["counts"]["excluded_missing_accepted_receipt"] == 1


def test_pending_selection_is_reported_without_fabricating_combat_after(tmp_path):
    """Keep choice-panel plays in the exclusion accounting."""
    event = {"timestamp_utc": "2026-10-26T10:00:02+00:00", "observation": {
        "state": {"screen": "CARD_SELECTION", "action_revision": 2}}}
    _write_logs(tmp_path, [_request("pending", _state(1))],
                [{"action_id": "pending", "status": "pending"}], [event])
    result = replay.extract_cases(tmp_path)
    assert result["counts"]["accepted_play_card_requests"] == 1
    assert result["counts"]["excluded_next_screen_CARD_SELECTION"] == 1


def test_compare_uses_living_order_and_reports_power_amount_mismatch():
    """Ignore dead slots and ephemeral indexes without hiding changed Power amounts."""
    expected = _state(2)
    prediction = {"state": {"player": deepcopy(expected["combat"]["player"]),
                             "enemies": [
                                 {"i": 0, "enemy_id": "INKLET", "hp": {"current": 0, "max": 12},
                                  "block": 0, "powers": [], "alive": False},
                                 {"i": 9, "enemy_id": "NIBBIT", "hp": {"current": 20, "max": 40},
                                  "block": 0, "powers": [], "alive": True}]}}
    assert replay.compare_prediction(prediction, expected)["matches"]
    prediction["state"]["player"]["powers"] = ["STRENGTH_POWER 3"]
    result = replay.compare_prediction(prediction, expected)
    assert not result["matches"]
    assert [difference["field"] for difference in result["differences"]] == ["player.powers"]


def test_missing_resources_are_not_assumed_zero():
    """Distinguish unsupported host outputs from genuine zero resources."""
    expected = _state(2)
    prediction = deepcopy(expected)
    del prediction["combat"]["player"]["stars"]
    result = replay.compare_prediction(prediction, expected)
    assert result["differences"] == [{"field": "player.stars", "expected": 0,
                                      "actual": {"missing": True}}]

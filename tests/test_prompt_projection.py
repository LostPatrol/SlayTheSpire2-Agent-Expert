"""Verify exact preservation of public observations under shared prompt encoding."""

from copy import deepcopy
import json
from pathlib import Path

from sts2_agent.prompt_projection import compact_public, expand_public, serialize_projection


# The recorded Queen decision includes live Power counters and Bound card mechanics.
QUEEN_FIXTURE = Path(__file__).parent / "fixtures/public/queen-bound-decision.json"


def test_projection_order_keeps_dynamic_handles_last_without_changing_data_or_size():
    """Preserve every fact while placing changing combat handles after shared values."""
    original = {"hand": [{"i": 0, "name": "Current card", "cost": 1}]}
    projection = compact_public(original)
    projection["current_action_reference"] = {"hand": original["hand"]}
    encoded = serialize_projection(projection)
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert json.loads(encoded) == projection
    assert len(encoded) == len(canonical)
    assert list(json.loads(encoded)) == ["encoding", "definitions", "payload", "current_action_reference"]
    assert expand_public(json.loads(encoded)) == original


def test_recorded_queen_observation_round_trips_without_field_loss():
    """Retain every original card, pile, enemy, relic and live numeric field."""
    original = json.loads(QUEEN_FIXTURE.read_text(encoding="utf-8"))["observation"]
    before = deepcopy(original)
    projection = compact_public(original)
    assert expand_public(projection) == before
    assert original == before
    assert len(json.dumps(projection, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))


def test_sharing_preserves_indices_distinct_values_and_literal_marker_keys():
    """Share only equal values and safely restore public data resembling encoding syntax."""
    description = "Visible rule text. " * 12
    original = {
        "hand": [{"i": 0, "card_id": "STRIKE", "damage": 6, "description": description},
                 {"i": 1, "card_id": "STRIKE", "damage": 9, "description": description}],
        "literal_ref": {"$ref": "ordinary public text"},
        "literal_literal": {"$literal": {"$ref": "another public text"}},
        "facts": [{"description": description}, {"description": description}],
        "defaults": [False, 0, None, [], {}],
    }
    projection = compact_public(original)
    assert expand_public(projection) == original
    assert compact_public(original) == projection
    restored = expand_public(projection)
    assert restored["hand"][0]["damage"] == 6
    assert restored["hand"][1]["damage"] == 9


def test_nested_shared_lists_and_unknown_rich_text_remain_exact():
    """Repeated parent objects retain descendant values without a needless reference chain."""
    rule = {"text": "[unknown]保留所有文字[/unknown]" * 12, "amount": 2}
    cards = [{"i": index, "rule": rule} for index in range(4)]
    original = {"run": {"piles": {"draw": cards}}, "combat": {"draw": cards},
                "memory": {"facts": [rule, rule]}}
    projection = compact_public(original)
    assert expand_public(projection) == original
    assert len(json.dumps(projection, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))


def test_retained_definitions_are_immutable_and_new_messages_only_send_changes():
    """Read prior definitions while preserving changed cards and live numeric counters."""
    original = json.loads(QUEEN_FIXTURE.read_text(encoding="utf-8"))["observation"]
    first = compact_public(original)
    known = deepcopy(first["definitions"])
    snapshot = deepcopy(known)
    current = deepcopy(original)
    current["state"]["combat"]["player"]["energy"] = 99
    second = compact_public(current, known)
    assert known == snapshot
    assert not set(second["definitions"]) & set(known)
    assert expand_public(second, known) == current
    assert len(json.dumps(second, ensure_ascii=False)) < len(json.dumps(first, ensure_ascii=False)) / 3
    known.update(second["definitions"])
    repeated = compact_public(current, known)
    assert repeated["definitions"] == {}
    assert expand_public(repeated, known) == current


def test_reference_depth_is_bounded_even_for_deep_repeated_public_data():
    """No encoded leaf needs more than four lookups across all shared definitions."""
    original = {"text": "Shared visible information. " * 15}
    for index in range(12):
        original = {"left": original, "right": deepcopy(original), "level": index}
    projection = compact_public(original)

    def depth(item):
        """Count definition lookups along the longest encoded path."""
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                return 1 + depth(projection["definitions"][item["$ref"]])
            return max((depth(child) for child in item.values()), default=0)
        if isinstance(item, list):
            return max((depth(child) for child in item), default=0)
        return 0

    assert depth(projection["payload"]) <= 4
    assert expand_public(projection) == original


def test_retained_short_hash_collision_does_not_change_public_value():
    """A conflicting retained short identity cannot replace a different current value."""
    original = {"description": "A new and fully visible current rule. " * 12}
    first = compact_public(original)
    identifier = next(iter(first["definitions"]))
    known = {identifier: "A different earlier visible public rule. " * 12}
    projection = compact_public(original, known)
    assert expand_public(projection, known) == original
    assert identifier not in projection["definitions"]
    assert len(next(iter(projection["definitions"]))) > len(identifier)

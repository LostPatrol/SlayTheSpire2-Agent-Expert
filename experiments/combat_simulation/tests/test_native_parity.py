"""Check that the native comparison cannot hide missing fields or physical card differences."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

# Reuse the experiment module without making the native oracle a production dependency.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
from native_parity import compare, normalize


def endpoint():
    """Provide every field promised by the common-subset comparison contract."""
    return {"player": {"hp": {"current": 80, "max": 80}, "block": 0, "energy": 3,
                       "powers": ["STRENGTH_POWER 2"]},
            "enemies": [{"hp": {"current": 40, "max": 40}, "block": 0, "powers": []}],
            "hand": [{"card_id": "STRIKE_IRONCLAD", "upgraded": False},
                     {"card_id": "STRIKE_IRONCLAD", "upgraded": True}],
            "piles": {"draw": 2, "discard": 0, "exhaust": 0}, "potions": [], "pending_choice": None}


def test_schema_dialects_compare_without_losing_quantities():
    """Equivalent naming and HP formats preserve a strict comparison of the same fields."""
    native = endpoint()
    mentor = deepcopy(native)
    mentor["player"].update(hp=80, max_hp=80, powers=[{"id": "Strength", "amount": 2}])
    mentor["hand"][0]["card_id"] = "StrikeIronclad"
    assert compare(normalize(native), normalize(mentor))["status"] == "matched"


@pytest.mark.parametrize("field", ["powers", "hp", "energy"])
def test_missing_observation_fields_cannot_become_zero_or_empty(field):
    """An absent native or simulated resource must fail visibly instead of matching defaults."""
    state = endpoint()
    del state["player"][field]
    with pytest.raises((KeyError, ValueError)):
        normalize(state)


def test_same_id_different_upgrades_and_choice_boundaries_are_compared():
    """A selected physical copy and an unfinished choice remain observable discrepancies."""
    native = endpoint()
    predicted = deepcopy(native)
    predicted["hand"].reverse()
    predicted["pending_choice"] = {"candidates": []}
    result = compare(normalize(native), normalize(predicted))
    assert result["status"] == "mismatch"
    assert {item["field"] for item in result["differences"]} == {"hand", "pending_choice"}

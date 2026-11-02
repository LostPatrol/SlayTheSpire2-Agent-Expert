"""Exercise expanded comparison against hidden blind spots and partial observations."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

# The test imports the offline experiment, never the production controller or native DLL.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
from parity_contract import compare_contract, state_contract
from mentor_production_parity import cleanup_boundary
from batch_validation import pending_draw_boundary


def card(name="STRIKE_IRONCLAD"):
    """Build one completely observed card descriptor for contract tests."""
    return {"card_id": name, "upgraded": False, "cost": 1, "star_cost": 0,
            "effective_keywords": [], "affliction": None, "enchantment": None, "dynamic_values": []}


def endpoint():
    """Provide a complete small snapshot independent of either engine implementation."""
    return {"player": {"hp": {"current": 70, "max": 80}, "energy": 3, "stars": 0,
            "block": 0, "powers": [], "power_details": [], "cards_played_this_turn": 0,
            "attacks_played_this_turn": 0, "skills_played_this_turn": 0, "manual_card_plays_this_turn": 0,
            "non_auto_card_plays_this_turn": 0},
            "enemies": [{"i": 0, "enemy_id": "NIBBIT", "hp": 40, "block": 0,
                         "alive": True, "hittable": True, "powers": [], "power_details": []}],
            "hand": [card()], "piles": {"draw": 1, "discard": 0, "exhaust": 0},
            "pile_cards": {"draw": [card()], "discard": [], "exhaust": []},
            "potions": [{"i": 0, "potion_id": "FIRE_POTION", "occupied": True, "usable": True}],
            "pending_choice": {"kind": "Exhaust", "min_select": 1, "max_select": 1,
                               "can_skip": False, "options": [card()]}}


def test_reward_boundary_preserves_real_combat_errors():
    """Reward auto-collection is incomparable, but HP errors remain numeric failures."""
    native = {"enemies": [{"alive": False}], "potions": [{"occupied": True}]}
    simulated = {"terminal": True, "enemies": [{"alive": False}], "potions": [{"occupied": False}]}
    reward = [{"field": "potions[0].id", "expected": "FIREPOTION", "actual": None}]
    assert cleanup_boundary(reward, native, simulated) == "native_victory_cleanup_boundary"
    assert cleanup_boundary(reward + [{"field": "player.hp", "expected": 70, "actual": 60}], native, simulated) is None


def test_copy_attribute_association_survives_unknown_dynamic_preview():
    """Swapping modifiers between differently priced copies changes the known instance multiset."""
    expected = endpoint()
    first, second = card(), card()
    first.update(cost=0, affliction={"id": "TAINTED", "amount": 1}, dynamic_values=None)
    second.update(cost=1, dynamic_values=None)
    expected["pile_cards"]["draw"] = [first, second]
    expected["piles"]["draw"] = 2
    actual = deepcopy(expected)
    actual["pile_cards"]["draw"][0]["affliction"] = None
    actual["pile_cards"]["draw"][1]["affliction"] = {"id": "TAINTED", "amount": 1}
    result = compare_contract(state_contract(expected), state_contract(actual))
    assert result["status"] == "mismatch"
    assert "piles.draw.known_instances_multiset" in {row["field"] for row in result["differences"]}


def test_pending_draw_ignores_unconsumed_discard_but_keeps_shuffle_unknown():
    """A homogeneous surviving draw pile is deterministic even with an untouched mixed discard."""
    before = {"pending_choice": {}, "endpoint": {"piles": {"draw": 3, "discard": 2}},
              "public_state": {"combat": {"draw": [card("DEFEND_IRONCLAD")] * 3,
                                            "discard": [card("ANGER"), card("STRIKE_IRONCLAD")]}}}
    after = {"endpoint": {"piles": {"draw": 1, "discard": 2}}}
    assert not pending_draw_boundary(before, after)
    after["endpoint"]["piles"] = {"draw": 1, "discard": 0}
    assert pending_draw_boundary(before, after)


def test_non_auto_count_uses_completed_native_rows_including_copies():
    """Copied plays count, automatic plays do not, and incomplete history proves nothing."""
    source = endpoint()
    source["public_card_play_history"] = {"available": True, "complete": True, "plays": [
        {"this_turn": True, "auto_played": False, "first_in_series": True},
        {"this_turn": True, "auto_played": False, "first_in_series": False},
        {"this_turn": True, "auto_played": True, "first_in_series": True}]}
    projected = state_contract(source)
    assert projected["values"]["player.non_auto_card_plays_this_turn"] == 2
    assert "player.non_auto_card_plays_this_turn" in projected["derivations"]
    source["public_card_play_history"]["complete"] = False
    del source["player"]["non_auto_card_plays_this_turn"]
    assert "player.non_auto_card_plays_this_turn" in state_contract(source)["unavailable"]


@pytest.mark.parametrize("mutation,field", [
    (lambda value: value["player"].update(stars=99), "player.stars"),
    (lambda value: value["hand"][0].update(cost=99), "hand[0].energy_cost"),
    (lambda value: value["enemies"][0].update(enemy_id="WRONG"), "enemies[0].id"),
    (lambda value: value["potions"][0].update(potion_id="WRONG"), "potions[0].id"),
    (lambda value: value["pending_choice"]["options"][0].update(card_id="WRONG"), "pending_choice.options[0].id"),
    (lambda value: value["pile_cards"]["draw"][0].update(card_id="WRONG"), "piles.draw.id_multiset"),
])
def test_new_contract_detects_previously_ignored_state(mutation, field):
    """Every documented blind spot must produce a visible field mismatch."""
    expected, actual = endpoint(), endpoint()
    mutation(actual)
    result = compare_contract(state_contract(expected), state_contract(actual))
    assert result["status"] == "mismatch"
    assert field in {difference["field"] for difference in result["differences"]}


def test_unknown_cost_is_partial_not_zero_or_full_match():
    """Expired observed quotes remain unknown even when the other engine supplies a cost."""
    expected, actual = endpoint(), endpoint()
    actual["hand"][0]["cost"] = None
    result = compare_contract(state_contract(expected), state_contract(actual))
    assert result["status"] == "partial_match"
    assert not result["coverage"]["complete"]
    assert "hand[0].energy_cost" in {row["field"] for row in result["unavailable_fields"]}


def test_pile_order_is_not_compared_but_multiplicity_is():
    """Unknown draw order may differ while the number of visible copies may not."""
    expected = endpoint()
    expected["pile_cards"]["draw"] = [card(), card("DEFEND_IRONCLAD")]
    expected["piles"]["draw"] = 2
    actual = deepcopy(expected)
    actual["pile_cards"]["draw"].reverse()
    assert compare_contract(state_contract(expected), state_contract(actual))["status"] == "matched"
    actual["pile_cards"]["draw"].append(card())
    assert compare_contract(state_contract(expected), state_contract(actual))["status"] == "mismatch"


def test_dispatch_count_is_not_native_total_evidence():
    """A manual-only legacy counter cannot certify total automatic and repeated plays."""
    expected, actual = endpoint(), endpoint()
    expected["play_count_scope"] = "manual_dispatch_only"
    result = compare_contract(state_contract(expected), state_contract(actual))
    assert result["status"] == "partial_match"
    assert "player.attacks_played_this_turn" not in result["compared_fields"]

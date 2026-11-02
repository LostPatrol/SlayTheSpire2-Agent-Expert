"""Verify automatic housekeeping leaves every strategic choice with the model."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from sts2_agent.automatic import automatic_decision


# Reuse the reviewed public reward contract, including old bridge line types.
FIXTURES = Path(__file__).parent / "fixtures" / "public"


def reward_observation():
    """Return a detached old-bridge reward with a visible empty potion belt."""
    return json.loads((FIXTURES / "reward.json").read_text(encoding="utf-8"))


def test_reward_gold_then_open_cards_without_choosing():
    """Claim one reward per observation, never choose or skip a card."""
    obs = reward_observation()
    assert automatic_decision(obs)["action"] == {"type": "claim_reward", "option_index": 0}
    obs["state"]["reward"]["rewards"].pop(0)
    assert automatic_decision(obs)["action"] == {"type": "claim_reward", "option_index": 1}
    obs["state"]["reward"].update(pending_card_choice=True, cards=[{"i": 0, "card_id": "ANGER"}])
    obs["available_actions"] = ["choose_reward_card", "skip_reward_cards"]
    assert automatic_decision(obs) is None


@pytest.mark.parametrize("relic", ["DELICATE_FROND", "PETRIFIED_TOAD", "BELT_BUCKLE", "SOZU", "娇嫩蕨草"])
def test_empty_slot_tradeoffs_leave_potion_for_model(relic):
    """A free slot does not prove that keeping a potion is always beneficial."""
    obs = reward_observation()
    obs["state"]["reward"]["rewards"] = [{"i": 3, "reward_type": "Potion", "claimable": True}]
    obs["state"]["run"]["relic_ids"].append(relic)
    assert automatic_decision(obs) is None


def test_potion_requires_empty_slot_and_uses_native_sparse_index():
    """Unknown/full slots never trigger replacement or a guessed choice."""
    obs = reward_observation()
    obs["state"]["reward"]["rewards"] = [{"i": 7, "line": "Potion: 力量药水", "claimable": True}]
    assert automatic_decision(obs)["action"] == {"type": "claim_reward", "option_index": 7}
    obs["state"]["run"]["potions"] = [{"i": 0, "potion_id": "STRENGTH_POTION", "occupied": True}]
    assert automatic_decision(obs) is None
    obs["state"]["run"].pop("potions")
    assert automatic_decision(obs) is None


def test_proceed_does_not_skip_unhandled_reward_or_card_choice():
    """An enabled proceed button does not authorize discarding unresolved choices."""
    obs = reward_observation()
    obs["state"]["reward"]["rewards"] = [{"i": 2, "line": "Relic: 未知遗物", "claimable": True}]
    assert automatic_decision(obs) is None
    obs["state"]["reward"]["rewards"] = []
    assert automatic_decision(obs)["action"] == {"type": "proceed"}
    obs["state"]["reward"]["pending_card_choice"] = True
    assert automatic_decision(obs) is None


def test_native_potion_procurement_gate_precedes_slot_inference_and_upstream_drain():
    """Native hooks can block a visible empty slot, while upstream has no optional empty-slot take/skip."""
    obs = reward_observation()
    obs["state"]["reward"]["rewards"] = [{"i": 4, "reward_type": "POTION", "claimable": True,
        "potion_id": "FIRE_POTION", "potion_slot_available": True, "potion_procurement_allowed": False,
        "potion_can_receive": False}]
    config = {"simulation": {"run_decision_mode": "upstream"}}
    assert automatic_decision(obs, config) is None
    obs["state"]["reward"]["rewards"][0].update(potion_procurement_allowed=True, potion_can_receive=True)
    obs["state"]["run"]["relic_ids"] = ["BELT_BUCKLE"]
    assert automatic_decision(obs, config)["action"] == {"type": "claim_reward", "option_index": 4}
    assert automatic_decision(obs, {"simulation": {"run_decision_mode": "llm"}}) is None


@pytest.mark.parametrize("owner", ["upstream", "assist", "llm"])
def test_full_belt_swap_precedes_opening_cards_only_for_network_owners(owner):
    """A disabled full-belt reward button still carries a swap decision, while gold remains forced."""
    obs = reward_observation()
    config = {"simulation": {"run_decision_mode": owner}}
    rows = [{"i": 1, "reward_type": "CARD", "claimable": True},
            {"i": 9, "reward_type": "POTION", "potion_id": "FIRE_POTION", "claimable": False,
             "visible": True, "successfully_selected": False,
             "potion_slot_available": False, "potion_procurement_allowed": True, "potion_can_receive": False}]
    obs["state"]["reward"]["rewards"] = [{"i": 4, "reward_type": "GOLD", "claimable": True}, *rows]
    assert automatic_decision(obs, config)["action"] == {"type": "claim_reward", "option_index": 4}
    obs["state"]["reward"]["rewards"] = rows
    decision = automatic_decision(obs, config)
    if owner == "llm":
        assert decision["action"] == {"type": "claim_reward", "option_index": 1}
    else:
        assert decision is None
    rows[1]["potion_procurement_allowed"] = False
    assert automatic_decision(obs, config)["action"] == {"type": "claim_reward", "option_index": 1}


def test_disabled_full_belt_row_cannot_be_silently_abandoned_or_reclaimed():
    """A disabled unclaimed potion still needs policy, whereas an explicitly received or hidden row does not."""
    obs = reward_observation()
    config = {"simulation": {"run_decision_mode": "upstream"}}
    row = {"i": 9, "reward_type": "POTION", "potion_id": "FIRE_POTION", "claimable": False,
        "visible": True, "successfully_selected": False, "potion_slot_available": False,
        "potion_procurement_allowed": True, "potion_can_receive": False}
    obs["state"]["reward"]["rewards"] = [row]
    assert automatic_decision(obs, config) is None
    row.pop("successfully_selected")
    assert automatic_decision(obs, config) is None
    row["successfully_selected"] = True
    row["claimable"] = True
    assert automatic_decision(obs, config)["action"] == {"type": "proceed"}
    row["successfully_selected"] = False
    row["visible"] = False
    assert automatic_decision(obs, config)["action"] == {"type": "proceed"}


def test_only_map_edge_is_automatic_but_route_choice_is_not():
    """Use the option's real index, never infer it from a map coordinate."""
    obs = {"state": {"screen": "MAP", "map": {"options": [{"i": 5, "line": "3,4 未知"}]}},
           "available_actions": ["choose_map_node", "discard_potion"]}
    assert automatic_decision(obs)["action"] == {"type": "choose_map_node", "option_index": 5}
    obs["state"]["map"]["options"].append({"i": 8, "line": "3,5 商店"})
    assert automatic_decision(obs) is None


def test_shop_inventory_is_information_only_and_finished_flow_proceeds():
    """Open shop inventory before considering purchases, then finish a sole forward action."""
    obs = {"state": {"screen": "SHOP"}, "available_actions": ["open_shop_inventory", "proceed", "discard_potion"]}
    assert automatic_decision(obs)["action"] == {"type": "open_shop_inventory"}
    assert automatic_decision(obs, scene_actions=["open_shop_inventory"]) is None
    assert automatic_decision(obs, scene_actions=["open_shop_inventory", "close_shop_inventory"])["action"] == {"type": "proceed"}
    obs["available_actions"] = ["buy_card", "close_shop_inventory"]
    assert automatic_decision(obs) is None
    obs["available_actions"] = ["proceed"]
    assert automatic_decision(obs)["action"] == {"type": "proceed"}
    obs["state"]["screen"] = "EVENT"
    assert automatic_decision(obs) is None


def test_end_turn_requires_explicitly_unplayable_cards_and_no_potions():
    """A playable or unknown card, resource, or ability prevents a forced turn end."""
    obs = {"state": {"screen": "COMBAT", "combat": {"hand": [],
               "player": {"cards_played_this_turn": 1}, "action_readiness": {
                   "can_use_combat_actions": True, "snapshot_stable": True, "actions_settled": True}},
               "run": {"potions": []}},
           "available_actions": ["end_turn"]}
    assert automatic_decision(obs)["action"] == {"type": "end_turn"}
    obs["state"]["combat"]["player"]["cards_played_this_turn"] = 0
    assert automatic_decision(obs) is None
    obs["state"]["combat"]["player"]["cards_played_this_turn"] = 1
    obs["state"]["combat"]["hand"] = [{"i": 0, "playable": False}]
    assert automatic_decision(obs)["action"] == {"type": "end_turn"}
    for card in [{"i": 0, "playable": True}, {"i": 0}]:
        obs["state"]["combat"]["hand"] = [card]
        assert automatic_decision(obs) is None
    obs["state"]["combat"]["hand"] = []
    obs["state"]["run"]["potions"] = [{"i": 0, "potion_id": "ENERGY_POTION"}]
    assert automatic_decision(obs) is None
    obs["state"]["run"]["potions"] = []
    obs["available_actions"].append("use_character_ability")
    assert automatic_decision(obs) is None
    obs["available_actions"] = ["end_turn"]
    obs["state"].pop("run")
    assert automatic_decision(obs) is None


def test_finish_controls_and_disabled_config_do_not_mutate_input():
    """Keep deterministic proposals separately logged and permit explicit opt-out."""
    obs = {"state": {"screen": "CARD_SELECTION"}, "available_actions": ["confirm_selection"]}
    original = deepcopy(obs)
    decision = automatic_decision(obs)
    assert decision["action"] == {"type": "confirm_selection"}
    assert decision["decision_source"] == "automatic"
    assert automatic_decision(obs, {"automatic": {"enabled": False}}) is None
    assert obs == original


def test_card_reward_opens_at_most_once_per_reward_screen():
    """After the choice was opened, the same screen never reopens it; the model decides."""
    obs = reward_observation()
    obs["state"]["reward"]["rewards"].pop(0)          # gold is already claimed
    assert automatic_decision(obs)["action"] == {"type": "claim_reward", "option_index": 1}
    assert automatic_decision(obs, card_reward_opened=True) is None
    # A different screen resets the flag and may open the cards once again.
    assert automatic_decision(obs, card_reward_opened=False)["action"] == {
        "type": "claim_reward", "option_index": 1}

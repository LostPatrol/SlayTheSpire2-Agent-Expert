"""Exercise fixed-plan draws and decisions without hidden-order access or branch leakage."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

# Reuse the isolated host transport and public fixture builders.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
from run import HOST_DLL, SimulatorClient
from scenarios import position


@pytest.fixture(scope="module")
def client():
    """Share a built simulator process across deterministic behavioral checks."""
    assert HOST_DLL.exists(), "Build the managed host before running this suite"
    with SimulatorClient() as simulator:
        yield simulator


def public_card(card_id, *, index=0, upgraded=False, cost=1, target=None):
    """Build a visible hand row while leaving rule values to the pinned core."""
    return {
        "i": index, "card_id": card_id, "upgraded": upgraded,
        "line": f"{card_id}{'+' if upgraded else ''} [{cost}]",
        "energy_cost": cost, "star_cost": 0, "playable": True,
        "target": target, "targets": [0] if target else [],
        "keywords": [], "mods": [],
    }


def pile(card_id, count=1, *, upgraded=False):
    """Represent a public unordered stack with an explicit count and upgrade."""
    return {
        "card_ids": [card_id], "upgraded": upgraded,
        "line": f"{card_id}{'+' if upgraded else ''}*{count} [1]",
        "keywords": [], "mods": [],
    }


def frame(cards, *, draw=None, discard=None, powers=None):
    """Start each test from a fresh settled public frame with controlled pile contents."""
    state = position([], powers=powers)
    state["combat"].update(hand=deepcopy(cards), draw=deepcopy(draw or []),
                           discard=deepcopy(discard or []), exhaust=[])
    for index, card in enumerate(state["combat"]["hand"]):
        card["i"] = index
    return state


def evaluate(client, state, actions, *, samples=1, **limits):
    """Evaluate the supplied action list with no automatic action search."""
    return client.request({
        "op": "evaluate", "public_state": state,
        "candidate_plans": [{"id": "plan", "actions": actions}],
        "samples": samples, "scenario_seed": 5923,
        "max_actions": 8, "max_events": 512, "timeout_ms": 10000, **limits,
    })


def sample(result):
    """Return the sole sample while making transport failures easy to inspect."""
    assert result["ok"], result
    return result["candidates"][0]["samples"][0]


def play(handle, target=None):
    """Use initial-hand identity rather than a shifting simulated hand index."""
    action = {"type": "play_card", "card_handle": handle}
    if target is not None:
        action["target_index"] = target
    return action


def test_drawn_card_can_be_used_by_declared_card_id(client):
    """A predeclared card-ID action can consume a matching revealed draw."""
    state = frame([public_card("SHRUG_IT_OFF")], draw=[pile("STRIKE_IRONCLAD")])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "play_card", "card_id": "STRIKE_IRONCLAD", "target_index": 0,
    }]))
    assert result["status"] == "completed", result
    assert result["state"]["player"]["block"] == 8
    assert result["state"]["player"]["energy"] == 1
    assert result["state"]["enemies"][0]["hp"]["current"] == 94
    assert result["state"]["piles"] == {"draw": 0, "discard": 2, "exhaust": 0}


def test_draw_across_reshuffle_and_draw_from_empty_piles(client):
    """A draw reshuffles a known discard multiset and safely stops when both piles empty."""
    state = frame([public_card("SHRUG_IT_OFF")], discard=[pile("STRIKE_IRONCLAD")])
    drawn = sample(evaluate(client, state, [play("h0")]))
    assert drawn["status"] == "completed", drawn
    assert [card["card_id"] for card in drawn["state"]["hand"]] == ["STRIKE_IRONCLAD"]
    empty = frame([public_card("SHRUG_IT_OFF")])
    ended = sample(evaluate(client, empty, [play("h0")]))
    assert ended["status"] == "completed", ended
    assert ended["state"]["hand"] == []
    assert ended["state"]["player"]["block"] == 8


def test_drawn_instance_preserves_upgrade_and_public_base_growth(client):
    """Pile metadata survives import and a grown Rampage receives Strength only once."""
    grown = pile("RAMPAGE")
    grown["dynamic_values"] = [{
        "name": "Damage", "base_value": 14, "current_value": 17, "enchanted_value": 14,
    }]
    state = frame([public_card("SHRUG_IT_OFF")], draw=[grown], powers=["STRENGTH_POWER 3"])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "play_card", "card_id": "RAMPAGE", "target_index": 0,
    }]))
    assert result["status"] == "completed", result
    assert result["state"]["enemies"][0]["hp"]["current"] == 83
    upgraded = pile("STRIKE_IRONCLAD", upgraded=True)
    upgraded["line"] = "Strike*1 [1]"
    state = frame([public_card("SHRUG_IT_OFF")], draw=[upgraded])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "play_card", "card_id": "STRIKE_IRONCLAD", "target_index": 0,
    }]))
    assert result["status"] == "completed", result
    assert result["state"]["enemies"][0]["hp"]["current"] == 91


def test_full_hand_stops_draw_without_discarding_overflow(client):
    """After playing into a nine-card hand only one of Offering's three draws fits."""
    state = frame([public_card("OFFERING", cost=0)] + [public_card("STRIKE_IRONCLAD")] * 9,
                  draw=[pile("DEFEND_IRONCLAD", 3)])
    result = sample(evaluate(client, state, [play("h0")]))
    assert result["status"] == "completed", result
    assert len(result["state"]["hand"]) == 10
    assert result["state"]["piles"] == {"draw": 2, "discard": 0, "exhaust": 1}
    assert result["state"]["player"]["hp"]["current"] == 74
    assert result["state"]["player"]["energy"] == 5


def test_no_draw_prevents_draw_without_erasing_the_cards_other_effects(client):
    """Battle Trance's public No Draw condition preserves the pile and Shrug's block."""
    state = frame([public_card("SHRUG_IT_OFF")], draw=[pile("STRIKE_IRONCLAD", 2)],
                  powers=["NO_DRAW_POWER 1"])
    result = sample(evaluate(client, state, [play("h0")]))
    assert result["status"] == "completed", result
    assert result["state"]["player"]["block"] == 8
    assert result["state"]["piles"]["draw"] == 2
    assert result["state"]["hand"] == []


def test_burning_pact_requires_a_declared_choice(client):
    """Drawing cannot continue until a candidate plan answers the visible exhaust choice."""
    state = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD"),
                   public_card("DEFEND_IRONCLAD")], draw=[pile("STRIKE_IRONCLAD", 3)])
    result = sample(evaluate(client, state, [play("h0")]))
    assert result["status"] == "needs_decision", result
    assert result["state"]["piles"]["draw"] == 3
    assert result["state"]["piles"]["exhaust"] == 0


def test_burning_pact_resumes_exhaust_then_draw_exactly_once(client):
    """A selected exhaust pays Feel No Pain once before the deferred two-card draw."""
    state = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD"),
                   public_card("DEFEND_IRONCLAD")], draw=[pile("STRIKE_IRONCLAD", 3)],
                  powers=["FEEL_NO_PAIN_POWER 3"])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "select_card", "choice_index": 0,
    }]))
    assert result["status"] == "completed", result
    assert result["state"]["player"]["block"] == 3
    assert result["state"]["player"]["energy"] == 2
    assert len(result["state"]["hand"]) == 3
    assert result["state"]["piles"] == {"draw": 1, "discard": 1, "exhaust": 1}
    assert len(result["events"]) == 2


def test_armaments_choice_upgrades_only_the_selected_card(client):
    """The chosen Strike becomes upgraded while the unselected Defend keeps its base form."""
    state = frame([public_card("ARMAMENTS"), public_card("STRIKE_IRONCLAD"),
                   public_card("DEFEND_IRONCLAD")])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "select_card", "choice_index": 0,
    }, play("h1", 0)]))
    assert result["status"] == "completed", result
    assert result["state"]["player"]["block"] == 5
    assert result["state"]["enemies"][0]["hp"]["current"] == 91
    assert result["state"]["hand"][0]["upgraded"] is False


def test_true_grit_upgraded_exhausts_the_selected_card(client):
    """True Grit+ exhausts Defend and leaves the declared Strike available afterward."""
    state = frame([public_card("TRUE_GRIT", upgraded=True), public_card("STRIKE_IRONCLAD"),
                   public_card("DEFEND_IRONCLAD")], powers=["FEEL_NO_PAIN_POWER 3"])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "select_card", "choice_index": 1,
    }, play("h1", 0)]))
    assert result["status"] == "completed", result
    assert result["state"]["player"]["block"] == 12
    assert result["state"]["enemies"][0]["hp"]["current"] == 94
    assert result["state"]["piles"]["exhaust"] == 1


def test_random_exhaust_cannot_reassign_an_upgraded_duplicate_identity(client):
    """Unreviewed random removal rejects atomically instead of reassigning another copy's handle."""
    state = frame([public_card("TRUE_GRIT"), public_card("STRIKE_IRONCLAD"),
                   public_card("STRIKE_IRONCLAD", upgraded=True)])
    result = evaluate(client, state, [play("h0")], samples=12)
    assert result["ok"], result
    for branch in result["candidates"][0]["samples"]:
        assert branch["status"] == "unsupported", branch
        assert branch["stop_reason"] == "random_hand_removal_identity_not_reviewed"
        assert branch["events"] == []
        assert [(card["handle"], card["upgraded"]) for card in branch["state"]["hand"]] == [
            ("h0", False), ("h1", False), ("h2", True),
        ]
        assert branch["state"]["player"]["block"] == 0
        assert branch["state"]["player"]["energy"] == 3


@pytest.mark.parametrize("boundary", ["corruption", "deferred_reshuffle", "iron_club", "letter_opener"])
def test_unreviewed_post_choice_continuations_remain_unsupported(client, boundary):
    """Choices cannot publish exact outcomes while their source-card hooks still need suspension."""
    state = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD"),
                   public_card("DEFEND_IRONCLAD")], draw=[pile("STRIKE_IRONCLAD", 3)])
    if boundary == "corruption":
        state["combat"]["player"]["powers"] = [
            "CORRUPTION_POWER 1", "FEEL_NO_PAIN_POWER 3", "JUGGERNAUT_POWER 5",
        ]
    elif boundary == "deferred_reshuffle":
        state["combat"]["draw"] = [pile("STRIKE_IRONCLAD", 2)]
        state["combat"]["player"]["powers"] = ["DARK_EMBRACE_POWER 1"]
    elif boundary == "iron_club":
        state["run"].update(relic_ids=["IRON_CLUB"], relic_stacks=[3])
    else:
        state["run"].update(relic_ids=["LETTER_OPENER"], relic_stacks=[None])
        state["combat"]["player"]["skills_played_this_turn"] = 2
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "select_card", "choice_index": 0,
    }]))
    assert result["status"] == "unsupported", result
    assert result["events"] == []
    assert result["state"]["piles"]["exhaust"] == 0
    assert result["state"]["enemies"][0]["hp"]["current"] == 100


def test_candidate_branches_do_not_modify_each_other_or_public_input(client):
    """One branch's draw and card play cannot alter a stop-only sibling or the source."""
    state = frame([public_card("SHRUG_IT_OFF")], draw=[pile("STRIKE_IRONCLAD")])
    original = deepcopy(state)
    result = client.request({
        "op": "evaluate", "public_state": state, "samples": 2, "scenario_seed": 17,
        "candidate_plans": [{"id": "draw", "actions": [play("h0")]},
                            {"id": "stop", "actions": []}],
    })
    assert result["ok"], result
    left, right = result["candidates"]
    for branch in left["samples"]:
        assert branch["state"]["player"]["block"] == 8
    for branch in right["samples"]:
        assert branch["state"]["player"]["block"] == 0
        assert branch["state"]["player"]["energy"] == 3
        assert branch["state"]["piles"]["draw"] == 1
    assert state == original


def test_unknown_pile_order_and_raw_rng_do_not_change_sampled_results(client):
    """Canonical unknown multisets make equal public states invariant to hidden ordering."""
    state = frame([public_card("SHRUG_IT_OFF")],
                  draw=[pile("STRIKE_IRONCLAD"), pile("DEFEND_IRONCLAD")])
    other = deepcopy(state)
    other["combat"]["draw"].reverse()
    state.update(seed="secret-a", rng={"call_count": 17})
    other.update(seed="secret-b", rng={"call_count": 999})
    state["combat"]["hidden_order"] = [1, 0]
    other["combat"]["hidden_order"] = [0, 1]
    first = evaluate(client, state, [play("h0")], samples=12)
    second = evaluate(client, other, [play("h0")], samples=12)
    assert first["ok"] and second["ok"], (first, second)
    for left, right in zip(first["candidates"][0]["samples"], second["candidates"][0]["samples"]):
        assert left["status"] == right["status"]
        assert left["state"] == right["state"]


@pytest.mark.parametrize("bad_stack", [
    {"line": "Unknown*1 [1]", "card_ids": ["NOT_A_KNOWN_CARD"]},
    {"line": "Merged*1 [1]", "card_ids": ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]},
    {"line": "STRIKE_IRONCLAD*1 [1]", "card_ids": ["STRIKE_IRONCLAD"],
     "enchantment": {"id": "UNREVIEWED_ENCHANTMENT", "amount": 1}},
])
def test_unknown_pile_rules_are_not_replaced_with_a_wound(client, bad_stack):
    """A missing identity or unaudited overlay cannot become a canonical sampled card."""
    state = frame([public_card("SHRUG_IT_OFF")], draw=[bad_stack])
    result = sample(evaluate(client, state, [play("h0")]))
    assert result["status"] == "unsupported", result
    assert not any(card["card_id"] == "WOUND" for card in result["state"]["hand"])


def test_missing_drawn_card_policy_stops_for_a_new_decision(client):
    """A planned ID absent from the revealed hand never triggers a hidden best-card search."""
    state = frame([public_card("SHRUG_IT_OFF")], draw=[pile("DEFEND_IRONCLAD")])
    result = sample(evaluate(client, state, [play("h0"), {
        "type": "play_card", "card_id": "STRIKE_IRONCLAD", "target_index": 0,
    }]))
    assert result["status"] == "needs_decision", result
    assert result["state"]["enemies"][0]["hp"]["current"] == 100
    assert len(result["events"]) == 1


def test_unfinished_samples_are_not_dropped_from_resource_statistics(client):
    """A fixed Strike policy that cannot run in every sample must not publish a partial mean."""
    state = frame([public_card("SHRUG_IT_OFF")],
                  draw=[pile("STRIKE_IRONCLAD"), pile("DEFEND_IRONCLAD")])
    result = evaluate(client, state, [play("h0"), {
        "type": "play_card", "card_id": "STRIKE_IRONCLAD", "target_index": 0,
    }], samples=12)
    assert result["ok"], result
    candidate = result["candidates"][0]
    statuses = [item["status"] for item in candidate["samples"]]
    assert "completed" in statuses and "needs_decision" in statuses, statuses
    assert candidate["sample_count"] == 12
    assert candidate["metrics"] is None


def test_action_limit_reports_incomplete_without_applying_remaining_steps(client):
    """A path budget cannot silently truncate and label a multi-action plan complete."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"])
    result = sample(evaluate(client, state, [play("h0", 0), play("h1", 0)], max_actions=1))
    assert result["status"] == "incomplete", result
    assert result["state"]["enemies"][0]["hp"]["current"] == 94
    assert len(result["events"]) == 1

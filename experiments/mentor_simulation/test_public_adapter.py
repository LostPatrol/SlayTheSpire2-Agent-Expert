"""Exercise production public-state import, identity, uncertainty and continuation contracts."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

# Import only the isolated experiment transport and public fixture builders.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/combat_simulation"))
from native_parity import JsonlProcess
from scenarios import enemy, position

# A build under test may be supplied without deploying it into the active controller.
HOST_EXE = Path(os.environ.get("MENTOR_PRODUCTION_EXE", ROOT / "local/simulation/mentor/mentor-simulation.exe"))


@pytest.fixture(scope="module")
def client():
    """Require an actually compiled production host for behavioral assertions."""
    assert HOST_EXE.is_file(), "Build/deploy the production Mentor host or set MENTOR_PRODUCTION_EXE"
    host = JsonlProcess([str(HOST_EXE)])
    try:
        yield host
    finally:
        host.close()


def public_card(name, index=0, *, cost=1, target=None, upgraded=False):
    """Represent one visible physical card without inventing modified dynamic values."""
    return {"i": index, "card_id": name, "upgraded": upgraded,
            "energy_cost": cost, "star_cost": 0, "costs_x": False,
            "playable": True, "target": target, "targets": [0] if target else [],
            "line": f"{name}{'+' if upgraded else ''} [{cost}]", "mods": [], "keywords": []}


def frame(cards=None):
    """Build a settled public-only frame with explicit empty unknown piles and belt."""
    state = position([])
    state["combat"].update(hand=deepcopy(cards or []), draw=[], discard=[], exhaust=[])
    state["run"]["potions"] = []
    state["available_actions"] = ["play_card", "use_potion", "end_turn"]
    state["supported"] = True
    return state


def evaluate(client, state, actions, *, samples=1):
    """Audit a finite legacy prefix while retaining its continuation between choices."""
    result = client.request({"op": "evaluate", "rollout": False, "public_state": state,
        "candidate_plans": [{"id": "test", "actions": actions}], "samples": samples,
        "max_actions": 8, "max_events": 512, "timeout_ms": 5000, "scenario_seed": 1771})
    assert result.get("ok"), result
    return result["candidates"][0]


def test_public_resources_sparse_enemy_slots_and_power_damage(client):
    """Import existing resources and powers without replaying start hooks or doubling previews."""
    state = frame([public_card("STRIKE_IRONCLAD", index=11, target="enemies")])
    player = state["combat"]["player"]
    player.update(hp="61/87", block=7, energy=4, stars=2,
                  powers=["STRENGTH_POWER 3", "WEAK_POWER 1 [debuff]"])
    state["combat"]["enemies"] = [enemy(hp=40), enemy(hp=50, powers=["VULNERABLE_POWER 2 [debuff]"])]
    for slot, target in zip((3, 8), state["combat"]["enemies"]):
        target["i"] = slot
    state["combat"]["hand"][0]["targets"] = [3, 8]
    state["combat"]["hand"][0]["dynamic_values"] = [
        {"name": "Damage", "base_value": 6, "current_value": 6, "enchanted_value": 6}]
    inspected = client.request({"op": "inspect", "public_state": state})
    assert inspected.get("ok"), inspected
    imported = inspected["state"]
    assert imported["player"]["hp"] == {"current": 61, "max": 87}
    assert (imported["player"]["block"], imported["player"]["energy"], imported["player"]["stars"]) == (7, 4, 2)
    assert [target["i"] for target in imported["enemies"]] == [3, 8]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 11, "target_index": 8}]})
    assert result.get("ok"), result
    assert result["state"]["enemies"][0]["hp"]["current"] == 40
    assert result["state"]["enemies"][1]["hp"]["current"] == 40
    assert result["state"]["player"]["energy"] == 3


def test_unreviewed_overlay_does_not_hide_a_playable_sibling(client):
    """An unsupported physical copy stays visible while an unrelated card remains usable."""
    state = frame([public_card("STRIKE_IRONCLAD", index=4, target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=9)])
    state["combat"]["hand"][0]["affliction"] = {"affliction_id": "UNKNOWN_OVERLAY", "amount": 1}
    good = client.request({"op": "simulate", "public_state": state,
                           "actions": [{"type": "play_card", "card_index": 9}]})
    assert good.get("ok"), good
    assert good["state"]["player"]["block"] == 5
    assert good["state"]["hand"][0]["card_id"] == "STRIKE_IRONCLAD"
    bad = client.request({"op": "simulate", "public_state": state,
                          "actions": [{"type": "play_card", "card_index": 4, "target_index": 0}]})
    assert bad.get("ok") is False
    assert not bad.get("events")
    assert "overlay" in json.dumps(bad).lower() or "affliction" in json.dumps(bad).lower()


def test_unknown_power_never_becomes_an_exact_zero_effect(client):
    """An unknown global hook must remain visible as an explicit support gap."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    state["combat"]["player"]["powers"] = ["UNMODELED_RULE_POWER 2"]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok") is False
    assert "UNMODELED_RULE" in json.dumps(result)
    assert not result.get("events")


def test_unknown_pile_order_and_hidden_fields_do_not_change_scenarios(client):
    """Public multiset normalization happens before IDs or experimental randomness can depend on order."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [
        {"line": "STRIKE_IRONCLAD*1 [1]", "card_ids": ["STRIKE_IRONCLAD"]},
        {"line": "DEFEND_IRONCLAD*1 [1]", "card_ids": ["DEFEND_IRONCLAD"]},
    ]
    other = deepcopy(state)
    other["combat"]["draw"].reverse()
    state.update(seed="private-a", rng={"counter": 7})
    other.update(seed="private-b", rng={"counter": 999})
    state["combat"]["hidden_order"] = [0, 1]
    other["combat"]["hidden_order"] = [1, 0]
    left = evaluate(client, state, [{"type": "play_card", "card_index": 0}], samples=12)
    right = evaluate(client, other, [{"type": "play_card", "card_index": 0}], samples=12)
    assert [sample["state"] for sample in left["samples"]] == [sample["state"] for sample in right["samples"]]
    assert left["completed_samples"] == right["completed_samples"] == 12


def test_unknown_draw_is_not_a_deterministic_prediction(client):
    """The sampled API may evaluate an unknown draw that the exact single-step API rejects."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [
        {"line": "STRIKE_IRONCLAD [1]", "card_ids": ["STRIKE_IRONCLAD"]},
        {"line": "DEFEND_IRONCLAD [1]", "card_ids": ["DEFEND_IRONCLAD"]},
    ]
    exact = client.request({"op": "simulate", "public_state": state,
                            "actions": [{"type": "play_card", "card_index": 0}]})
    assert exact.get("ok") is False
    sampled = evaluate(client, state, [{"type": "play_card", "card_index": 0}], samples=4)
    assert sampled["completed_samples"] == 4
    assert all(sample["state"]["player"]["block"] == 8 for sample in sampled["samples"])


def test_public_fixed_plan_keeps_both_duplicated_pact_choices(client):
    """Production evaluation must not switch to a fresh snapshot or auto-pick the replay choice."""
    state = frame([public_card("BURNING_PACT", index=7), public_card("DEFEND_IRONCLAD", index=8),
                   public_card("STRIKE_IRONCLAD", index=9, target="enemies"),
                   public_card("STRIKE_IRONCLAD", index=10, target="enemies")])
    state["combat"]["draw"] = [{"line": "ANGER*6 [0]", "card_ids": ["ANGER"]}]
    state["run"]["potions"] = [{"i": 5, "potion_id": "DUPLICATOR", "occupied": True,
                                  "usable": True, "target": None, "targets": []}]
    prefix = [{"type": "use_potion", "option_index": 5},
              {"type": "play_card", "card_index": 7}, {"type": "select_card", "choice_index": 0}]
    pending = evaluate(client, state, prefix)["samples"][0]
    assert pending["status"] == "needs_decision"
    assert pending["state"]["pending_choice"] is not None
    assert pending["state"]["piles"]["draw"] == 4
    completed = evaluate(client, state, prefix + [{"type": "select_card", "choice_index": 0}])["samples"][0]
    assert completed["status"] == "completed"
    assert completed["state"]["piles"]["draw"] == 2
    assert completed["state"]["piles"]["discard"] == 1
    assert completed["state"]["piles"]["exhaust"] == 2


def test_unknown_iron_club_counter_is_not_invented_as_zero(client):
    """A cross-combat trigger count absent from public input cannot be reconstructed from this turn alone."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    state["run"].update(relic_ids=["IRON_CLUB"], relic_stacks=[None])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok") is False
    assert "counter" in json.dumps(result).lower() or "history" in json.dumps(result).lower()


def test_visible_scarf_fifth_card_cost_survives_public_reimport(client):
    """A visible fifth-card discount and its public counter must survive a fresh import."""
    state = frame([public_card("BLUDGEON", index=7, cost=0, target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=8, cost=0)])
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4])
    state["combat"]["player"].update(cards_played_this_turn=4, attacks_played_this_turn=4,
                                      skills_played_this_turn=0, energy=3)
    state["combat"]["enemies"] = [enemy(hp=70)]
    inspected = client.request({"op": "inspect", "public_state": state})
    assert inspected["state"]["hand"][0]["cost"] == 0
    result = evaluate(client, state, [{"type": "play_card", "card_index": 7, "target_index": 0},
                                      {"type": "play_card", "card_index": 8}])["samples"][0]
    assert result["status"] == "completed", result
    assert result["state"]["player"]["energy"] == 2
    assert result["state"]["enemies"][0]["hp"]["current"] == 38
    assert result["state"]["player"]["block"] == 5

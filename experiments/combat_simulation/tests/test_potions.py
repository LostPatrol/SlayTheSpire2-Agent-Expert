"""Verify public potion slots, unpowered effects, trigger boundaries and atomic failures."""

from pathlib import Path
import sys

import pytest

# Reuse the same isolated host and public fixtures as the combat adapter checks.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
from run import HOST_DLL, SimulatorClient
from scenarios import enemy, position


@pytest.fixture(scope="module")
def client():
    """Keep one built offline host for the potion behavior checks."""
    assert HOST_DLL.exists(), "Build the combat host before running potion tests"
    with SimulatorClient() as simulator:
        yield simulator


def belt(state, *ids, targets=None, description=None):
    """Add explicit public belt occupancy and current target permissions."""
    state["run"]["potions"] = [
        {"i": i, "potion_id": potion_id, "occupied": True, "usable": True,
         "is_queued": False, "targets": list(targets or []), "description": description or ""}
        for i, potion_id in enumerate(ids)
    ]
    return state


def use(client, state, slot=0, target=None, extra=()):
    """Resolve the same public action shape that the controller sends."""
    action = {"type": "use_potion", "option_index": slot}
    if target is not None:
        action["target_index"] = target
    return client.request({"op": "simulate", "public_state": state, "actions": [action, *extra]})


def test_block_potion_is_unpowered_and_consumes_only_its_slot(client):
    """Dexterity, Frail and NoBlock do not alter non-card potion block."""
    state = belt(position([], block=3, powers=["DEXTERITY_POWER 5", "FRAIL_POWER 1", "NO_BLOCK_POWER 1"]),
                 "BLOCK_POTION", "ENERGY_POTION", description="Gain 12 Block.")
    # Each potion keeps its own description; the second slot has no scalar override.
    state["run"]["potions"][1]["description"] = "Gain [energy:2]."
    result = use(client, state)
    assert result["ok"]
    assert result["state"]["player"]["block"] == 15
    assert [slot["occupied"] for slot in result["state"]["potions"]] == [False, True]
    assert result["state"]["player"]["cards_played_this_turn"] == 0


def test_fire_potion_ignores_powered_modifiers_and_keeps_enemy_slots(client):
    """Potion damage does not acquire Strength, Weak, Vulnerable or Thorns retaliation."""
    state = belt(position([], powers=["STRENGTH_POWER 9", "WEAK_POWER 1"],
                          enemies=[enemy(hp=18, powers=["VULNERABLE_POWER 2", "THORNS_POWER 4"]), enemy(hp=70)]),
                 "FIRE_POTION", targets=[0, 1], description="Deal 20 damage.")
    result = use(client, state, target=0)
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 0
    assert result["state"]["enemies"][1]["hp"]["current"] == 70
    assert result["state"]["enemies"][1]["i"] == 1
    assert result["state"]["player"]["hp"]["current"] == 80


def test_explosive_ampoule_spends_enemy_block_once_per_target(client):
    """One unpowered area hit hits every living enemy without requiring a target."""
    state = belt(position([], enemies=[enemy(hp=30, block=4), enemy(hp=40, powers=["INTANGIBLE_POWER 1"])]),
                 "EXPLOSIVE_AMPOULE", description="Deal 10 damage to ALL enemies.")
    result = use(client, state)
    assert result["ok"]
    assert [target["hp"]["current"] for target in result["state"]["enemies"]] == [24, 39]


@pytest.mark.parametrize("potion_id,power,amount", [
    ("STRENGTH_POTION", "STRENGTH_POWER", 2),
    ("DEXTERITY_POTION", "DEXTERITY_POWER", 2),
    ("FOCUS_POTION", "FOCUS_POWER", 2),
    ("GHOST_IN_A_JAR", "INTANGIBLE_POWER", 1),
    ("HEART_OF_IRON", "PLATING_POWER", 7),
    ("LIQUID_BRONZE", "THORNS_POWER", 3),
    ("REGEN_POTION", "REGEN_POWER", 5),
])
def test_simple_potion_powers_are_applied_without_future_turn_effects(client, potion_id, power, amount):
    """Current stacks are exact while future healing and side-turn hooks remain outside the horizon."""
    result = use(client, belt(position([]), potion_id))
    assert result["ok"]
    assert f"{power} {amount}" in result["state"]["player"]["powers"]
    assert result["state"]["player"]["hp"]["current"] == 80


@pytest.mark.parametrize("potion_id,power,amount", [
    ("VULNERABLE_POTION", "VULNERABLE_POWER", 3),
    ("WEAK_POTION", "WEAK_POWER", 3),
    ("POISON_POTION", "POISON_POWER", 6),
])
def test_targeted_debuff_potions_respect_artifact(client, potion_id, power, amount):
    """Artifact blocks one complete application and only the selected enemy is affected."""
    state = belt(position([], enemies=[enemy(powers=["ARTIFACT_POWER 1"]), enemy()]), potion_id, targets=[0, 1])
    blocked = use(client, state, target=0)
    applied = use(client, state, target=1)
    assert blocked["ok"] and applied["ok"]
    assert blocked["state"]["enemies"][0]["powers"] == []
    assert f"{power} {amount} [debuff]" in applied["state"]["enemies"][1]["powers"]
    assert applied["state"]["enemies"][0]["powers"] == ["ARTIFACT_POWER 1"]


def test_shackling_potion_uses_temporary_strength_and_artifact(client):
    """A blocked Strength loss does not create a later restoration bonus."""
    state = belt(position([], enemies=[enemy(powers=["ARTIFACT_POWER 1"]), enemy(powers=["STRENGTH_POWER 2"])]),
                 "SHACKLING_POTION")
    result = use(client, state)
    assert result["ok"]
    assert result["state"]["enemies"][0]["powers"] == []
    assert "STRENGTH_POWER -5 [debuff]" in result["state"]["enemies"][1]["powers"]
    assert any(value.startswith("SHACKLING_POTION_POWER 7") for value in result["state"]["enemies"][1]["powers"])


def test_resource_potions_and_heal_clamping(client):
    """Energy gain respects NoEnergyGain and healing never exceeds the observed maximum."""
    energy = use(client, belt(position([], energy=0), "ENERGY_POTION"))
    blocked = use(client, belt(position([], energy=0, powers=["NO_ENERGY_GAIN_POWER 1"]), "ENERGY_POTION"))
    stars = use(client, belt(position([]), "STAR_POTION"))
    heal = use(client, belt(position([], hp=75), "BLOOD_POTION"))
    fruit = use(client, belt(position([], hp=60), "FRUIT_JUICE"))
    assert all(result["ok"] for result in (energy, blocked, stars, heal, fruit))
    assert energy["state"]["player"]["energy"] == 2
    assert blocked["state"]["player"]["energy"] == 0
    assert stars["state"]["player"]["stars"] == 3
    assert heal["state"]["player"]["hp"] == {"current": 80, "max": 80}
    assert fruit["state"]["player"]["hp"] == {"current": 65, "max": 85}


@pytest.mark.parametrize("change,expected", [
    ({"usable": False}, "not_usable"),
    ({"is_queued": True}, "not_usable"),
    ({"description": "Deal 99 damage."}, "visible_amount"),
    ({"potion_id": "ATTACK_POTION"}, "not_reviewed"),
])
def test_potion_rejections_preserve_belt_and_resources(client, change, expected):
    """Unsupported or disabled effects never get replaced by a no-op and consumed."""
    state = belt(position([]), "FIRE_POTION", targets=[0])
    state["run"]["potions"][0].update(change)
    result = use(client, state, target=0)
    assert not result["ok"] and expected in str(result["errors"])
    assert result["state"]["potions"][0]["occupied"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 100


def test_invalid_targets_and_double_consumption_do_not_apply_twice(client):
    """Current public target permissions and immutable belt slots gate every use."""
    state = belt(position([]), "FIRE_POTION", targets=[0])
    invalid = use(client, state, target=1)
    repeated = use(client, state, target=0, extra=[{"type": "use_potion", "option_index": 0, "target_index": 0}])
    assert not invalid["ok"] and invalid["state"]["potions"][0]["occupied"]
    assert not repeated["ok"] and len(repeated["events"]) == 1
    assert repeated["state"]["enemies"][0]["hp"]["current"] == 80


def test_unknown_relic_hook_and_unreviewed_damage_reaction_block_potions(client):
    """Unreviewed potion hooks and creature reactions cannot silently disappear."""
    relic = belt(position([]), "BLOCK_POTION")
    relic["run"].update(relic_ids=["REPTILE_TRINKET"], relic_stacks=[None])
    rejected_relic = use(client, relic)
    reaction = belt(position([], enemies=[enemy(powers=["SLUMBER_POWER 2"])]), "FIRE_POTION", targets=[0])
    rejected_damage = use(client, reaction, target=0)
    assert not rejected_relic["ok"] and "relic_rule_not_reviewed" in str(rejected_relic["errors"])
    assert not rejected_damage["ok"] and "reaction_not_reviewed" in str(rejected_damage["errors"])


def test_kill_trigger_unknown_draw_rejects_entire_potion_atomically(client):
    """A lethal potion cannot leak its damage or consumption when Horn draws unknown cards."""
    state = belt(position([], enemies=[enemy(hp=10), enemy(hp=70)]), "FIRE_POTION", targets=[0, 1])
    state["run"].update(relic_ids=["GREMLIN_HORN"], relic_stacks=[None])
    result = use(client, state, target=0)
    assert not result["ok"] and "boundary" in str(result["errors"])
    assert result["state"]["enemies"][0]["hp"]["current"] == 10
    assert result["state"]["player"]["energy"] == 3
    assert result["state"]["potions"][0]["occupied"]


def test_potion_then_card_preserves_hand_handles_and_enemy_death_identity(client):
    """Killing the first enemy with a potion does not retarget the next declared card."""
    state = belt(position(["STRIKE_IRONCLAD"], enemies=[enemy(hp=10), enemy(hp=30)]),
                 "FIRE_POTION", targets=[0, 1])
    result = use(client, state, target=0, extra=[{
        "type": "play_card", "card_handle": "h0", "target_index": 1,
    }])
    assert result["ok"] and len(result["events"]) == 2
    assert [target["hp"]["current"] for target in result["state"]["enemies"]] == [0, 24]
    assert result["state"]["player"]["energy"] == 2
    assert not result["state"]["potions"][0]["occupied"]


def test_candidate_potion_consumption_and_strength_do_not_leak_between_branches(client):
    """An independently evaluated no-potion candidate retains its belt and original damage."""
    state = belt(position(["STRIKE_IRONCLAD"]), "STRENGTH_POTION")
    state["combat"].update(draw=[], discard=[], exhaust=[])
    play = {"type": "play_card", "card_handle": "h0", "target_index": 0}
    result = client.request({
        "op": "evaluate", "public_state": state, "samples": 1, "scenario_seed": 8521,
        "candidate_plans": [
            {"id": "with_potion", "actions": [{"type": "use_potion", "option_index": 0}, play]},
            {"id": "without_potion", "actions": [play]},
        ],
    })
    assert result["ok"]
    boosted, original = [candidate["samples"][0] for candidate in result["candidates"]]
    assert boosted["status"] == original["status"] == "completed"
    assert boosted["state"]["enemies"][0]["hp"]["current"] == 92
    assert original["state"]["enemies"][0]["hp"]["current"] == 94
    assert not boosted["state"]["potions"][0]["occupied"]
    assert original["state"]["potions"][0]["occupied"]

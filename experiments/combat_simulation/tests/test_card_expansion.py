"""Verify Exterminate and Howl from Beyond against their visible current-turn rules."""

import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


# Reuse only the public scenario helpers and isolated JSONL client.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
import run
from scenarios import enemy, position


@pytest.fixture(scope="module")
def client():
    """Use an optional candidate DLL without changing production configuration."""
    host = Path(os.environ.get("STS2_SIMULATION_TEST_DLL", run.HOST_DLL))
    assert host.exists(), "Build the isolated host before testing"
    with patch.object(run, "HOST_DLL", host), run.SimulatorClient() as simulator:
        yield simulator


def card_state(card_id, *, upgraded=False, powers=None, enemies=None, energy=3):
    """Supply current live-style bases separately from displayed modified damage."""
    state = position(["STRIKE_IRONCLAD"], powers=powers, enemies=enemies, energy=energy)
    base = (4 if upgraded else 3) if card_id == "EXTERMINATE" else (21 if upgraded else 16)
    card = state["combat"]["hand"][0]
    card.update(card_id=card_id, upgraded=upgraded, energy_cost=1 if card_id == "EXTERMINATE" else 3,
                target=None, targets=[], line=f"{card_id} [{'1' if card_id == 'EXTERMINATE' else '3'}]: Deal {base} damage to ALL enemies.",
                dynamic_values=[{"name": "Damage", "base_value": base, "current_value": base, "enchanted_value": base}])
    if card_id == "EXTERMINATE":
        card["dynamic_values"].append({"name": "Repeat", "base_value": 4, "current_value": 4, "enchanted_value": 4})
    state["combat"]["draw"] = []
    state["combat"]["discard"] = []
    state["combat"]["exhaust"] = []
    return state


def play(client, state, actions=None):
    """Execute current hand actions without resolving an actual enemy turn."""
    return client.request({"op": "simulate", "public_state": state,
                           "actions": actions or [{"type": "play_card", "card_index": 0}]})


@pytest.mark.parametrize("upgraded,damage", [(False, 12), (True, 16)])
def test_exterminate_hits_all_enemies_four_times_and_discards(client, upgraded, damage):
    """A lethal first creature cannot redirect or suppress the other creatures' hits."""
    state = card_state("EXTERMINATE", upgraded=upgraded, enemies=[enemy(hp=5), enemy(hp=100)])
    result = play(client, state)
    assert result["ok"], result.get("errors")
    assert result["state"]["enemies"][0]["alive"] is False
    assert result["state"]["enemies"][1]["hp"]["current"] == 100 - damage
    assert result["state"]["piles"] == {"draw": 0, "discard": 1, "exhaust": 0}
    assert result["state"]["player"]["energy"] == 2


def test_exterminate_applies_live_modifiers_once_per_hit_and_spends_vigor_once(client):
    """Strength/Vigor/Weak combine with each target's multipliers before per-hit truncation."""
    state = card_state("EXTERMINATE", powers=["STRENGTH_POWER 10", "VIGOR_POWER 8", "WEAK_POWER 1"],
                       enemies=[enemy(powers=["SOAR_POWER 1", "VULNERABLE_POWER 2"]), enemy()])
    state["combat"]["hand"][0]["dynamic_values"][0]["current_value"] = 15
    result = play(client, state)
    assert result["ok"], result.get("errors")
    assert [entry["hp"]["current"] for entry in result["state"]["enemies"]] == [56, 40]
    assert not any(power.startswith("VIGOR_POWER") for power in result["state"]["player"]["powers"])


@pytest.mark.parametrize("upgraded,expected_hp", [(False, 81), (True, 77)])
def test_howl_from_beyond_is_one_aoe_play_without_automatic_exhaust(client, upgraded, expected_hp):
    """Normal play deals the printed hit once and goes to discard even when its text mentions Exhaust."""
    state = card_state("HOWL_FROM_BEYOND", upgraded=upgraded, powers=["STRENGTH_POWER 10"],
                       enemies=[enemy(powers=["SOAR_POWER 1", "VULNERABLE_POWER 2"]), enemy()])
    preview = 31 if upgraded else 26
    state["combat"]["hand"][0]["dynamic_values"][0]["current_value"] = preview
    result = play(client, state)
    assert result["ok"], result.get("errors")
    assert result["state"]["enemies"][0]["hp"]["current"] == expected_hp
    assert result["state"]["enemies"][1]["hp"]["current"] == 100 - preview
    assert result["state"]["piles"] == {"draw": 0, "discard": 1, "exhaust": 0}


def test_exhausted_howl_is_visible_as_unscored_at_stop_instead_of_a_fake_repeat(client):
    """The current-turn horizon never pretends that stopping executes Howl's turn-end hook."""
    state = card_state("HOWL_FROM_BEYOND")
    state["combat"]["exhaust"] = [{"card_ids": ["HOWL_FROM_BEYOND"], "line": "Howl From Beyond [3]"}]
    result = play(client, state, [{"type": "end_turn"}])
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 100
    assert result["state"]["piles"]["exhaust"] == 1
    assert "exhaust:HOWL_FROM_BEYOND:turn_end_auto_play_not_scored" in result["score_support"]["reasons"]


def test_unobserved_exterminate_repeat_override_is_not_silently_ignored(client):
    """A future modified hit count stays unsupported until the core can import it accurately."""
    state = card_state("EXTERMINATE")
    state["combat"]["hand"][0]["dynamic_values"][1].update(base_value=5, current_value=5, enchanted_value=5)
    result = play(client, state)
    assert not result["ok"]
    assert "card_dynamic_base_not_imported:Repeat:4->5" in str(result["errors"])


@pytest.mark.parametrize("upgraded,amount", [(False, 1), (True, 2)])
def test_pyre_spends_current_energy_and_only_adds_future_turn_power(client, upgraded, amount):
    """Pyre's Energy variable controls later turns, not a refund when it is played."""
    state = position(["INFLAME"], energy=3)
    state["combat"]["hand"][0].update(card_id="PYRE", upgraded=upgraded, energy_cost=2,
        line="Pyre [2]", dynamic_values=[{"name": "Energy", "base_value": amount,
        "current_value": amount, "enchanted_value": amount}])
    result = play(client, state)
    assert result["ok"], result.get("errors")
    assert result["state"]["player"]["energy"] == 1
    assert f"PYRE_POWER {amount}" in result["state"]["player"]["powers"]


@pytest.mark.parametrize("power", ["RITUAL_POWER 3", "BATTLEWORN_DUMMY_TIME_LIMIT_POWER 2", "RAMPART_POWER 25"])
def test_future_side_turn_powers_do_not_reapply_during_current_card_play(client, power):
    """Import existing public resources without firing the next side-turn hook."""
    state = position(["STRIKE_IRONCLAD"], enemies=[enemy(block=5, powers=[power])])
    result = play(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert result["ok"], result.get("errors")
    target = result["state"]["enemies"][0]
    assert target["hp"]["current"] == 99 and target["block"] == 0
    assert power in target["powers"]
    assert any("future_side_turn_trigger_not_scored" in reason for reason in result["score_support"]["reasons"])


def test_tender_reduces_current_strength_and_dexterity_after_each_card(client):
    """The first hit uses current Strength, and the next hit uses the post-play tax."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
                     powers=["STRENGTH_POWER 2", "DEXTERITY_POWER 2", "TENDER_POWER 1"])
    result = play(client, state, [{"type": "play_card", "card_index": i, "target_index": 0} for i in (0, 1)])
    assert result["ok"], result.get("errors")
    assert result["state"]["enemies"][0]["hp"]["current"] == 85
    assert not any(power.startswith(("STRENGTH_POWER", "DEXTERITY_POWER"))
                   for power in result["state"]["player"]["powers"])


def test_juggling_only_blocks_the_copy_boundary_and_requires_public_attack_count(client):
    """Known non-triggering attacks remain comparable; generated hand identities remain explicit."""
    state = position(["STRIKE_IRONCLAD"], powers=["JUGGLING_POWER 1"])
    action = [{"type": "play_card", "card_index": 0, "target_index": 0}]
    state["combat"]["player"]["attacks_played_this_turn"] = 0
    assert play(client, state, action)["ok"]
    state["combat"]["player"]["attacks_played_this_turn"] = 2
    assert "hand_generation_or_auto_play_boundary" in str(play(client, state, action)["errors"])
    del state["combat"]["player"]["attacks_played_this_turn"]
    assert "juggling_attack_count_not_observed" in str(play(client, state, action)["errors"])


def test_minion_does_not_keep_combat_open_after_primary_death(client):
    """A dead leader ends the projection, while killing only a minion does not."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
                     enemies=[enemy(hp=6), enemy(hp=100, damage=30, powers=["MINION_POWER 1"])])
    actions = [{"type": "play_card", "card_index": 0, "target_index": 0},
               {"type": "play_card", "card_index": 1, "target_index": 1}]
    first = play(client, state, actions[:1])
    assert first["ok"] and first["legal_actions"] == []
    assert first["state"]["projected_incoming_damage"] == 0
    assert "position_already_stopped_or_terminal" in str(play(client, state, actions)["errors"])
    state["combat"]["enemies"][0]["hp"] = "100/100"
    state["combat"]["enemies"][1]["hp"] = "6/6"
    minion = play(client, state, [actions[1]])
    assert minion["ok"] and minion["legal_actions"]


def test_galvanic_does_not_block_attacks_or_guess_power_card_afflictions(client):
    """The board Power can coexist with attacks, while per-card Galvanized remains unimported."""
    state = position(["STRIKE_IRONCLAD", "INFLAME"], enemies=[enemy(powers=["GALVANIC_POWER 6"])])
    attack = play(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert attack["ok"] and attack["state"]["player"]["hp"]["current"] == 80
    assert "galvanic_card_affliction_not_imported" in str(play(client, state, [{"type": "play_card", "card_index": 1}])["errors"])


@pytest.mark.parametrize("upgraded,supported", [(True, True), (False, False)])
def test_juggling_upgrade_imports_its_actual_innate_keyword(client, upgraded, supported):
    """Upgrade-granted Innate is known, while an unexplained keyword remains a boundary."""
    state = position(["INFLAME"])
    state["combat"]["hand"][0].update(card_id="JUGGLING", upgraded=upgraded, mods=["Innate"])
    result = play(client, state)
    assert result["ok"] is supported
    if supported:
        assert "JUGGLING_POWER 1" in result["state"]["player"]["powers"]
    else:
        assert "card_modifiers_not_imported" in str(result["errors"])

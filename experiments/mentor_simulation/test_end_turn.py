"""Verify full public round settlement and explicit uncertainty at the production boundary."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

# Reuse the production transport and reviewed public fixture helpers.
ROOT = Path(__file__).resolve().parents[2]
# A running controller may lock deployment; exercise the newly built artifact independently.
HOST_EXE = Path(os.environ.get("MENTOR_PRODUCTION_EXE", ROOT / "local/simulation/mentor/mentor-simulation.exe"))
sys.path.insert(0, str(ROOT / "experiments/combat_simulation"))
from native_parity import JsonlProcess
from test_public_adapter import frame, public_card


@pytest.fixture(scope="module")
def client():
    """Run the actual compiled host without connecting to the installed game."""
    process = JsonlProcess([str(HOST_EXE)])
    try:
        yield process
    finally:
        process.close()


def round_frame(*, damage=8, hits=1, block=0, powers=None, intent="Attack"):
    """Declare a complete visible telegraph with no private AI state or draw order."""
    state = frame()
    state["combat"]["player"].update(hp="50/80", block=block, energy=0, powers=powers or [])
    state["combat"]["enemies"] = [{"i": 0, "enemy_id": "EXOSKELETON", "hp": "40/40",
        "block": 0, "powers": [], "alive": True, "hittable": True,
        "intents": [{"intent_type": intent, "damage": damage, "hits": hits,
                     "total_damage": damage * hits}]}]
    return state


def end_round(client, state, samples=1):
    """Audit one legacy end-turn under independent public-multiset scenarios."""
    result = client.request({"op": "evaluate", "rollout": False, "public_state": state, "samples": samples,
        "candidate_plans": [{"id": "end", "actions": [{"type": "end_turn"}]}]})
    assert result["ok"], result
    return result["candidates"][0]


def test_end_turn_runs_enemy_attack_cleanup_energy_and_next_draw(client):
    """A completed round actually spends HP and prepares the next player's hand."""
    state = round_frame(block=3)
    state["combat"]["hand"] = [public_card("STRIKE_IRONCLAD", target="enemies")]
    state["combat"]["draw"] = [{"card_ids": ["DEFEND_IRONCLAD"], "line": "DEFEND_IRONCLAD*5", "energy_cost": 1}]
    result = end_round(client, state)
    assert result["completed_samples"] == 1, result
    assert result["deterministic_prefix"]["checkpoints"][-1]["stage"] == "before_next_player_draw"
    after = result["samples"][0]["state"]
    assert after["player"]["hp"]["current"] == 45
    assert after["player"]["block"] == 0
    assert after["player"]["energy"] == 3
    assert after["round"] == 2 and after["phase"] == "PlayerPlayPhase"
    assert after["end_turn_executed"] is True
    assert after["stopped"] is True
    assert len(after["hand"]) == 5
    assert after["piles"]["discard"] == 1


def test_enemy_buff_and_player_temporary_strength_expire(client):
    """Native rule hooks, rather than arithmetic subtraction, settle both sides' Powers."""
    state = round_frame(intent="Buff", damage=0,
                        powers=["STRENGTH_POWER 5", "FLEX_POTION_POWER 5"])
    result = end_round(client, state)
    assert result["completed_samples"] == 1, result
    after = result["samples"][0]["state"]
    assert not any("STRENGTH" in power for power in after["player"]["powers"])
    assert "STRENGTH_POWER 2" in after["enemies"][0]["powers"]
    assert after["player"]["hp"]["current"] == 50


def test_flame_barrier_reflects_each_hit_and_expires(client):
    """Enemy multi-hit damage and player retaliation both execute through the core."""
    result = end_round(client, round_frame(damage=1, hits=3, block=3,
                                         powers=["FLAME_BARRIER_POWER 4"]))
    assert result["completed_samples"] == 1, result
    after = result["samples"][0]["state"]
    assert after["player"]["hp"]["current"] == 50
    assert after["enemies"][0]["hp"]["current"] == 28
    assert not any("FLAME_BARRIER" in power for power in after["player"]["powers"])


def test_missing_public_move_is_not_a_completed_round(client):
    """An unidentifiable visible move cannot silently become a harmless enemy turn."""
    result = end_round(client, round_frame(damage=99))
    assert result["completed_samples"] == 0
    assert result["metrics"] is None
    assert "public_intent_move_not_matched" in result["samples"][0]["stop_reason"]


def test_reviewed_outside_combat_relics_do_not_block_card_search(client):
    """Reviewed obtain/deck-add/reward effects need not disable unrelated combat actions."""
    state = round_frame()
    state["combat"]["player"]["energy"] = 3
    state["combat"]["hand"] = [public_card("STRIKE_IRONCLAD", target="enemies")]
    state["run"]["relic_ids"] = ["GOLDEN_PEARL", "LUCKY_FYSH", "FRESNEL_LENS", "PAELS_TOOTH"]
    result = client.request({"op": "search", "strategy": "greedy", "public_state": state})
    assert result["comparison_available"] is True, result
    assert not result["support"]["blocking_reasons"]
    assert result["limits"]["stop_behavior"] == "projection_stop_only"
    assert result["best"]["state"]["end_turn_executed"] is False


@pytest.mark.parametrize(("counter", "energy"), [(0, 3), (1, 3), (2, 4), (5, 4)])
def test_happy_flower_restores_public_count_without_replaying_turn_start(client, counter, energy):
    """The next energy grant follows the displayed persistent count, never the combat round."""
    state = round_frame(intent="Buff", damage=0)
    state["turn"] = 7
    state["run"]["relic_ids"] = ["LARGE_CAPSULE", "HAPPY_FLOWER"]
    state["run"]["relic_stacks"] = [None, counter]
    result = end_round(client, state)
    assert result["completed_samples"] == 1, result
    assert result["samples"][0]["state"]["player"]["energy"] == energy


def test_missing_flower_count_only_restricts_round_settlement(client):
    """Unknown turn timing cannot fabricate energy or disable unrelated current-turn play."""
    state = round_frame(intent="Buff", damage=0)
    state["run"]["relic_ids"] = ["LARGE_CAPSULE", "HAPPY_FLOWER"]
    state["run"]["relic_stacks"] = [None, None]
    state["combat"]["player"]["energy"] = 3
    state["combat"]["hand"] = [public_card("STRIKE_IRONCLAD", target="enemies")]
    search = client.request({"op": "search", "strategy": "greedy", "public_state": state})
    assert search["comparison_available"] is True, search
    result = end_round(client, state)
    assert result["completed_samples"] == 0
    assert result["metrics"] is None
    assert "happy_flower_turn_counter_not_observed" in result["samples"][0]["stop_reason"]


def test_recorded_native_round_hp_after_declared_end_turn(client):
    """Compare real archived native successors without reading hidden runtime state."""
    path = ROOT / "runs/cf676459-7d14-4fe5-8b4b-73b22fc55603/transitions.jsonl"
    if not path.exists():
        pytest.skip("The optional archived native run is not present")
    expected = {8: 74, 26: 72, 52: 60, 183: 62, 334: 0}
    checked = set()
    for line in path.open(encoding="utf-8"):
        item = json.loads(line)
        if item["event_type"] != "decision":
            continue
        data = item["data"]
        episode = int(data["decision"]["decision_episode_id"].rsplit(":", 1)[1])
        if episode not in expected:
            continue
        result = end_round(client, deepcopy(data["observation"]["state"]))
        assert result["completed_samples"] == 1, result
        player = result["samples"][0]["state"]["player"]
        assert player["hp"]["current"] == expected[episode]
        if episode in {26, 183}:
            assert "WEAK_POWER 1 [debuff]" in player["powers"]
        checked.add(episode)
    assert checked == set(expected)


def test_reviewed_energy_relics_do_not_replay_initial_grants(client):
    """Import native max energy once; Lantern cannot grant again on a later turn."""
    state = round_frame(intent="Buff", damage=0)
    state["turn"] = 3
    state["run"].update(max_energy=4, relic_ids=["SPIKED_GAUNTLETS", "LANTERN"])
    result = end_round(client, state)
    assert result["completed_samples"] == 1, result
    assert result["samples"][0]["state"]["player"]["energy"] == 4


@pytest.mark.parametrize(("types", "strength", "dexterity"), [
    (["Attack", "Skill"], 3, 1), (["Attack", "Status"], 2, 0)])
def test_rainbow_ring_restores_public_types_without_counting_status_as_power(client, types, strength, dexterity):
    """The native completed-play types, including auto plays, decide the missing activation bit."""
    state = round_frame()
    state["combat"]["player"]["energy"] = 3
    state["combat"]["hand"] = [public_card("INFLAME", target=None)]
    state["run"]["relic_ids"] = ["RAINBOW_RING"]
    state["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "plays": [{"card_type": kind, "this_turn": True, "auto_played": True} for kind in types]}
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result["ok"], result
    powers = result["state"]["player"]["powers"]
    assert f"STRENGTH_POWER {strength}" in powers
    assert (f"DEXTERITY_POWER {dexterity}" in powers) if dexterity else not any("DEXTERITY" in row for row in powers)


def test_panache_uses_display_count_and_keeps_damage_amount(client):
    """A remaining count of two must not fire on the next play or become the damage amount."""
    state = round_frame(powers=["PANACHE_POWER 10"])
    state["combat"]["player"].update(energy=3, power_details=[
        {"index": 0, "power_id": "PANACHE_POWER", "amount": 10, "display_amount": 2}])
    state["combat"]["hand"] = [public_card("STRIKE_IRONCLAD", target="enemies")]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result["ok"], result
    assert result["state"]["enemies"][0]["hp"]["current"] == 34


def test_exact_mangle_power_id_restores_native_temporary_strength(client):
    """MANGLE_POWER must select the turn-end handler, not the distinct legacy MANGLE kind."""
    state = round_frame()
    state["combat"]["enemies"][0]["powers"] = ["STRENGTH_POWER -10 [debuff]", "MANGLE_POWER 10 [debuff]"]
    state["combat"]["enemies"][0]["intents"][0].update(damage=0, total_damage=0)
    result = end_round(client, state)
    assert result["completed_samples"] == 1, result
    powers = result["samples"][0]["state"]["enemies"][0]["powers"]
    assert not any("MANGLE" in power or "STRENGTH" in power for power in powers)


def test_unknown_next_draw_preserves_deterministic_settlement_before_draw(client):
    """Mixed unseen cards cannot invalidate the resolved enemy attack and pre-draw setup."""
    state = round_frame(block=3)
    state["combat"]["draw"] = [
        {"card_ids": ["STRIKE_IRONCLAD"], "line": "STRIKE_IRONCLAD*3", "energy_cost": 1},
        {"card_ids": ["DEFEND_IRONCLAD"], "line": "DEFEND_IRONCLAD*3", "energy_cost": 1}]
    result = client.request({"op": "simulate", "public_state": state, "actions": [{"type": "end_turn"}]})
    assert not result["ok"] and result["errors"][0]["error"] == "unknown_draw_boundary"
    stages = result["turn_stages"]
    assert stages["status"] == "completed", stages
    endpoint = stages["checkpoints"][-1]
    assert endpoint["stage"] == "before_next_player_draw"
    assert endpoint["state"]["player"]["hp"]["current"] == 45
    assert endpoint["state"]["player"]["energy"] == 3
    assert endpoint["state"]["player"]["block"] == 0
    assert endpoint["state"]["piles"]["draw"] == 6 and endpoint["state"]["retained_hand_count"] == 0
    assert endpoint["state"]["end_turn_executed"] is False
    assert stages["boundary"]["reason"] == "draw_resolution_deferred"


def test_unreviewed_draw_card_does_not_erase_earlier_stages(client):
    """An untouched draw-only overlay remains a later boundary rather than a false early failure."""
    state = round_frame(block=3)
    state["combat"]["draw"] = [{"card_ids": ["DEFEND_IRONCLAD"], "line": "DEFEND_IRONCLAD*5",
                                 "energy_cost": 1, "mods": ["UNKNOWN_DRAW_OVERLAY"]}]
    result = client.request({"op": "simulate", "public_state": state, "actions": [{"type": "end_turn"}]})
    assert not result["ok"]
    endpoint = result["turn_stages"]["checkpoints"][-1]
    assert endpoint["stage"] == "before_next_player_draw"
    assert endpoint["state"]["player"]["hp"]["current"] == 45


def test_unmatched_enemy_action_keeps_player_cleanup_without_applying_enemy_placeholder(client):
    """Failure to identify a later enemy move cannot roll back a known player-end phase."""
    result = client.request({"op": "simulate", "public_state": round_frame(damage=99),
                             "actions": [{"type": "end_turn"}]})
    prefix = result["turn_stages"]
    assert prefix["status"] == "partial" and prefix["boundary"]["stage"] == "enemy_actions_end"
    assert [point["stage"] for point in prefix["checkpoints"]] == [
        "player_turn_end_triggers", "player_turn_end", "enemy_turn_start"]
    assert prefix["checkpoints"][-1]["state"]["player"]["hp"]["current"] == 50


def test_missing_post_draw_flower_counter_does_not_block_pre_draw_setup(client):
    """Happy Flower fires after drawing; its unknown count cannot invalidate earlier reset energy."""
    state = round_frame()
    state["run"]["relic_ids"] = ["HAPPY_FLOWER"]
    result = client.request({"op": "simulate", "public_state": state, "actions": [{"type": "end_turn"}]})
    assert not result["ok"]
    assert result["turn_stages"]["checkpoints"][-1]["stage"] == "before_next_player_draw"
    assert result["turn_stages"]["checkpoints"][-1]["state"]["player"]["energy"] == 3


def residual_fixture(prefix):
    """Load one frozen public root without consulting native or private runtime state."""
    directory = ROOT / "agent/codex/end-turn-cache-20261029/turns/fixtures"
    paths = list(directory.glob(prefix + "-*.json"))
    if not paths:
        pytest.skip("Archived public fixture is not present in this checkout")
    observation = json.loads(paths[0].read_text(encoding="utf-8"))["observation"]
    return {"op": "simulate", "public_state": observation["state"],
            "public_history": observation.get("public_history", {}), "actions": [{"type": "end_turn"}]}


@pytest.mark.parametrize("prefix", ["19", "41", "43"])
def test_static_dynamic_values_allow_normal_hand_cleanup(client, prefix):
    """Known Ashen Strike and Equilibrium bases must not obstruct an ordinary discard."""
    response = client.request(residual_fixture(prefix))
    stages = response["turn_stages"]
    assert "player_turn_end" in [point["stage"] for point in stages["checkpoints"]], stages
    assert "unsupported_card_overlay" not in str(stages.get("boundary", {}))


@pytest.mark.parametrize(("prefix", "current_hits"), [("51", 3), ("52", 4)])
def test_public_second_form_restores_multi_claw_escalation(client, prefix, current_hits):
    """Painful Stabs identifies phase two; the visible hit count restores its extra hits."""
    response = client.request(residual_fixture(prefix))
    stages = response["turn_stages"]
    points = {point["stage"]: point["state"] for point in stages["checkpoints"]}
    before = points["enemy_turn_start"]["player"]
    after = points["enemy_actions_end"]["player"]
    assert after["hp"]["current"] == before["hp"]["current"] - max(0, 7 * current_hits - before["block"])
    assert "next_enemy_intent" in points, stages


@pytest.mark.parametrize(("prefix", "revived_hp"), [("50", 200), ("53", 300)])
def test_public_adaptable_form_revives_without_false_infinite_hp(client, prefix, revived_hp):
    """A visible pending corpse revives once into the form proven by its current powers."""
    response = client.request(residual_fixture(prefix))
    stages = response["turn_stages"]
    point = next(point for point in stages["checkpoints"] if point["stage"] == "enemy_actions_end")
    assert point["state"]["enemies"][0]["hp"]["current"] == revived_hp, point
    assert "infinite_hp" not in str(stages.get("boundary", {}))


def test_unproven_adaptable_form_keeps_explicit_boundary(client):
    """An inconsistent public max HP must not fabricate a revive count."""
    request = residual_fixture("53")
    request["public_state"]["combat"]["enemies"][0]["hp"] = "0/250"
    response = client.request(request)
    assert "public_revive_form_not_observed" in response["turn_stages"]["boundary"]["reason"], response

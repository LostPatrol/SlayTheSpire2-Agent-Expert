"""Check search scoring against the adapter's public combat-terminal semantics."""

import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


# Public scenario builders and the isolated core host share the experiment directory.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
import run
from scenarios import enemy, position


@pytest.fixture(scope="module")
def client():
    """Allow a candidate build to be tested before production deployment."""
    host = Path(os.environ.get("STS2_SIMULATION_TEST_DLL", run.HOST_DLL)).resolve()
    assert host.is_file(), "Build the isolated simulator before testing"
    with patch.object(run, "HOST_DLL", host), run.SimulatorClient() as simulator:
        yield simulator


@pytest.mark.parametrize("strategy", ["greedy", "beam", "exhaustive"])
@pytest.mark.parametrize("power", ["MINION_POWER 1", "ILLUSION_POWER 1"])
def test_primary_kill_wins_without_falsely_killing_or_scoring_surviving_minion(client, strategy, power):
    """A defeated primary ends combat before a live, otherwise-lethal minion attacks."""
    state = position(["STRIKE_IRONCLAD"], hp=1,
                     enemies=[enemy(hp=6), enemy(hp=100, damage=999, powers=[power])])
    result = client.request({"op": "search", "public_state": state, "strategy": strategy, "max_depth": 1})
    assert result["ok"], result.get("reason")
    best = result["best"]
    assert best["actions"][0]["target_index"] == 0
    assert best["termination"] == "combat_terminal"
    assert len(best["actions"]) == 1
    breakdown = best["score_breakdown"]
    assert breakdown["combat_won"] is True
    assert breakdown["projected_incoming_damage"] == breakdown["projected_hp_loss"] == 0
    assert breakdown["projected_lethal"] is False
    assert breakdown["enemy_kills"] == 1
    assert breakdown["enemy_hp_reduction"] == 6
    assert best["state"]["enemies"][1]["alive"] is True
    assert best["state"]["enemies"][1]["hp"]["current"] == 100
    assert best["state"]["player"]["hp"]["current"] == 1


def test_minion_kill_does_not_claim_combat_victory_with_live_primary(client):
    """Only killing the minion leaves the primary enemy and combat active."""
    state = position(["STRIKE_IRONCLAD"], hp=1,
                     enemies=[enemy(hp=100), enemy(hp=6, damage=999, powers=["MINION_POWER 1"])])
    result = client.request({"op": "search", "public_state": state, "strategy": "exhaustive", "max_depth": 1})
    assert result["ok"], result.get("reason")
    best = result["best"]
    assert best["actions"][0]["target_index"] == 1
    assert best["score_breakdown"]["combat_won"] is False
    assert best["state"]["terminal"] is False
    assert best["score_breakdown"]["enemy_kills"] == 1
    assert best["state"]["enemies"][0]["hp"]["current"] == 100

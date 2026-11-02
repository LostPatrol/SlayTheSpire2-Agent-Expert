"""Check creature-bound power payouts and defensive caps in the real compiled core."""

from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


# Reuse the experiment's IPC client, never the running game or its hidden state.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
import run as experiment
from scenarios import enemy, position

# A candidate build can be checked before replacing the deployed simulator.
TEST_DLL = Path(os.environ.get("STS2_SIMULATION_TEST_DLL", str(experiment.HOST_DLL))).resolve()
# Compile only an assertion probe against the real assembly; no combat logic is reimplemented.
CORE_PROBE = """
// Exercise indexed power payouts across a real core enemy death.
using System.Text.Json;
using Sts2Emulator.Core;
using Sts2Emulator.Core.Effects;

// Assassin/Axe Ruby Raider IDs come from the pinned generated enemy table.
const int TargetDefinition = 3;
const int SurvivorDefinition = 5;

/// <summary>Play one real Strike while two creatures hold distinct power amounts.</summary>
static object Resolve(bool lethal, bool survivorHasPowers)
{
    var target = new EnemyState { DefId = TargetDefinition, Hp = lethal ? 1 : 40, MaxHp = 40,
        Buffs = [new(BuffId.Strangle, 5), new(BuffId.Oblivion, 7)] };
    var survivor = new EnemyState { DefId = SurvivorDefinition, Hp = 40, MaxHp = 40,
        Buffs = survivorHasPowers ? [new(BuffId.Strangle, 2), new(BuffId.Oblivion, 3)] : [] };
    var state = new CombatState { PlayerHp = 80, PlayerMaxHp = 80, Energy = 3, MaxEnergy = 3,
        PlayerTurn = true, Hand = [new(IC.StrikeIronclad, false)], Enemies = [target, survivor] };
    CombatEngine.Step(state, 0, new Random(0), 0);
    return new { targetHp = target.Hp, targetDoom = BuffSystem.Get(target.Buffs, BuffId.Doom),
        survivorHp = survivor.Hp, survivorDoom = BuffSystem.Get(survivor.Buffs, BuffId.Doom) };
}

/// <summary>A queued attack and a chosen attack each trigger the same unpowered Rage block.</summary>
static int ResolveQueuedRage()
{
    var state = new CombatState { PlayerHp = 80, PlayerMaxHp = 80, Energy = 3, MaxEnergy = 3,
        PlayerTurn = true, Hand = [new(IC.StrikeIronclad, false)],
        AutoPlayQueue = [new(IC.StrikeIronclad, false)],
        PlayerBuffs = [new(BuffId.Rage, 3), new(BuffId.Frail, 2), new(BuffId.Dexterity, 5)],
        Enemies = [new() { DefId = SurvivorDefinition, Hp = 100, MaxHp = 100 }] };
    CombatEngine.Step(state, 0, new Random(0), 0);
    return state.PlayerBlock;
}

Console.WriteLine(JsonSerializer.Serialize(new { lethal = Resolve(true, true),
    absent = Resolve(true, false), nonlethal = Resolve(false, true), queuedRageBlock = ResolveQueuedRage() }));
"""
# Canonical costs/targets are taken from the fixed generated card table.
SELF_DAMAGE_CARDS = {"BLOODLETTING": (0, None), "BLOOD_WALL": (2, None),
                     "HEMOKINESIS": (1, "enemies"), "OFFERING": (0, None)}


def self_damage_position(card, powers):
    """Build these audited self-damage cards without expanding the shared scenario catalog."""
    state = position(["STRIKE_IRONCLAD"], powers=powers)
    cost, target = SELF_DAMAGE_CARDS[card]
    state["combat"]["hand"][0].update(card_id=card, energy_cost=cost, target=target,
        targets=[0] if target else [], line=f"{card} [{cost}]: self-damage test")
    return state


@pytest.fixture(scope="module")
def client():
    """Bind a compiled offline simulator without a gameplay request or a network call."""
    original = experiment.HOST_DLL
    experiment.HOST_DLL = TEST_DLL
    try:
        with experiment.SimulatorClient() as simulator:
            yield simulator
    finally:
        experiment.HOST_DLL = original


@pytest.fixture(scope="module")
def creature_payouts(tmp_path_factory):
    """Run direct core behavior for powers not yet admitted by the public adapter."""
    directory = tmp_path_factory.mktemp("core-power-probe")
    project = directory / "CoreProbe.csproj"
    project.write_text(
        '<!-- Direct behavioral probe against the selected simulator assembly. -->\n'
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
        '<TargetFramework>net9.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings>'
        '</PropertyGroup><ItemGroup><Reference Include="CombatSimulation.Host">'
        f'<HintPath>{escape(str(TEST_DLL))}</HintPath></Reference></ItemGroup></Project>', encoding="utf-8")
    (directory / "Program.cs").write_text(CORE_PROBE, encoding="utf-8")
    subprocess.run([str(experiment.DOTNET), "build", str(project), "-c", "Release", "--nologo"],
                   check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
    result = subprocess.run([str(experiment.DOTNET), str(directory / "bin/Release/net9.0/CoreProbe.dll")],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=15)
    return json.loads(result.stdout)


@pytest.mark.parametrize("case,target_hp,target_doom,survivor_hp,survivor_doom", [
    ("lethal", 0, 0, 38, 3), ("absent", 0, 0, 40, 0), ("nonlethal", 29, 7, 38, 3),
])
def test_enemy_payouts_stay_with_their_original_creature(creature_payouts, case, target_hp,
                                                       target_doom, survivor_hp, survivor_doom):
    """A killed creature's snapshot cannot move to the next enemy after roster removal."""
    assert creature_payouts[case] == {"targetHp": target_hp, "targetDoom": target_doom,
                                    "survivorHp": survivor_hp, "survivorDoom": survivor_doom}


def test_queued_attack_triggers_rage_without_card_block_modifiers(creature_payouts):
    """The auto-play queue must grant the same three block as the chosen attack."""
    assert creature_payouts["queuedRageBlock"] == 6


@pytest.mark.parametrize("modifiers", [["FRAIL_POWER 2"], ["DEXTERITY_POWER 5"],
                                      ["FRAIL_POWER 2", "DEXTERITY_POWER 5"], ["NO_BLOCK_POWER 2"]])
def test_rage_power_block_ignores_frail_and_dexterity(client, modifiers):
    """Two ordinary attack cards gain exactly three Power block each under card modifiers."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], powers=["RAGE_POWER 3", *modifiers])
    result = client.request({"op": "simulate", "public_state": state, "actions": [
        {"type": "play_card", "card_handle": "h0", "target_index": 0},
        {"type": "play_card", "card_handle": "h1", "target_index": 0}]})
    assert result["ok"], result
    assert result["state"]["player"]["block"] == 6


@pytest.mark.parametrize("card", ["BLOODLETTING", "BLOOD_WALL", "HEMOKINESIS", "OFFERING"])
def test_intangible_caps_each_card_self_damage_and_rupture_still_triggers(client, card):
    """Unblockable self-damage becomes one HP and grants Rupture once, not per lost HP."""
    state = self_damage_position(card, ["INTANGIBLE_POWER 2", "RUPTURE_POWER 2", "NO_DRAW_POWER 1"])
    action = {"type": "play_card", "card_handle": "h0"}
    if state["combat"]["hand"][0]["target"] == "enemies":
        action["target_index"] = 0
    result = client.request({"op": "simulate", "public_state": state, "actions": [action]})
    assert result["ok"], result
    player = result["state"]["player"]
    assert player["hp"]["current"] == 79
    assert "STRENGTH_POWER 2" in player["powers"]
    assert "INTANGIBLE_POWER 2" in player["powers"]


def test_buffer_prevents_intangible_self_damage_without_spurious_rupture(client):
    """Buffer spends one prevention charge; a fully prevented cost does not trigger Rupture."""
    state = self_damage_position("BLOODLETTING", ["INTANGIBLE_POWER 2", "BUFFER_POWER 1", "RUPTURE_POWER 2"])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_handle": "h0"}]})
    assert result["ok"], result
    player = result["state"]["player"]
    assert player["hp"]["current"] == 80
    assert not any(power.startswith(("BUFFER_POWER", "STRENGTH_POWER")) for power in player["powers"])
    assert player["energy"] == 5


@pytest.mark.parametrize("powers,block,hp_after", [
    ([], 0, 76), (["INTANGIBLE_POWER 2"], 0, 78), (["BUFFER_POWER 1"], 0, 78), ([], 3, 79),
])
def test_multihit_thorns_are_separate_damage_instances(client, powers, block, hp_after):
    """Each hit reflects once; block, Intangible and Buffer keep their distinct timing."""
    state = position(["TWIN_STRIKE"], powers=["VIGOR_POWER 4", *powers], block=block,
                     enemies=[enemy(powers=["THORNS_POWER 2"])])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_handle": "h0", "target_index": 0}]})
    assert result["ok"], result
    assert result["state"]["player"]["hp"]["current"] == hp_after
    assert result["state"]["enemies"][0]["hp"]["current"] == 82
    assert not any(power.startswith("VIGOR_POWER") for power in result["state"]["player"]["powers"])

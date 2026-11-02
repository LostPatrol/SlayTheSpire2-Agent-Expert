"""Public diagnostic positions for the isolated current-turn simulator benchmark."""

from copy import deepcopy
import json
from pathlib import Path

# The existing public fixture supplies the protocol shape, not simulator expectations.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests/fixtures/public/combat.json"

# Printed values are diagnostic descriptions; the adapter must use core base rules.
CARD_DATA = {
    "STRIKE_IRONCLAD": (1, "enemies", "造成6点伤害。", "造成9点伤害。"),
    "DEFEND_IRONCLAD": (1, None, "获得5点格挡。", "获得8点格挡。"),
    "BASH": (2, "enemies", "造成8点伤害。给予2层易伤。", "造成10点伤害。给予3层易伤。"),
    "INFLAME": (1, None, "获得2点力量。", "获得3点力量。"),
    "BODY_SLAM": (1, "enemies", "造成等同于格挡的伤害。", "造成等同于格挡的伤害。"),
    "TWIN_STRIKE": (1, "enemies", "造成5点伤害两次。", "造成7点伤害两次。"),
    "BLUDGEON": (3, "enemies", "造成32点伤害。", "造成42点伤害。"),
    "WHIRLWIND": ("X", None, "造成5点伤害X次。", "造成8点伤害X次。"),
    "ANGER": (0, "enemies", "造成6点伤害。将其复制加入弃牌堆。", "造成8点伤害。将其复制加入弃牌堆。"),
    "SHRUG_IT_OFF": (1, None, "获得8点格挡。抽1张牌。", "获得11点格挡。抽1张牌。"),
    "THUNDERCLAP": (1, None, "对所有敌人造成4点伤害并给予1层易伤。", "对所有敌人造成7点伤害并给予1层易伤。"),
    "UPPERCUT": (2, "enemies", "造成13点伤害，给予1层虚弱和易伤。", "造成13点伤害，给予2层虚弱和易伤。"),
}


def enemy(hp=100, damage=0, block=0, powers=None, enemy_id="NIBBIT"):
    """Construct an enemy from visible HP, powers, and its declared attack."""
    return {
        "i": 0, "enemy_id": enemy_id, "name": enemy_id,
        "hp": f"{hp}/{hp}", "block": block, "powers": list(powers or []),
        "alive": True, "hittable": True,
        "intents": [{"i": 0, "intent_type": "Attack" if damage else "Buff",
                     "label": str(damage), "damage": damage, "hits": 1,
                     "total_damage": damage, "status_card_count": None}],
    }


def position(cards, *, energy=3, enemies=None, powers=None, hp=80, block=0):
    """Build a public-only position; a tuple card entry requests its upgraded variant."""
    state = json.loads(FIXTURE.read_text(encoding="utf-8"))["state"]
    state["action_revision"] = 0
    player = state["combat"]["player"]
    player.update(hp=f"{hp}/80", energy=energy, block=block, powers=list(powers or []))
    state["run"].update(hp=f"{hp}/80", relics=[], relic_ids=[], relic_stacks=[], relic_descriptions=[])
    visible_enemies = deepcopy(enemies or [enemy()])
    for index, item in enumerate(visible_enemies):
        item["i"] = index
    state["combat"]["enemies"] = visible_enemies
    hand = []
    for index, entry in enumerate(cards):
        card_id, upgraded = entry if isinstance(entry, tuple) else (entry, False)
        cost, target, text, upgraded_text = CARD_DATA[card_id]
        if card_id == "BODY_SLAM" and upgraded:
            cost = 0
        hand.append({
            "i": index, "card_id": card_id,
            "line": f"{card_id}{'+' if upgraded else ''} [{cost}费]：{upgraded_text if upgraded else text}",
            "upgraded": upgraded, "energy_cost": cost, "star_cost": 0,
            "playable": cost == "X" or cost <= energy,
            "target": target, "targets": list(range(len(visible_enemies))) if target else [],
            "why": None, "unplayable_reason": None, "keywords": [], "mods": [],
        })
    state["combat"]["hand"] = hand
    return state


def diagnostic_cases():
    """Return explicit tactical checks and a deliberately large permutation case."""
    return [
        {"id": "starter", "purpose": "Energy tradeoff with the existing public fixture",
         "state": json.loads(FIXTURE.read_text(encoding="utf-8"))["state"]},
        {"id": "strength_order", "purpose": "A setup power has no immediate damage but improves two attacks",
         "state": position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD", "INFLAME"])},
        {"id": "vulnerable_order", "purpose": "Bash before Strike increases damage",
         "state": position(["STRIKE_IRONCLAD", "BASH"])},
        {"id": "block_body_slam", "purpose": "Block both prevents damage and enables an attack",
         "state": position(["BODY_SLAM", "DEFEND_IRONCLAD"], energy=2, enemies=[enemy(damage=7)])},
        {"id": "kill_prevents_attack", "purpose": "Target choice must account for the enemy attack removed by a kill",
         "state": position(["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"], energy=1,
                           enemies=[enemy(hp=40, damage=3), enemy(hp=6, damage=18)])},
        {"id": "slippery_hits", "purpose": "Multi-hit attacks remove Slippery before a later attack",
         "state": position(["BLUDGEON", "TWIN_STRIKE", "STRIKE_IRONCLAD"],
                           enemies=[enemy(powers=["SLIPPERY_POWER 2"])])},
        {"id": "burrowed_break", "purpose": "Breaking Burrowed block can cancel the declared attack",
         "state": position(["DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], energy=2,
                           enemies=[enemy(block=10, damage=23, powers=["BURROWED_POWER 1"], enemy_id="TUNNELER")])},
        {"id": "x_cost_setup", "purpose": "Energy spent on setup competes with X-cost multi-target damage",
         "state": position(["WHIRLWIND", "INFLAME"], enemies=[enemy(hp=40, damage=8), enemy(hp=40, damage=8)])},
        {"id": "upgraded_duplicates", "purpose": "Copies of one card ID with different upgrades remain distinct",
         "state": position(["STRIKE_IRONCLAD", ("STRIKE_IRONCLAD", True)], energy=1)},
        {"id": "weak_attack", "purpose": "Public Weak changes attack damage without changing printed base data",
         "state": position(["STRIKE_IRONCLAD", "TWIN_STRIKE", "DEFEND_IRONCLAD"],
                           powers=["WEAK_POWER 1"], enemies=[enemy(damage=9)])},
        {"id": "zero_cost_horizon", "purpose": "Explicit finite horizon remains visible with many free cards",
         "max_depth": 3, "state": position(["ANGER"] * 5, energy=0)},
        {"id": "draw_boundary", "purpose": "Unknown draw cannot become a secretly known future card",
         "state": position(["SHRUG_IT_OFF", "STRIKE_IRONCLAD"], energy=2, enemies=[enemy(damage=10)])},
        {"id": "area_vulnerable", "purpose": "Area damage and debuff order compete with targeted attacks",
         "state": position(["THUNDERCLAP", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
                           enemies=[enemy(hp=18, damage=8), enemy(hp=12, damage=10)])},
        {"id": "large_permutations", "purpose": "Exhaustive search must skip above the preset terminal-plan limit",
         "state": position(["ANGER"] * 10, energy=0, enemies=[enemy(hp=10000)] * 3)},
    ]

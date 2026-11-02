"""Validate native fixture identity, rich public snapshots, and replay-aware counters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from native_oracle import DEFAULT_CHECKOUT, NativeOracle

# These native relics trigger on total completed plays, including copied plays.
COUNT_RELICS = ("ORNAMENTAL_FAN", "KUNAI", "SHURIKEN", "LETTER_OPENER")


def check(condition, message):
    """Fail the recorded case on a violated native contract."""
    if not condition:
        raise AssertionError(message)


def scenarios():
    """Keep sentinel cases focused on previously missed initializer and snapshot semantics."""
    cases = [{"id": "omitted_piles", "setup": {"hand": ["STRIKE_IRONCLAD"] * 6, "energy": 3}, "actions": []}]
    for relic in COUNT_RELICS:
        skill = relic == "LETTER_OPENER"
        cases.append({"id": relic, "setup": {"hand": ["DEFEND_IRONCLAD" if skill else "STRIKE_IRONCLAD"] * 2,
            "draw": [], "discard": [], "exhaust": [], "energy": 3, "relics": [relic],
            "potions": ["DUPLICATOR"], "encounter": "NIBBITS_NORMAL"},
            "actions": [{"type": "use_potion", "option_index": 0},
                {"type": "play_card", "card_index": 0, **({} if skill else {"target_index": 0})},
                {"type": "play_card", "card_index": 0, **({} if skill else {"target_index": 1})}]})
    cases.extend([
        {"id": "powered_preview", "setup": {"hand": ["INFLAME", "STRIKE_IRONCLAD"], "energy": 3},
         "actions": [{"type": "play_card", "card_index": 0}]},
        {"id": "current_free_cost", "setup": {"hand": ["CORRUPTION", "DEFEND_IRONCLAD"], "energy": 3},
         "actions": [{"type": "play_card", "card_index": 0}]},
        {"id": "sparse_enemy_slot", "setup": {"hand": [{"card_id": "BLUDGEON", "upgraded": True},
            "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], "energy": 10, "encounter": "NIBBITS_NORMAL"},
         "actions": [{"type": "play_card", "card_index": 0, "target_index": target} for target in (0, 0, 1)]},
        {"id": "nested_selection", "setup": {"hand": ["BURNING_PACT", "DEFEND_IRONCLAD",
            "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], "draw": ["ANGER"] * 6,
            "discard": [], "exhaust": [], "potions": ["DUPLICATOR"]},
         "actions": [{"type": "use_potion", "option_index": 0}, {"type": "play_card", "card_index": 0},
            {"type": "select_card", "choice_index": 0}, {"type": "select_card", "choice_index": 0}]},
        {"id": "scarf_ordinary", "setup": {"hand": ["DEFEND_IRONCLAD"] * 5 + ["BLUDGEON"],
            "energy": 10, "relics": ["BRILLIANT_SCARF"], "encounter": "NIBBITS_NORMAL"},
         "actions": [{"type": "play_card", "card_index": 0}] * 5},
        {"id": "scarf_duplicator", "setup": {"hand": ["DEFEND_IRONCLAD"] * 3 + ["BLUDGEON"],
            "energy": 10, "relics": ["BRILLIANT_SCARF"], "potions": ["DUPLICATOR"], "encounter": "NIBBITS_NORMAL"},
         "actions": [{"type": "use_potion", "option_index": 0}] + [{"type": "play_card", "card_index": 0}] * 3},
        {"id": "scarf_havoc_auto", "setup": {"hand": ["HAVOC"] + ["DEFEND_IRONCLAD"] * 3 + ["BLUDGEON"],
            "draw": ["DEFEND_IRONCLAD"], "energy": 10, "relics": ["BRILLIANT_SCARF"], "encounter": "NIBBITS_NORMAL"},
         "actions": [{"type": "play_card", "card_index": 0}] * 4},
    ])
    return cases


def validate(record):
    """Check native outcomes without consulting an emulator's expected values."""
    identity, initial, steps = record["id"], record["initial"], record["steps"]
    final = steps[-1] if steps else initial
    for snapshot in [initial, *steps]:
        endpoint = snapshot["endpoint"]
        check(endpoint["schema_version"] == 2, "rich endpoint schema missing")
        for pile, count in endpoint["piles"].items():
            check(len(endpoint["pile_cards"][pile]) == count, "pile descriptors disagree with native count")
        check(endpoint["public_card_play_history"]["complete"], "native completed plays unavailable")
        check(endpoint.get("pending_choice") == snapshot["pending_choice"], "choice views disagree")
    if identity == "omitted_piles":
        check(len(initial["endpoint"]["hand"]) == 6, "fixture hand count changed")
        check(initial["endpoint"]["piles"] == {"draw": 0, "discard": 0, "exhaust": 0}, "omitted piles retained aliases")
    elif identity in COUNT_RELICS:
        copied = steps[1]["public_state"]["public_history"]
        completed = final["public_state"]["public_history"]
        check(copied["total_card_plays_this_turn"] == 2 and copied["manual_card_plays_this_turn"] == 1,
              "copied native play was confused with a manual series")
        check(completed["total_card_plays_this_turn"] == 3 and completed["manual_card_plays_this_turn"] == 2,
              "third native play not recorded")
        player = final["endpoint"]["player"]
        if identity == "ORNAMENTAL_FAN":
            check(player["block"] == 4, "Fan did not trigger on copied third attack")
        elif identity in ("KUNAI", "SHURIKEN"):
            power = "DEXTERITY_POWER 1" if identity == "KUNAI" else "STRENGTH_POWER 1"
            check(power in player["powers"], "attack counter relic did not trigger")
        else:
            check(player["block"] == 15, "duplicated skill did not settle once per play")
            check(all(before["hp"]["current"] - after["hp"]["current"] == 5 for before, after in
                      zip(initial["endpoint"]["enemies"], final["endpoint"]["enemies"])), "Letter Opener did not hit all enemies")
    elif identity == "powered_preview":
        damage = next(value for value in final["endpoint"]["hand"][0]["dynamic_values"] if value["name"] == "Damage")
        check((damage["base_value"], damage["current_value"]) == (6, 8), "powered preview was replaced with base value")
    elif identity == "current_free_cost":
        check(final["endpoint"]["hand"][0]["energy_cost"] == 0, "live cost modifiers not exported")
    elif identity == "sparse_enemy_slot":
        before = steps[1]
        check(before["endpoint"]["enemies"][0]["alive"] is False, "first enemy not defeated")
        check(before["public_state"]["combat"]["hand"][0]["targets"] == [1], "public target indices were compacted")
        check(before["endpoint"]["enemies"][1]["hp"]["current"] - final["endpoint"]["enemies"][1]["hp"]["current"] == 6,
              "stable target slot was dispatched to another enemy")
    elif identity == "nested_selection":
        check(steps[1]["pending_choice"] is not None and steps[2]["pending_choice"] is not None,
              "native continuation lost a mandatory second choice")
        check(final["pending_choice"] is None and final["endpoint"]["piles"] == {"draw": 2, "discard": 1, "exhaust": 2},
              "nested selection did not complete exactly once")
    elif identity.startswith("scarf_"):
        for snapshot in steps:
            history = snapshot["endpoint"]["public_card_play_history"]
            plays = [play for play in history["plays"] if play["this_turn"]]
            manual = [play for play in plays if not play["auto_played"]]
            count = len(manual)
            # Scarf counts non-auto completed iterations, including copied replays.
            counter = snapshot["public_state"]["run"]["relic_stacks"][0]
            if 1 <= count <= 4:
                check(counter == count, "Scarf display disagrees with non-auto completed plays")
            bludgeon = next(card for card in snapshot["endpoint"]["hand"] if card["card_id"] == "BLUDGEON")
            check(bludgeon["energy_cost"] == (0 if count == 4 else 3), "Scarf free cost uses the wrong play counter")
        if identity == "scarf_duplicator":
            check(len(plays) == 4 and len(manual) == 4
                  and sum(play["first_in_series"] for play in manual) == 3, "copied iteration was not retained")
        elif identity == "scarf_havoc_auto":
            check(len(plays) == 5 and len(manual) == 4
                  and sum(play["auto_played"] for play in plays) == 1, "automatic play did not remain distinct")
        else:
            check(len(plays) == 5 and len(manual) == 5 and final["endpoint"]["player"]["energy"] == 6,
                  "ordinary fifth play did not consume its one free action")


def main():
    """Run isolated native sentinels and retain every initial state and successor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    started = time.perf_counter()
    with NativeOracle(args.checkout, runtime_dir=args.output.parent / "contract-runtime") as oracle:
        for case in scenarios():
            record = {**case, "steps": []}
            try:
                record["initial"] = oracle.init(case["setup"])
                for action in case["actions"]:
                    record["steps"].append(oracle.step(action))
                validate(record)
                record["status"] = "passed"
            except Exception as error:
                record.update(status="failed", error=str(error), stderr=list(oracle.stderr))
            records.append(record)
            print(case["id"], record["status"], record.get("error", ""), flush=True)
    report = {"schema_version": 1, "elapsed_seconds": time.perf_counter() - started,
              "passed": sum(record["status"] == "passed" for record in records), "cases": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["passed"] != len(records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

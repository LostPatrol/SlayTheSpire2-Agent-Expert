"""Exercise actual copied-game actions and pending choices in isolated native fixtures."""

import json
from pathlib import Path
from native_oracle import NativeOracle, DEFAULT_CHECKOUT

# Keep native evidence and transient game stub output out of deliverable sources.
EVIDENCE = DEFAULT_CHECKOUT / "evidence"


def main():
    """Check native outcomes without invoking any emulator or paid policy model."""
    EVIDENCE.mkdir(exist_ok=True)
    reports = []
    with NativeOracle() as oracle:
        for name, setup, actions in [
            ("strike", {"hand": ["STRIKE_IRONCLAD"]}, [{"type": "play_card", "card_index": 0, "target_index": 0}]),
            ("defend", {"hand": ["DEFEND_IRONCLAD"]}, [{"type": "play_card", "card_index": 0}]),
            ("inflame_strike", {"hand": ["INFLAME", "STRIKE_IRONCLAD"]},
             [{"type": "play_card", "card_index": 0}, {"type": "play_card", "card_index": 0, "target_index": 0}]),
            ("armaments", {"hand": ["ARMAMENTS", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]},
             [{"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 0}]),
            ("true_grit_plus", {"hand": [{"card_id": "TRUE_GRIT", "upgraded": True}, "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]},
             [{"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 1}]),
            ("burning_pact", {"hand": ["BURNING_PACT", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"],
                              "draw": ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]},
             [{"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 0}]),
            ("fire_potion", {"hand": [], "potions": ["FIRE_POTION"]},
             [{"type": "use_potion", "option_index": 0, "target_index": 0}]),
            ("block_potion", {"hand": [], "potions": ["BLOCK_POTION"]},
             [{"type": "use_potion", "option_index": 0}]),
        ]:
            # Empty unknown piles are explicit fixture choices, never real hidden information.
            setup = {"draw": [], "discard": [], "exhaust": [], **setup}
            initial = oracle.init(setup)
            steps = [oracle.step(action) for action in actions]
            reports.append({"id": name, "setup": setup, "actions": actions, "initial": initial, "steps": steps})
            endpoint = steps[-1]["endpoint"]
            print(name, json.dumps({"player": endpoint["player"], "enemies": endpoint["enemies"],
                                    "piles": endpoint["piles"]}), flush=True)
    (EVIDENCE / "native-smoke.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    assert reports[0]["initial"]["endpoint"]["enemies"][0]["hp"]["current"] - reports[0]["steps"][-1]["endpoint"]["enemies"][0]["hp"]["current"] == 6
    assert reports[1]["steps"][-1]["endpoint"]["player"]["block"] == 5
    assert reports[2]["steps"][-1]["endpoint"]["player"]["powers"] == ["STRENGTH_POWER 2"]
    assert reports[3]["steps"][0]["pending_choice"] is not None
    assert reports[3]["steps"][-1]["endpoint"]["hand"][0]["upgraded"]
    assert reports[4]["steps"][-1]["endpoint"]["piles"]["exhaust"] == 1
    assert reports[5]["steps"][0]["pending_choice"] is not None
    assert reports[5]["steps"][-1]["endpoint"]["piles"] == {"draw": 0, "discard": 1, "exhaust": 1}
    assert reports[6]["initial"]["endpoint"]["enemies"][0]["hp"]["current"] - reports[6]["steps"][-1]["endpoint"]["enemies"][0]["hp"]["current"] == 20
    assert reports[7]["steps"][-1]["endpoint"]["player"]["block"] == 12
    print("8 native scenarios passed")


if __name__ == "__main__":
    main()

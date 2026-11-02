"""Reproduce and verify native continuation opening a second player choice synchronously."""

import json
from pathlib import Path
import sys
from native_oracle import NativeOracle, DEFAULT_CHECKOUT

# Each invocation writes a separately named evidence file under the private native checkout.
EVIDENCE = DEFAULT_CHECKOUT / "evidence"


def main():
    """Keep native timeout evidence while asserting two distinct mandatory selections survive."""
    tag = sys.argv[1] if len(sys.argv) > 1 else "nested-choice"
    record = {"id": "DUPLICATOR:PACT:two_choices", "steps": []}
    setup = {
        "hand": ["BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
        "draw": ["ANGER"] * 6, "discard": [], "exhaust": [], "potions": ["DUPLICATOR"],
    }
    actions = [{"type": "use_potion", "option_index": 0}, {"type": "play_card", "card_index": 0},
               {"type": "select_card", "choice_index": 0}, {"type": "select_card", "choice_index": 0}]
    with NativeOracle() as oracle:
        try:
            record["initial"] = oracle.init(setup)
            for action in actions:
                state = oracle.step(action)
                record["steps"].append({"action": action, "state": state})
            record["status"] = "completed"
        except Exception as error:
            record.update(status="failed", error=str(error))
        finally:
            record["stderr"] = list(oracle.stderr)
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / f"{tag}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    assert record["status"] == "completed", record.get("error")
    assert record["steps"][1]["state"]["pending_choice"] is not None
    assert record["steps"][2]["state"]["pending_choice"] is not None
    final = record["steps"][3]["state"]
    assert final["pending_choice"] is None
    assert final["endpoint"]["piles"] == {"draw": 2, "discard": 1, "exhaust": 2}
    assert len(final["endpoint"]["hand"]) == 5
    assert final["endpoint"]["player"]["energy"] == 2
    print("Two native Burning Pact selections and the remaining settlement completed exactly once")


if __name__ == "__main__":
    main()

"""Audit isolated upstream headless multi-turn progress without accepting forced recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from benchmark import Host, ROOT

# Upstream recovery cancels/retries or force-finishes combat; any occurrence invalidates a trace.
FORCED_MARKERS = ("cancelling and retrying", "force-ending", "force ending", "force-finishing", "forcing end", "force-end")


def run(assembly, character, turns, timeout):
    """Retain every request/reply and disqualify headless recovery instead of hiding it."""
    os.environ["STS2_VALIDATION_STRICT"] = "1"
    host = Host([ROOT / ".tools/dotnet/dotnet.exe", assembly], timeout)
    events = []
    try:
        ready = host.lines.get(timeout=timeout)
        events.append({"ready": json.loads(ready) if ready else None})

        def command(payload):
            """Persist the actual result before checking the next dependent action."""
            result = host.request(payload)
            events.append({"request": payload, "response": result})
            if result.get("type") == "error":
                raise RuntimeError(result.get("message", "headless_error"))
            return result

        state = command({"cmd": "start_run", "character": character, "seed": "SOLVERVALIDATION", "ascension": 0})
        for _ in range(16):
            if state.get("decision") == "card_reward":
                state = command({"cmd": "action", "action": "skip_card_reward"})
            elif state.get("decision") in ("event", "event_choice"):
                state = command({"cmd": "action", "action": "choose_option", "args": {"option_index": 0}})
            else:
                break
        deck = [f"STRIKE_{character.upper()}", f"DEFEND_{character.upper()}"] * 5
        command({"cmd": "set_player", "hp": 80, "max_hp": 80, "deck": deck, "relics": [], "potions": []})
        command({"cmd": "enter_room", "type": "combat", "encounter": "NIBBITS_NORMAL"})
        command({"cmd": "get_state"})
        for _ in range(turns):
            command({"cmd": "action", "action": "end_turn"})
            command({"cmd": "get_state"})
        status = "completed_probe"
        error = None
    except Exception as failure:
        status, error = "error", str(failure)
    finally:
        host.close()
    forced = [line for line in host.errors if any(marker in line.lower() for marker in FORCED_MARKERS)]
    states = [item["response"] for item in events if item.get("request", {}).get("cmd") == "get_state"]
    observed_turns = [state.get("turn") for state in states]
    advanced = len(observed_turns) == turns + 1 and all(
        later == earlier + 1 for earlier, later in zip(observed_turns, observed_turns[1:]))
    return {"character": character, "status": "invalid_forced_recovery" if forced else status,
            "error": error, "forced_recovery": forced, "trace": events, "stderr": host.errors,
            "observed_turns": observed_turns, "requested_turns_advanced": advanced,
            "scope": "headless_native_progress_probe_not_full_state_parity",
            "end_turn_validated": False}


def main():
    """Run each requested character in a fresh in-memory native process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assembly", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--characters", nargs="+", default=["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"])
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for character in args.characters:
        started = time.perf_counter()
        result = run(args.assembly, character, args.turns, args.timeout)
        result["elapsed_seconds"] = time.perf_counter() - started
        (args.output / f"{character}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"character": character, "status": result["status"], "error": result["error"]}), flush=True)


if __name__ == "__main__":
    main()

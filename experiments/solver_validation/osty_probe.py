"""Check upgraded Unleash dealer attribution using a native Osty Strength fixture."""

import argparse
import json
import os
from pathlib import Path

from benchmark import Host, ROOT


def main():
    """Summon through native cards, grant a witnessed pet power, and observe native damage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assembly", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    os.environ["STS2_VALIDATION_STRICT"] = "1"
    host = Host([ROOT / ".tools/dotnet/dotnet.exe", args.assembly], 25)
    trace = []
    result = {}
    try:
        trace.append({"ready": json.loads(host.lines.get(timeout=25))})

        def command(payload):
            """Record native failures before stopping this probe."""
            response = host.request(payload)
            trace.append({"request": payload, "response": response})
            if response.get("type") == "error":
                raise RuntimeError(response.get("message"))
            return response

        state = command({"cmd": "start_run", "character": "Necrobinder", "seed": "OSTYPROBE", "ascension": 0})
        for _ in range(16):
            if state.get("decision") == "card_reward":
                state = command({"cmd": "action", "action": "skip_card_reward"})
            elif state.get("decision") == "event_choice":
                state = command({"cmd": "action", "action": "choose_option", "args": {"option_index": 0}})
            else:
                break
        command({"cmd": "set_player", "hp": 80, "max_hp": 80, "relics": [], "potions": [],
                 "deck": ["BODYGUARD", "BODYGUARD", "UNLEASH+1", "DEFEND_NECROBINDER", "STRIKE_NECROBINDER"]})
        state = command({"cmd": "enter_room", "type": "combat", "encounter": "NIBBITS_NORMAL"})
        for _ in range(2):
            selected = next(card for card in state["hand"] if card["id"] == "CARD.BODYGUARD")
            state = command({"cmd": "action", "action": "play_card", "args": {"card_index": selected["index"]}})
        injected = command({"cmd": "set_player", "osty_hp": 10, "osty_max_hp": 15, "osty_strength": 3})
        if (injected.get("osty_hp"), injected.get("osty_max_hp"), injected.get("osty_strength")) != (10, 15, 3):
            raise RuntimeError("native_osty_setup_not_verified")
        before = command({"cmd": "get_state"})
        selected = next(card for card in state["hand"] if card["id"] == "CARD.UNLEASH")
        if selected.get("upgraded") is not True:
            raise RuntimeError("native_unleash_upgrade_missing")
        command({"cmd": "action", "action": "play_card", "args": {"card_index": selected["index"], "target_index": 0}})
        after = command({"cmd": "get_state"})
        result = {"status": "observed", "native_hp_damage": before["monsters"][0]["hp"] - after["monsters"][0]["hp"],
                  "before": before, "after": after, "verified_osty": injected}
    except Exception as error:
        result = {"status": "error", "error": str(error)}
    finally:
        host.close()
    result.update(trace=trace, stderr=host.errors, scope="synthetic_native_osty_fixture_no_card_implementation_replacement")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("trace", "stderr", "before", "after")}))


if __name__ == "__main__":
    main()

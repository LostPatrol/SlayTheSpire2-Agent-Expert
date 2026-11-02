"""Replay recorded strict native end-turn trajectories through one continuous production core world."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil

from benchmark import Host, card, position


def canonical(identifier):
    """Compare registry spelling without conflating distinct game entities."""
    value = re.sub("[^A-Z0-9+]", "", identifier.upper().removeprefix("CARD.").removeprefix("POTION.")).removesuffix("POWER")
    return value.removesuffix("+1") + "+" if value.endswith("+1") else value


def convert_card(row, index):
    """Preserve actual native card upgrades while omitting private pile position from public rows."""
    identifier = row["id"].removeprefix("CARD.")
    result = card(identifier, index, upgraded=row.get("upgraded", False))
    if type(row.get("cost")) is int:
        result["energy_cost"] = row["cost"]
    return result


def public_root(decision, detail, character):
    """Project native visible fields explicitly; seed, move IDs and ordered piles stay separate."""
    state = position(character.upper(), [])
    combat = state["combat"]
    player = detail["player"]
    combat["player"].update(hp=f"{player['hp']}/{player['max_hp']}", block=player["block"],
                            energy=player["energy"], stars=decision.get("stars", 0),
                            powers=[f"{row['kind']} {row['amount']}" for row in player.get("powers", [])],
                            orb_capacity=detail.get("orb_slots", 0), orb_details=[])
    state["run"]["max_energy"] = decision.get("max_energy", 3)
    state["run"]["hp"] = f"{player['hp']}/{player['max_hp']}"
    state["turn"] = detail["turn"]
    for destination, origin in (("hand", "hand"), ("draw", "draw_pile"),
                                ("discard", "discard_pile"), ("exhaust", "exhaust_pile")):
        rows = [convert_card(row, index) for index, row in enumerate(decision.get(origin) or [])]
        if destination != "hand":
            for row in rows:
                row.pop("i", None)
        combat[destination] = rows if destination == "hand" else sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    combat["enemies"] = []
    for index, (visible, raw) in enumerate(zip(decision["enemies"], detail["monsters"])):
        intents = [{"intent_type": item["type"], "damage": item.get("damage", 0),
                    "hits": item.get("hits", 1), "total_damage": item.get("damage", 0) * item.get("hits", 1)}
                   for item in visible.get("intents") or []]
        combat["enemies"].append({"i": index, "enemy_id": raw["kind"],
            "hp": f"{raw['hp']}/{raw['max_hp']}", "block": raw["block"], "alive": raw["alive"],
            "hittable": raw["alive"], "powers": [f"{row['kind']} {row['amount']}" for row in raw.get("powers", [])],
            "intents": intents})
    return state


def internal_root(decision, detail):
    """Use only explicitly recorded synthetic oracle internals for the authorized exact arm."""
    return {"schema_version": 1,
        "availability": {"rng": "available", "draw_order": "available", "enemy_state": "available"},
        "rng": deepcopy(decision["rng"]), "draw_order_direction": "top_first",
        "draw_order": [convert_card(row, index) for index, row in enumerate(decision.get("draw_pile") or [])],
        "enemies": [{"index": index, "monster_id": raw["kind"], "move_state": raw["move_state"],
                     "move_history": [], "hidden_state": {}} for index, raw in enumerate(detail["monsters"])]}


def compare(native, simulated):
    """Compare the documented available contract and retain first differences explicitly."""
    expected = {"hp": native["player"]["hp"], "block": native["player"]["block"],
        "energy": native["player"]["energy"], "round": native["turn"], "terminal": native["combat_terminal"],
        "hand": native["hand_count"],
        "draw": native["draw_count"], "discard": native["discard_count"], "exhaust": native["exhaust_count"],
        "hand_ids": [canonical(identifier) for identifier in native["hand_ids"]],
        "enemies": [{"kind": canonical(enemy["kind"]), "hp": enemy["hp"], "block": enemy["block"],
                     "powers": sorted((canonical(power["kind"]), power["amount"]) for power in enemy.get("powers", []))}
                    for enemy in native["monsters"]]}
    actual = {key: simulated.get(key) for key in expected}
    actual["hand_ids"] = [canonical(identifier) for identifier in simulated.get("hand_ids", [])]
    actual["enemies"] = [{"kind": canonical(enemy["kind"].removeprefix("Some(").removesuffix(")")),
        "hp": enemy["hp"], "block": enemy["block"],
        "powers": sorted((canonical(power["kind"]), power["amount"]) for power in enemy.get("powers", []))}
        for enemy in simulated.get("enemies", [])]
    return [{"field": key, "native": expected[key], "simulator": actual[key]} for key in expected if expected[key] != actual[key]]


def snapshot_endpoint(snapshot):
    """Read the same contract from production evaluate's immediate post-prefix snapshot."""
    return {"hp": snapshot["player"]["hp"]["current"], "block": snapshot["player"]["block"],
        "energy": snapshot["player"]["energy"], "round": snapshot["round"], "terminal": snapshot["terminal"],
        "hand": len(snapshot["hand"]), "hand_ids": [row["card_id"] + ("+" if row.get("upgraded") else "") for row in snapshot["hand"]],
        **snapshot["piles"], "enemies": [{"kind": row["enemy_id"], "hp": row["hp"]["current"], "block": row["block"],
            "powers": [{"kind": power.get("power_id", power.get("id", "")), "amount": power["amount"]}
                       for power in row.get("power_details", [])]} for row in snapshot["enemies"]]}


def main():
    """Compare every recorded native turn without generating replacement truth on failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operation", choices=("evaluate", "combat_trial"), default="evaluate")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # Freeze the executable once so all native comparisons address exactly one build.
    source_executable = args.exe.resolve()
    args.exe = args.output.resolve() / "mentor-simulation.exe"
    shutil.copy2(source_executable, args.exe)
    rows = []
    for evidence_path in sorted(args.source.glob("*.json")):
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        trace = evidence["trace"]
        decision = next(item["response"] for item in trace if item.get("request", {}).get("cmd") == "enter_room")
        states = [item["response"] for item in trace if item.get("request", {}).get("cmd") == "get_state"]
        source = public_root(decision, states[0], evidence["character"])
        request = {"op": "combat_trial", "policy": "scripted", "public_state": source,
            "read_access": "internal", "internal_state": internal_root(decision, states[0]),
            "actions": [{"type": "end_turn"} for _ in states[1:]], "max_decisions": len(states), "seed": 719}
        if args.operation == "evaluate":
            request.update(op="evaluate", candidate_plans=[{"id": str(index),
                "actions": [{"type": "end_turn"} for _ in range(index)]} for index in range(len(states))],
                samples=1, simulations=1, max_rollout_depth=1, scenario_seed=719, timeout_ms=10000)
        host = Host(args.exe, 60)
        try:
            response = host.request(request)
        except Exception as error:
            response = {"ok": False, "status": "error", "error": str(error), "stderr": host.errors}
        finally:
            host.close()
        comparisons = []
        successors = response.get("trace", [])
        projected = {}
        if args.operation == "evaluate":
            for candidate in response.get("candidates", []):
                sample = candidate.get("samples", [{}])[0]
                if sample.get("status") == "completed" and isinstance(sample.get("state"), dict):
                    projected[int(candidate["id"])] = snapshot_endpoint(sample["state"])
        for index, native in enumerate(states):
            simulated = (projected.get(index) if args.operation == "evaluate" else
                response.get("initial") if index == 0 else successors[index - 1]["after"] if index <= len(successors) else None)
            comparisons.append({"turn": native["turn"], "status": "missing_simulated_state" if simulated is None else "difference" if compare(native, simulated) else "partial_match",
                                "differences": compare(native, simulated) if simulated is not None else []})
        row = {"character": evidence["character"], "native_source": str(evidence_path.resolve()),
            "executable_sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(),
            "native_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "request": request, "response": response, "comparisons": comparisons,
            "scope": "continuous_internal_snapshot_native_partial_contract_not_full_state_equivalence",
            "uncompared": ["max_hp", "player_stars", "player_powers", "orbs", "pet", "per_card_variables", "future_intents", "private_ai_counters", "rng_cursors", "discard/exhaust_order"]}
        sample_errors = [sample.get("stop_reason") for candidate in response.get("candidates", [])
                         for sample in candidate.get("samples", []) if sample.get("status") != "completed"]
        rows.append({"character": row["character"], "statuses": [item["status"] for item in comparisons],
                     "host_ok": response.get("ok"), "errors": response.get("errors", response.get("error")),
                     "sample_errors": sample_errors})
        (args.output / evidence_path.name).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(rows[-1]), flush=True)
    (args.output / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

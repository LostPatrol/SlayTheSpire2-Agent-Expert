"""Compare fixed synthetic plans with isolated game-DLL and pure-rule processes."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import threading
import time

# Test processes and copied proprietary assemblies stay outside publishable sources.
ROOT = Path(__file__).resolve().parents[2]
# The rule-only probe is deliberately separate from the native oracle and live controller.
MENTOR_EXE = ROOT / "agent/codex/tech-selection-20261028/mentor-target/release/mentor-simulation-probe.exe"
# Bound each transport request independently of the caller's case count.
REQUEST_TIMEOUT = 20
# Exclude AI turns, undisclosed randomness and internal engine counters from this comparison.
COMMON_FIELDS = (
    "player.hp", "player.max_hp", "player.energy", "player.block", "player.powers",
    "enemies", "hand", "piles.draw", "piles.discard", "piles.exhaust", "pending_choice", "potion_count",
)


class JsonlProcess:
    """Use a finite JSONL transport without consulting any live game process."""

    def __init__(self, command):
        """Start one child and dedicate a response queue to this process generation."""
        self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.lines = Queue()
        self.errors = []
        threading.Thread(target=self._stdout, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _stdout(self):
        """Keep complete response lines independent of child startup timing."""
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _stderr(self):
        """Drain diagnostics without allowing an unbounded retained log."""
        for line in self.process.stderr:
            self.errors[:] = (self.errors + [line.rstrip()])[-20:]

    def request(self, request):
        """Return an explicit failure when a backend stalls or exits."""
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=REQUEST_TIMEOUT)
        except Empty:
            raise RuntimeError("backend_timeout") from None
        if line is None:
            raise RuntimeError("backend_exited:" + "\n".join(self.errors))
        return json.loads(line)

    def close(self):
        """Reap only the backend child created by this transport."""
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


def identifier(value):
    """Normalize naming dialects without substituting different game entities."""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def hp_value(value):
    """Read a current HP from public strings, scalar probes or endpoint objects."""
    if isinstance(value, dict):
        return value["current"]
    if isinstance(value, str):
        return int(value.split("/")[0])
    return value


def powers(values):
    """Compare public power IDs and magnitudes while retaining unknown representations."""
    result = []
    for value in values:
        if isinstance(value, dict):
            name, amount = value["id"], value["amount"]
        else:
            match = re.match(r"^(\S+)\s+(-?\d+)", value)
            if not match:
                raise ValueError("power_representation_not_comparable:" + str(value))
            name, amount = match[1], int(match[2])
        key = identifier(name)
        if key.endswith("POWER"):
            key = key[:-5]
        if amount:
            result.append((key, amount))
    return sorted(result)


def normalize(endpoint, *, pending=None, potion_count=None):
    """Project the documented common subset, preserving hand order and duplicate copies."""
    player = endpoint["player"]
    if "powers" not in player or any("powers" not in enemy for enemy in endpoint["enemies"]):
        raise ValueError("public_power_state_missing")
    if potion_count is None and not {"potions", "potion_count"}.intersection(endpoint):
        raise ValueError("public_potion_state_missing")
    raw_hp = player["hp"]
    maximum = (raw_hp["max"] if isinstance(raw_hp, dict) else
               int(raw_hp.split("/")[1]) if isinstance(raw_hp, str) and "/" in raw_hp else player.get("max_hp"))
    if maximum is None:
        raise ValueError("public_max_hp_missing")
    enemies = []
    for enemy in endpoint["enemies"]:
        hp = hp_value(enemy["hp"])
        if hp > 0:
            enemies.append({"hp": hp, "block": enemy["block"], "powers": powers(enemy.get("powers", []))})
    return {
        "player.hp": hp_value(raw_hp), "player.max_hp": maximum,
        "player.energy": player["energy"], "player.block": player["block"],
        "player.powers": powers(player.get("powers", [])), "enemies": enemies,
        "hand": [(identifier(card.get("card_id", card.get("id"))), card["upgraded"])
                 for card in endpoint["hand"]],
        **{"piles." + key: endpoint["piles"][key] for key in ("draw", "discard", "exhaust")},
        "pending_choice": endpoint.get("pending_choice", pending) is not None,
        "potion_count": (potion_count if potion_count is not None else endpoint.get("potion_count",
                         sum(bool(potion.get("occupied")) for potion in endpoint.get("potions", [])))),
    }


def compare(expected, predicted):
    """Compare the frozen schema-1 subset; production validation uses parity_contract instead."""
    differences = [{"field": key, "expected": expected[key], "actual": predicted[key]}
                   for key in COMMON_FIELDS if expected[key] != predicted[key]]
    return {"status": "mismatch" if differences else "matched", "differences": differences,
            "contract_version": 1, "scope": "legacy_common_subset_not_complete_public_state",
            "compared_fields": list(COMMON_FIELDS)}


def mentor_request(setup, initial, actions):
    """Build a synthetic rule probe using only fixture data and initially visible resources."""
    combat = initial["public_state"]["combat"]
    if len(combat["enemies"]) != 1 or combat["enemies"][0]["powers"] or combat["player"]["powers"]:
        raise ValueError("mentor_probe_requires_one_unpowered_initial_enemy_and_player")
    if setup.get("exhaust"):
        raise ValueError("mentor_probe_initial_exhaust_not_imported")
    def cards(name):
        """Keep declared fixture instances separate, including upgrades."""
        return [{"id": item if isinstance(item, str) else item["card_id"],
                 "upgraded": False if isinstance(item, str) else item.get("upgraded", False)}
                for item in setup.get(name, [])]
    converted = []
    for action in actions:
        if action["type"] == "select_card":
            converted.append({"type": "select_card", "choice_indices": [action["choice_index"]]})
        elif action["type"] == "use_potion":
            converted.append({"type": "use_potion", "potion_index": action["option_index"],
                              "target_index": action.get("target_index")})
        else:
            converted.append({**action, "target_index": action.get("target_index")})
    return {
        "scenario": {"hand": cards("hand"), "draw": cards("draw"), "discard": cards("discard"),
                     "potions": setup.get("potions", []), "hp": hp_value(combat["player"]["hp"]),
                     "energy": combat["player"]["energy"], "enemy_hp": hp_value(combat["enemies"][0]["hp"])},
        "actions": converted, "scenario_seed": 719,
    }


def current_action(action, state):
    """Translate scripted dynamic hand indices into the current simulator's branch handles."""
    action = deepcopy(action)
    if action["type"] == "play_card":
        row = action.pop("card_index")
        if row >= len(state["hand"]):
            raise ValueError("scripted_card_absent_from_simulated_hand")
        action["card_handle"] = state["hand"][row]["handle"]
    return action


def current_step(client, initial, plan_id, planned, previous, action):
    """Replay a growing fixed plan while preserving choices inside the same simulated path."""
    planned.append(current_action(action, previous))
    result = client.request({"op": "evaluate", "public_state": initial["public_state"],
        "candidate_plans": [{"id": plan_id, "actions": planned}], "samples": 1,
        "max_actions": 8, "max_events": 512, "timeout_ms": 5000, "scenario_seed": 719})
    if not result.get("ok"):
        raise RuntimeError("current_backend_protocol_failure:" + str(result))
    return result["candidates"][0]["samples"][0]


def artifact_hashes(checkout, mentor_exe):
    """Bind results to the actual rule, oracle and comparison artifacts used in this batch."""
    paths = [Path(__file__), ROOT / "experiments/native_oracle/native_oracle.py",
             checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll",
             checkout / "src/GodotStubs/bin/Debug/net9.0/GodotSharp.dll", checkout / "lib/sts2.dll",
             ROOT / "experiments/combat_simulation/host/bin/Release/net9.0/CombatSimulation.Host.dll",
             Path(mentor_exe)]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in paths}


def case_matrix():
    """Generate a fixed small matrix without using simulator support or outcome to filter cases."""
    cases = []
    cards = [
        ("STRIKE_IRONCLAD", True), ("DEFEND_IRONCLAD", False), ("BASH", True),
        ("ANGER", True), ("IRON_WAVE", True), ("TWIN_STRIKE", True), ("BODY_SLAM", True),
        ("BLUDGEON", True), ("INFLAME", False), ("UPPERCUT", True), ("THUNDERCLAP", False),
        ("HEMOKINESIS", True), ("IMPERVIOUS", False), ("RAGE", False), ("METALLICIZE", False),
        ("BLOODLETTING", False), ("OFFERING", False), ("SHRUG_IT_OFF", False),
        ("POMMEL_STRIKE", True), ("BATTLE_TRANCE", False), ("CORRUPTION", False), ("WHIRLWIND", False),
    ]
    for name, target in cards:
        for upgraded in (False, True):
            action = {"type": "play_card", "card_index": 0}
            if target:
                action["target_index"] = 0
            cases.append({"id": name + ("+" if upgraded else ""),
                          "setup": {"hand": [{"card_id": name, "upgraded": upgraded}]}, "actions": [action]})
    for name, target in (("SHRUG_IT_OFF", False), ("POMMEL_STRIKE", True),
                         ("OFFERING", False), ("BATTLE_TRANCE", False)):
        for pile in ("draw", "discard"):
            action = {"type": "play_card", "card_index": 0}
            if target:
                action["target_index"] = 0
            cases.append({"id": name + ":" + pile, "setup": {"hand": [name],
                          pile: ["STRIKE_IRONCLAD"] * 4}, "actions": [action]})
    for source, upgraded in (("ARMAMENTS", False), ("TRUE_GRIT", True), ("BURNING_PACT", False)):
        for pile in ("draw", "discard"):
            cases.append({"id": source + ":choice:" + pile,
                          "setup": {"hand": [{"card_id": source, "upgraded": upgraded},
                                              "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
                                    pile: ["ANGER"] * 3},
                          "actions": [{"type": "play_card", "card_index": 0},
                                      {"type": "select_card", "choice_index": 0}]})
    cases.append({"id": "CORRUPTION:PACT:choice:reshuffle", "setup": {
        "hand": ["CORRUPTION", "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
        "discard": ["ANGER"] * 3}, "actions": [{"type": "play_card", "card_index": 0},
        {"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 0}]})
    for name, targeted in (("BLOCK_POTION", False), ("ENERGY_POTION", False), ("FIRE_POTION", True),
                           ("STRENGTH_POTION", False), ("DEXTERITY_POTION", False), ("BLOOD_POTION", False),
                           ("VULNERABLE_POTION", True), ("WEAK_POTION", True), ("POISON_POTION", True)):
        action = {"type": "use_potion", "option_index": 0}
        if targeted:
            action["target_index"] = 0
        cases.append({"id": name + ":STRIKE", "setup": {"hand": ["STRIKE_IRONCLAD"], "potions": [name]},
                      "actions": [action, {"type": "play_card", "card_index": 0, "target_index": 0}]})
    cases.extend([
        {"id": "BLOOD_POTION:injured", "setup": {"hand": [], "hp": 37, "potions": ["BLOOD_POTION"]},
         "actions": [{"type": "use_potion", "option_index": 0}]},
        {"id": "OFFERING:full_hand", "setup": {"hand": ["OFFERING"] + ["STRIKE_IRONCLAD"] * 9,
         "draw": ["ANGER"] * 4}, "actions": [{"type": "play_card", "card_index": 0}]},
        {"id": "BATTLE_TRANCE:no_draw:SHRUG", "setup": {"hand": ["BATTLE_TRANCE", "SHRUG_IT_OFF"],
         "draw": ["STRIKE_IRONCLAD"] * 4}, "actions": [{"type": "play_card", "card_index": 0},
         {"type": "play_card", "card_index": 0}]},
        {"id": "DARK_EMBRACE:PACT:draw", "setup": {
         "hand": ["DARK_EMBRACE", "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
         "draw": ["ANGER"] * 4}, "actions": [{"type": "play_card", "card_index": 0},
         {"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 0}]},
        {"id": "CORRUPTION:DARK_EMBRACE:PACT:reshuffle", "setup": {"energy": 10,
         "hand": ["CORRUPTION", "DARK_EMBRACE", "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
         "discard": ["ANGER"] * 5}, "actions": [{"type": "play_card", "card_index": 0},
         {"type": "play_card", "card_index": 0}, {"type": "play_card", "card_index": 0},
         {"type": "select_card", "choice_index": 0}]},
        {"id": "DUPLICATOR:PACT:two_choices", "setup": {
         "hand": ["BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
         "draw": ["ANGER"] * 6, "potions": ["DUPLICATOR"]},
         "actions": [{"type": "use_potion", "option_index": 0}, {"type": "play_card", "card_index": 0},
         {"type": "select_card", "choice_index": 0}, {"type": "select_card", "choice_index": 0}]},
    ])
    for case in cases:
        for name in ("draw", "discard", "exhaust"):
            case["setup"].setdefault(name, [])
        case["setup"].setdefault("encounter", "NIBBITS_WEAK")
    return cases


def run(cases, *, output, mentor_exe=MENTOR_EXE, oracle_checkout=None):
    """Execute native successors first and compare both independent rule backends at every step."""
    sys.path.insert(0, str(ROOT / "experiments/native_oracle"))
    from native_oracle import DEFAULT_CHECKOUT, NativeOracle

    current = JsonlProcess([str(ROOT / ".tools/dotnet/dotnet.exe"),
                            str(ROOT / "experiments/combat_simulation/host/bin/Release/net9.0/CombatSimulation.Host.dll")])
    mentor = JsonlProcess([str(mentor_exe)]) if Path(mentor_exe).exists() else None
    checkout = Path(oracle_checkout or DEFAULT_CHECKOUT)
    artifacts_before = artifact_hashes(checkout, mentor_exe)
    rows = []
    started = time.perf_counter()
    try:
        for case in cases:
            row = {"id": case["id"], "fixture": deepcopy(case), "steps": []}
            phase = "native_initialization"
            try:
                with NativeOracle(oracle_checkout or DEFAULT_CHECKOUT) as oracle:
                    initial = oracle.init(case["setup"])
                    row["native_initial"] = initial
                    phase = "current_import"
                    current_state = current.request({"op": "inspect", "public_state": initial["public_state"]})["state"]
                    planned = []
                    current_stopped = None
                    for index, action in enumerate(case["actions"]):
                        phase = "native_action"
                        native = oracle.step(action)
                        count = sum(potion.get("occupied", False) for potion in native["public_state"]["run"]["potions"])
                        expected = normalize(native["endpoint"], pending=native["pending_choice"], potion_count=count)
                        step = {"action_index": index, "action": action, "native": native}
                        if current_stopped:
                            step["current"] = {"status": "unsupported", "reason": current_stopped}
                        else:
                            try:
                                sample = current_step(current, initial, case["id"], planned, current_state, action)
                                current_state = sample["state"]
                                comparison = compare(expected, normalize(current_state))
                                step["current"] = {"status": comparison["status"] if sample["status"] == "completed"
                                    else sample["status"], "stop_reason": sample["stop_reason"],
                                    "comparison": comparison, "state": current_state}
                                if sample["status"] not in ("completed", "needs_decision"):
                                    current_stopped = sample["stop_reason"]
                            except Exception as error:
                                current_stopped = str(error)
                                step["current"] = {"status": "failed", "reason": str(error)}
                        if mentor is None:
                            step["mentor"] = {"status": "unavailable", "reason": "mentor_probe_not_built"}
                        else:
                            try:
                                result = mentor.request(mentor_request(case["setup"], initial, case["actions"][:index + 1]))
                                step["mentor"] = {**compare(expected, normalize(result["state"])),
                                                  "state": result["state"], "ok": result.get("ok")}
                                if not result.get("ok"):
                                    step["mentor"]["status"] = "unsupported"
                                    step["mentor"]["error"] = result.get("error")
                            except Exception as error:
                                step["mentor"] = {"status": "incomparable", "reason": str(error)}
                        row["steps"].append(step)
                row["native_status"] = "completed"
            except Exception as error:
                row["native_status"] = "failed" if phase.startswith("native") else "incomplete"
                row["failure_phase"] = phase
                row["error"] = str(error)
            rows.append(row)
            print(json.dumps({"case": row["id"], "native": row["native_status"],
                              **{backend: [step[backend]["status"] for step in row["steps"]]
                                 for backend in ("current", "mentor")}}, ensure_ascii=False), flush=True)
    finally:
        current.close()
        if mentor:
            mentor.close()
    summary = {"cases": len(rows), "native": dict(Counter(row["native_status"] for row in rows)),
               **{backend: dict(Counter(step[backend]["status"] for row in rows for step in row["steps"]))
                  for backend in ("current", "mentor")}}
    report = {"scope": "offline_synthetic_native_dll_common_fields_not_full_game_parity",
              "compared_fields": list(COMMON_FIELDS), "summary": summary, "cases": rows,
              "fixture_hash": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
              "artifacts_before": artifacts_before,
              "artifacts_after": artifact_hashes(checkout, mentor_exe),
              "elapsed_seconds": time.perf_counter() - started,
              "limits": ["native Godot stubs and adapter require separate correctness review",
                         "no enemy AI, turn end, real saves, live RNG, or random draw distribution validation",
                         "needs_decision and unsupported results are not counted as matched",
                         "common-field equality is not exhaustive internal-state equivalence"]}
    provenance = checkout / "evidence/provenance.json"
    report["native_provenance"] = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else None
    report["artifacts_unchanged"] = report["artifacts_before"] == report["artifacts_after"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    """Run the declared matrix or a supplied synthetic-only fixture list."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--mentor-exe", type=Path, default=MENTOR_EXE)
    parser.add_argument("--oracle-checkout", type=Path)
    parser.add_argument("--strict", action="store_true", help="Return nonzero for failures, mismatches or uncovered steps")
    args = parser.parse_args()
    cases = json.loads(args.input.read_text(encoding="utf-8")) if args.input else case_matrix()
    if args.limit is not None:
        cases = cases[:args.limit]
    summary = run(cases, output=args.output, mentor_exe=args.mentor_exe, oracle_checkout=args.oracle_checkout)
    print(json.dumps(summary, ensure_ascii=False))
    if args.strict and (summary["native"] != {"completed": len(cases)} or any(
            status != "matched" and count for backend in ("current", "mentor")
            for status, count in summary[backend].items())):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

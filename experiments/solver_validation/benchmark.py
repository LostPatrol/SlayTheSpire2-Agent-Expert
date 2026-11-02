"""Reproducible continuous-combat trials through the production JSONL host."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import threading
import time

# Paths refer to project-owned artifacts; this runner never contacts a live bridge or model.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE = ROOT / "local/simulation/mentor/mentor-simulation.exe"
# Different policies share a world; policy randomness remains in the host's independent stream.
DEFAULT_SEEDS = (719, 1223)


def card(identifier, index, *, upgraded=False):
    """Describe a physical card without fabricating a modified current cost."""
    return {"i": index, "card_id": identifier, "card_ids": [identifier], "card_count": 1,
            "upgraded": upgraded, "playable": True, "mods": [], "keywords": [],
            "line": identifier + ("+" if upgraded else "") + "*1"}


def enemy(identifier="NIBBIT", hp=65, damage=12, index=0):
    """Supply a synthetic public intent, explicitly separate from a native snapshot."""
    return {"i": index, "enemy_id": identifier, "hp": f"{hp}/{hp}", "block": 0,
            "alive": True, "hittable": True, "powers": [],
            "intents": [{"intent_type": "Attack", "damage": damage, "hits": 1,
                         "total_damage": damage}]}


def position(character, cards, *, hp=65, enemies=None, potions=(), upgraded=False):
    """Construct a legal-shape synthetic observation with unknown unordered draw contents."""
    state = json.loads((ROOT / "tests/fixtures/public/combat.json").read_text(encoding="utf-8"))["state"]
    state["turn"] = 1
    state["combat"]["players"] = [{"is_local": True, "character_id": character}]
    state["combat"]["player"].update(hp=f"{hp}/80", block=0, energy=3, stars=3,
                                     powers=[], orbs=[], pets=[], focus=0)
    state["combat"]["enemies"] = deepcopy(enemies or [enemy()])
    state["combat"]["hand"] = [card(identifier, index, upgraded=upgraded and index % 2 == 0)
                                  for index, identifier in enumerate(cards[:5])]
    state["combat"]["draw"] = [card(identifier, index + 5, upgraded=upgraded and index % 2 == 0)
                                  for index, identifier in enumerate(cards[5:])]
    state["combat"]["discard"] = []
    state["combat"]["exhaust"] = []
    state["run"].update(character_id=character, max_energy=3, ascension=0, relic_ids=[],
                        relic_stacks=[], relics=[], hp=f"{hp}/80", room_type="Monster")
    state["run"]["potions"] = [{"i": index, "potion_id": identifier, "occupied": True,
                                   "usable": True} for index, identifier in enumerate(potions)]
    state["public_history"] = {"schema_version": 1, "history_complete": True,
        "total_card_plays_this_turn": 0, "total_attack_plays_this_turn": 0,
        "total_skill_plays_this_turn": 0, "manual_card_plays_this_turn": 0,
        "non_auto_card_plays_this_turn": 0}
    return state


def corpus():
    """Cover all characters and independently named mechanics without filtering host failures."""
    cases = []
    for character in ("IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER"):
        deck = [f"STRIKE_{character}", f"DEFEND_{character}"] * 5
        for upgraded in (False, True):
            cases.append({"id": character.lower() + ("-upgraded" if upgraded else "-base"),
                "character": character, "mechanisms": ["repeated_draw", "multiple_rounds", "upgrades" if upgraded else "base"],
                "public_state": position(character, deck, upgraded=upgraded)})
    mechanisms = [
        ("ironclad-strength-multienemy", ["INFLAME", "TWIN_STRIKE", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "BASH"] * 2,
         [enemy(hp=36), enemy(hp=40, index=1)], ["FIRE_POTION"]),
        ("ironclad-exhaust-choice", ["FEEL_NO_PAIN", "TRUE_GRIT", "BURNING_PACT", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"] * 2,
         [enemy(hp=80)], ["ENERGY_POTION"]),
        ("ironclad-long-scaling", ["DEMON_FORM", "DEFEND_IRONCLAD", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "SHRUG_IT_OFF"] * 2,
         [enemy(hp=220)], ["BLOCK_POTION"]),
    ]
    for name, deck, enemies, potions in mechanisms:
        cases.append({"id": name, "character": "IRONCLAD", "mechanisms": [name],
                      "public_state": position("IRONCLAD", deck, enemies=enemies, potions=potions)})
    specialty = (
        ("SILENT", "poison-draw-discard", ["NOXIOUS_FUMES", "DEADLY_POISON", "BACKFLIP", "DAGGER_THROW", "DEFEND_SILENT"]),
        ("DEFECT", "lightning-frost-evoke", ["ZAP", "DUALCAST", "BALL_LIGHTNING", "COLD_SNAP", "DEFEND_DEFECT"]),
        ("REGENT", "stars-power", ["GLITTERSTREAM", "FALLING_STAR", "CHILD_OF_THE_STARS", "STRIKE_REGENT", "DEFEND_REGENT"]),
        ("NECROBINDER", "osty-doom", ["BODYGUARD", "UNLEASH", "BLIGHT_STRIKE", "STRIKE_NECROBINDER", "DEFEND_NECROBINDER"]),
    )
    for character, mechanism, deck in specialty:
        cases.append({"id": character.lower() + "-" + mechanism, "character": character,
                      "mechanisms": [mechanism],
                      "public_state": position(character, deck * 2, upgraded=True, potions=["BLOCK_POTION"])})
    return cases


class Host:
    """Bound every JSONL request and preserve process diagnostics on timeout or crash."""

    def __init__(self, executable, timeout):
        """Launch a dedicated offline simulator process with no game connection."""
        self.timeout = timeout
        self.lines = Queue()
        self.errors = []
        command = [str(part) for part in executable] if isinstance(executable, (list, tuple)) else [str(executable)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()

    def _read(self):
        """Queue complete responses and signal process exit explicitly."""
        for line in self.process.stdout:
            if line.startswith("{"):
                self.lines.put(line)
        self.lines.put(None)

    def _errors(self):
        """Drain stderr so a native assertion cannot block behind a full pipe."""
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def request(self, payload):
        """Fail visibly on unfinished work rather than treating it as a loss or match."""
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=self.timeout)
        except Empty as error:
            raise RuntimeError("host_request_timeout") from error
        if line is None:
            raise RuntimeError("host_process_exited:" + "\n".join(self.errors[-20:]))
        return json.loads(line)

    def close(self):
        """Stop only this runner's own child process."""
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)


def summarize(rows):
    """Keep all attempted cells in the denominator and distinguish cutoffs from terminals."""
    summary = {}
    for arm in sorted({row["arm"] for row in rows}):
        selected = [row for row in rows if row["arm"] == arm]
        counts = Counter(row["response"].get("status", "error") for row in selected)
        terminals = [row for row in selected if row["response"].get("status") == "terminal"]
        wins = sum(row["response"].get("final", {}).get("player_won") is True for row in terminals)
        summary[arm] = {"attempted": len(selected), "statuses": dict(counts), "wins": wins,
            "losses": len(terminals) - wins, "win_fraction_all_attempts": wins / len(selected),
            "terminal_fraction": len(terminals) / len(selected),
            "multi_round_cells": sum(row["response"].get("final", {}).get("round", 1) > 1 for row in selected),
            "max_observed_round": max(row["response"].get("final", {}).get("round", 1) for row in selected),
            "simulations_spent": sum(row["response"].get("simulations_spent", 0) for row in selected)}
    return summary


def paired_outcomes(rows):
    """Expose paired discordant outcomes while retaining incomplete pairs as their own category."""
    indexed = {(row["case"], row["seed"], row["arm"]): row["response"] for row in rows}
    result = {}
    worlds = sorted({(row["case"], row["seed"]) for row in rows})
    for arm in sorted({row["arm"] for row in rows if row["arm"].startswith("mcts-")}):
        counts = Counter()
        for case, seed in worlds:
            treatment = indexed.get((case, seed, arm), {})
            control = indexed.get((case, seed, "random"), {})
            if treatment.get("status") != "terminal" or control.get("status") != "terminal":
                counts["incomplete_pair"] += 1
                continue
            outcome = (treatment["final"]["player_won"], control["final"]["player_won"])
            counts[{(True, True): "both_win", (False, False): "both_lose",
                    (True, False): "mcts_only_win", (False, True): "random_only_win"}[outcome]] += 1
        result[arm + "_vs_random"] = {"attempted_pairs": len(worlds), "outcomes": dict(counts)}
    return result


def main():
    """Run fixed worlds under each arm and save every attempt before summary generation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--budgets", type=int, nargs="+", default=[128, 1000])
    parser.add_argument("--max-decisions", type=int, default=200)
    parser.add_argument("--max-rollout-depth", type=int, default=200)
    parser.add_argument("--case", action="append")
    parser.add_argument("--trial-timeout-ms", type=int, default=120000)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # A concurrent developer rebuild must not change the executable between panel cells.
    source_executable = args.exe.resolve()
    frozen_executable = args.output.resolve() / "mentor-simulation.exe"
    shutil.copy2(source_executable, frozen_executable)
    args.exe = frozen_executable
    cases = [case for case in corpus() if not args.case or case["id"] in args.case]
    if not cases:
        parser.error("No requested cases exist")
    manifest = {"schema_version": 1, "scope": "synthetic_continuous_combat_not_native_correctness_or_full_run_winrate",
        "source_executable": str(source_executable), "executable": str(args.exe.resolve()),
        "sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(),
        "seeds": args.seeds, "budgets": args.budgets, "cases": cases,
        "max_decisions": args.max_decisions, "max_rollout_depth": args.max_rollout_depth,
        "trial_timeout_ms": args.trial_timeout_ms,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    metadata_host = Host(args.exe, 10)
    try:
        manifest["metadata"] = metadata_host.request({"op": "metadata"})
    finally:
        metadata_host.close()
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    arms = [("random", None), ("one_step_hce", None)] + [("mcts", budget) for budget in args.budgets]
    for case in cases:
        for seed in args.seeds:
            for policy, budget in arms:
                request = {"op": "combat_trial", "public_state": case["public_state"], "policy": policy,
                    "seed": seed, "read_access": "public", "simulations": budget or 1,
                    "max_rollout_depth": args.max_rollout_depth, "max_decisions": args.max_decisions,
                    "trial_timeout_ms": args.trial_timeout_ms}
                host = Host(args.exe, args.trial_timeout_ms / 1000 + 30)
                started = time.perf_counter()
                try:
                    response = host.request(request)
                except Exception as error:
                    response = {"ok": False, "status": "error", "error": str(error), "stderr": host.errors[-20:]}
                finally:
                    host.close()
                row = {"case": case["id"], "character": case["character"], "seed": seed,
                    "arm": policy + (f"-{budget}" if budget else ""), "response": response,
                    "roundtrip_ms": (time.perf_counter() - started) * 1000}
                rows.append(row)
                with (args.output / "attempts.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({key: row[key] for key in ("case", "seed", "arm")} | {
                    "status": response.get("status", "error"), "reason": response.get("stop_reason", response.get("errors"))}), flush=True)
    result = {"scope": manifest["scope"], "attempted_cells": len(rows), "arms": summarize(rows),
              "paired": paired_outcomes(rows)}
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

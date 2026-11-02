"""Validate the production public-state Mentor protocol against isolated native outcomes."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from native_parity import JsonlProcess, case_matrix
from parity_contract import CONTRACT_VERSION, compare_contract, coverage_summary, state_contract
from replay import extract_cases

# The stable deployment is independent of task-specific source and build directories.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE = ROOT / "local/simulation/mentor/mentor-simulation.exe"
# Use an independent hypothetical seed; no game seed or RNG enters the tested backend.
SCENARIO_SEED = 719
# The two accepted public corpora remain read-only historical evidence.
REPLAY_RUNS = ("d2b348c3-e645-44fc-873f-a61a8a1ab678", "8fafbaa5-dbc8-4ea7-9740-f430db6a5123")


def file_hash(path):
    """Bind a test result to actual local code or binary contents."""
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def pile_count(groups):
    """Read visible grouped multiplicities without treating group order as draw knowledge."""
    total = 0
    for group in groups:
        if type(group.get("card_count")) is int:
            total += group["card_count"]
            continue
        match = re.match(r"^[^\[]*?\*(\d+)(?=\s|\[|$)", group.get("line", ""))
        total += int(match[1]) if match else 1
    return total


def public_endpoint(state):
    """Project only source quantities that the production importer promises to retain."""
    combat = state["combat"]
    player = deepcopy(combat["player"])
    history = state.get("public_history", {})
    complete = history.get("history_complete") is True
    if complete:
        for target, source in (("cards_played_this_turn", "total_card_plays_this_turn"),
                               ("attacks_played_this_turn", "total_attack_plays_this_turn"),
                               ("skills_played_this_turn", "total_skill_plays_this_turn"),
                               ("manual_card_plays_this_turn", "manual_card_plays_this_turn")):
            if source in history:
                player[target] = history[source]
    return {"player": player, "enemies": combat["enemies"],
            "hand": combat["hand"], "piles": {name: pile_count(combat[name])
            for name in ("draw", "discard", "exhaust")},
            "pile_cards": {name: combat[name] for name in ("draw", "discard", "exhaust")},
            "play_count_scope": "completed_native_plays" if complete else "manual_dispatch_only",
            "pending_choice": None, "potions": state["run"].get("potions", [])}


def public_with_history(native, history):
    """Attach only history established by the fresh fixture and observed public card plays."""
    source = deepcopy(native["public_state"])
    if history is not None:
        # Complete native completed-play evidence supersedes legacy fixture-only hints.
        source["public_history"] = {**deepcopy(history), **source.get("public_history", {})}
    return source


def import_comparison(source, imported):
    """Use the same expanded public contract for roots and successors, exposing missing fields."""
    expected = public_endpoint(source)
    return compare_contract(state_contract(expected, public_state=source), state_contract(imported))


def converted_action(action, state):
    """Resolve a script's current hand row to its existing branch-local stable handle."""
    result = deepcopy(action)
    if result["type"] == "play_card":
        row = result.pop("card_index")
        result["card_handle"] = state["hand"][row]["handle"]
    return result


def evaluate(client, source, actions, identity):
    """Send a fixed plan through the same production operation used by the controller."""
    result = client.request({"op": "evaluate", "public_state": source,
        "candidate_plans": [{"id": identity, "actions": deepcopy(actions)}],
        "samples": 1, "max_actions": 8, "max_events": 512,
        "timeout_ms": 5000, "scenario_seed": SCENARIO_SEED})
    if not result.get("ok"):
        return {"status": "unsupported", "stop_reason": result.get("errors", result.get("reason")),
                "response": result}
    return result["candidates"][0]["samples"][0]


def sample_comparison(sample, native):
    """Keep unfinished or unsupported resolution distinct from verified pending choices."""
    status = sample["status"]
    if status not in ("completed", "needs_decision") or not isinstance(sample.get("state"), dict):
        return {"status": status, "reason": sample.get("stop_reason")}
    expected = state_contract(native["endpoint"], public_state=native["public_state"], pending=native["pending_choice"])
    result = compare_contract(expected, state_contract(sample["state"]))
    expected_pending = native["pending_choice"] is not None
    if (status == "needs_decision") != expected_pending:
        result["differences"].append({"field": "resolution_status", "expected":
            "needs_decision" if expected_pending else "completed", "actual": status})
        result["status"] = "mismatch"
    # Preserve native death/victory cleanup differences instead of erasing bodies or resource data.
    boundary = cleanup_boundary(result["differences"], native["endpoint"], sample["state"])
    if boundary:
        result["classification"] = boundary
    result["resolution_status"] = status
    return result


def cleanup_boundary(differences, native, simulated):
    """Label only observed corpse/terminal cleanup fields while retaining every actual difference."""
    if not differences:
        return None
    native_enemies = native.get("enemies", [])
    simulated_enemies = simulated.get("enemies", [])
    terminal = bool(native_enemies) and all(enemy.get("alive") is False for enemy in native_enemies)
    terminal = terminal and simulated.get("terminal") is True
    cleanup_resources = {"player.block", "player.powers", "player.power_details", "player.cards_played_this_turn",
                         "player.attacks_played_this_turn", "player.skills_played_this_turn", "player.manual_card_plays_this_turn",
                         "player.non_auto_card_plays_this_turn"}
    for difference in differences:
        field = difference["field"]
        if terminal and difference["expected"] in ([], 0) and (field.startswith(("hand.", "hand[", "piles.", "player.power_details.")) or field in cleanup_resources):
            continue
        reward_potion = re.match(r"potions\[(\d+)\]\.(id|occupied|usable)$", field)
        if terminal and reward_potion:
            index = int(reward_potion[1])
            native_potions, simulated_potions = native.get("potions", []), simulated.get("potions", [])
            # RunSimulator.DetectPostCombatState auto-collects potion rewards before the oracle snapshot.
            if (index < len(native_potions) and index < len(simulated_potions)
                    and native_potions[index].get("occupied") is True
                    and simulated_potions[index].get("occupied") is False):
                continue
        corpse = re.match(r"enemies\[(\d+)\]\.(powers|power_details(?:\..*)?)$", field)
        if corpse:
            index = int(corpse[1])
            if (index < len(native_enemies) and index < len(simulated_enemies)
                    and native_enemies[index].get("alive") is False
                    and simulated_enemies[index].get("alive") is False
                    and difference["expected"] in ([], 0)):
                continue
        return None
    return "native_victory_cleanup_boundary" if terminal else "native_corpse_cleanup_boundary"


def extended_matrix():
    """Exercise current powers, multi-enemy targeting and relic roots without support filtering."""
    cases = case_matrix()
    sequences = [
        ("CURRENT:STACKED_STRENGTH", ["INFLAME", "INFLAME", "TWIN_STRIKE"], "NIBBITS_WEAK"),
        ("CURRENT:RAGE_AND_BLOCK", ["RAGE", "TWIN_STRIKE", "DEFEND_IRONCLAD"], "NIBBITS_WEAK"),
        ("CURRENT:VULNERABLE_TWIN", ["BASH", "TWIN_STRIKE"], "NIBBITS_WEAK"),
        ("CURRENT:BLOCK_BODY_SLAM", ["DEFEND_IRONCLAD", "BODY_SLAM"], "NIBBITS_WEAK"),
        ("MULTI:THUNDERCLAP_WHIRLWIND", ["THUNDERCLAP", "WHIRLWIND"], "NIBBITS_NORMAL"),
        ("MULTI:EXTERMINATE", ["EXTERMINATE"], "EXOSKELETONS_WEAK"),
    ]
    for identity, cards, encounter in sequences:
        # Public targeted actions must not attach enemy slots to self or all-enemy cards.
        actions = []
        for card in cards:
            action = {"type": "play_card", "card_index": 0}
            if card in ("TWIN_STRIKE", "BASH", "BODY_SLAM"):
                action["target_index"] = 0
            actions.append(action)
        cases.append({"id": identity, "setup": {"hand": cards, "draw": [], "discard": [],
            "exhaust": [], "energy": 10, "encounter": encounter},
            "actions": actions})
    cases.append({"id": "MULTI:SECOND_TARGET_DEBUFF", "setup": {
        "hand": ["UPPERCUT", "STRIKE_IRONCLAD"], "draw": [], "discard": [], "exhaust": [],
        "energy": 10, "encounter": "NIBBITS_NORMAL"},
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 1}] * 2})
    cases.append({"id": "MULTI:THUNDERCLAP_WHIRLWIND_NONTERMINAL", "setup": {
        "hand": ["THUNDERCLAP", "WHIRLWIND"], "draw": [], "discard": [], "exhaust": [],
        "energy": 3, "encounter": "NIBBITS_NORMAL"},
        "actions": [{"type": "play_card", "card_index": 0}] * 2})
    for relic in ("VAJRA", "ODDLY_SMOOTH_STONE", "AKABEKO", "VENERABLE_TEA_SET",
                  "MERCURY_HOURGLASS", "GORGET", "PERMAFROST"):
        cases.append({"id": "IMPORT:" + relic, "setup": {"hand": ["STRIKE_IRONCLAD"],
            "draw": [], "discard": [], "exhaust": [], "relics": [relic], "encounter": "NIBBITS_WEAK"},
            "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    cases.append({"id": "IMPORT:BRILLIANT_SCARF:fifth_card", "setup": {
        "hand": ["ANGER"] * 4 + ["BLUDGEON"], "draw": [], "discard": [], "exhaust": [],
        "relics": ["BRILLIANT_SCARF"], "encounter": "NIBBITS_NORMAL"},
        "actions": [{"type": "play_card", "card_index": 0, "target_index": target}
                    for target in (0, 1, 0, 1, 0)]})
    return cases


def run_native(client, cases, checkout=None, runtime_dir=None):
    """Compare retained simulated continuations and separately reimport actual stable roots."""
    sys.path.insert(0, str(ROOT / "experiments/native_oracle"))
    from native_oracle import DEFAULT_CHECKOUT, NativeOracle

    checkout = Path(checkout or DEFAULT_CHECKOUT).resolve()
    native_paths = (checkout / "lib/sts2.dll",
        checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll",
        checkout / "src/Sts2Headless/RunSimulator.cs",
        checkout / "src/Sts2Headless/OracleControl.cs",
        ROOT / "experiments/native_oracle/native_oracle.py")
    native_before = {str(path): file_hash(path) for path in native_paths}
    rows = []
    for case in cases:
        row = {"id": case["id"], "fixture": deepcopy(case), "steps": []}
        # This opt-in fixture history never changes native snapshots or expected resource values.
        power_ids = case.get("complete_history_power_card_ids")
        history = ({"schema_version": 1, "history_complete": True,
                    "permafrost_first_power_used": False} if power_ids is not None else None)
        if history is not None:
            row["history_projection"] = {"source": "fresh_native_combat_and_all_observed_public_actions",
                "reviewed_power_card_ids": power_ids, "initial": deepcopy(history)}
        phase = "native_init"
        try:
            with NativeOracle(checkout, runtime_dir=runtime_dir) as oracle:
                initial = oracle.init(case["setup"])
                row["native_initial"] = initial
                root_public = public_with_history(initial, history)
                phase = "production_import"
                imported = client.request({"op": "inspect", "public_state": root_public})
                row["import_response"] = imported
                if isinstance(imported.get("state"), dict):
                    row["import_comparison"] = import_comparison(initial["public_state"], imported["state"])
                else:
                    row["import_comparison"] = {"status": "unsupported", "reason": imported.get("errors")}
                state = imported.get("state")
                planned = []
                stopped = None if isinstance(state, dict) else "initial_import_unsupported"
                before = initial
                for index, action in enumerate(case["actions"]):
                    fresh_public = public_with_history(before, history)
                    played_id = (before["public_state"]["combat"]["hand"][action["card_index"]]["card_id"]
                        if history is not None and action["type"] == "play_card" else None)
                    phase = "native_action"
                    after = oracle.step(action)
                    step = {"action_index": index, "action": action, "native": after}
                    if history is not None:
                        step["public_history_before"] = deepcopy(history)
                        if played_id in power_ids:
                            history["permafrost_first_power_used"] = True
                    if stopped:
                        step["plan"] = {"status": "unsupported", "reason": stopped}
                    else:
                        try:
                            planned.append(converted_action(action, state))
                            sample = evaluate(client, root_public, planned, case["id"])
                            step["sample"] = sample
                            step["plan"] = sample_comparison(sample, after)
                            state = sample.get("state", state)
                            if sample["status"] not in ("completed", "needs_decision"):
                                stopped = sample.get("stop_reason", "prior_step_unsupported")
                        except Exception as error:
                            stopped = str(error)
                            step["plan"] = {"status": "failed", "reason": str(error)}
                    if before["pending_choice"] is None:
                        try:
                            fresh = evaluate(client, fresh_public, [action], case["id"] + ":fresh")
                            step["stable_reimport"] = sample_comparison(fresh, after)
                            step["stable_reimport_sample"] = fresh
                        except Exception as error:
                            step["stable_reimport"] = {"status": "failed", "reason": str(error)}
                    else:
                        step["stable_reimport"] = {"status": "incomparable", "reason": "mid_choice_not_an_import_root"}
                    row["steps"].append(step)
                    before = after
            row["native_status"] = "completed"
        except Exception as error:
            row["native_status"] = "failed" if phase.startswith("native") else "incomplete"
            row["failure_phase"] = phase
            row["error"] = str(error)
        rows.append(row)
        print(json.dumps({"id": row["id"], "native": row["native_status"],
                          "plan": [step["plan"]["status"] for step in row["steps"]]}, ensure_ascii=False), flush=True)
    native_after = {str(path): file_hash(path) for path in native_paths}
    return {"cases": rows, "native_artifacts_before": native_before,
        "native_artifacts_after": native_after, "native_artifacts_unchanged": native_before == native_after,
        "field_coverage": coverage_summary([row.get("import_comparison", {}) for row in rows] +
            [step[name] for row in rows for step in row["steps"] for name in ("plan", "stable_reimport")]),
        "summary": {"cases": len(rows),
        "native": dict(Counter(row["native_status"] for row in rows)),
        "import": dict(Counter(row.get("import_comparison", {}).get("status", "unavailable") for row in rows)),
        **{name: dict(Counter(step[name]["status"] for row in rows for step in row["steps"]))
           for name in ("plan", "stable_reimport")}}}


def run_replay(client, runs):
    """Audit importer fidelity and deterministic predictions on accepted public historical frames."""
    rows = []
    for run_id in runs:
        for case in extract_cases(ROOT / "runs" / run_id)["cases"]:
            row = {"id": case["id"], "run": run_id, "metadata": case["metadata"]}
            try:
                result = client.request({"op": "inspect", "public_state": case["before"]})
                row["import_response"] = result
                row["import_comparison"] = (import_comparison(case["before"], result["state"])
                    if isinstance(result.get("state"), dict) else {"status": "unsupported", "reason": result.get("errors")})
                prediction = client.request({"op": "simulate", "public_state": case["before"],
                                             "actions": [case["action"]]})
                row["prediction"] = prediction
                events = prediction.get("events", [])
                if prediction.get("ok") is True and len(events) == 1 and events[0].get("applied") is True:
                    row["prediction_comparison"] = compare_contract(
                        state_contract(public_endpoint(case["after"]), public_state=case["after"]),
                        state_contract(prediction["state"]))
                else:
                    row["prediction_comparison"] = {"status": "unsupported", "reason": prediction.get("errors")}
            except Exception as error:
                row["error"] = str(error)
                row.setdefault("import_comparison", {"status": "failed", "reason": str(error)})
                row.setdefault("prediction_comparison", {"status": "failed", "reason": str(error)})
            rows.append(row)
    unsupported_reasons = Counter()
    for row in rows:
        comparison = row["prediction_comparison"]
        if comparison["status"] == "unsupported":
            reasons = comparison.get("reason")
            for reason in reasons if isinstance(reasons, list) else [reasons]:
                unsupported_reasons[str(reason.get("error") if isinstance(reason, dict) else reason)] += 1
    return {"cases": rows, "summary": {"cases": len(rows),
        "import": dict(Counter(row["import_comparison"]["status"] for row in rows)),
        "prediction": dict(Counter(row["prediction_comparison"]["status"] for row in rows)),
        "unsupported_reasons": dict(unsupported_reasons)}}


def main():
    """Run native, public-replay or both evidence sets through the deployed production service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("native", "replay", "all"))
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--oracle-checkout", type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    artifacts = {str(path): file_hash(path) for path in (args.exe, Path(__file__))}
    report = {"scope": "production_public_state_native_and_public_history_validation",
              "contract_version": CONTRACT_VERSION, "artifacts_before": artifacts,
              "coverage_policy": "Missing fields are explicit; partial_match is not complete validation."}
    client = JsonlProcess([str(args.exe)])
    try:
        if args.mode in ("native", "all"):
            cases = json.loads(args.input.read_text(encoding="utf-8")) if args.input else extended_matrix()
            report["native"] = run_native(client, cases[:args.limit] if args.limit else cases,
                args.oracle_checkout, args.output.parent / "native-runtime")
        if args.mode in ("replay", "all"):
            report["replay"] = run_replay(client, REPLAY_RUNS)
    finally:
        client.close()
    report["artifacts_after"] = {path: file_hash(path) for path in artifacts}
    report["artifacts_unchanged"] = report["artifacts_before"] == report["artifacts_after"]
    report["elapsed_seconds"] = time.perf_counter() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({name: report[name]["summary"] for name in ("native", "replay") if name in report}, ensure_ascii=False))


if __name__ == "__main__":
    main()

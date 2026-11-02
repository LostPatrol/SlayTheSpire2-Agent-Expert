"""Generate reproducible native-reachable combat trajectories and audit production successors."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time

from mentor_production_parity import (DEFAULT_EXE, ROOT, converted_action, evaluate, file_hash,
                                      import_comparison, sample_comparison)
from native_parity import JsonlProcess, hp_value
from parity_contract import CONTRACT_VERSION

# This matrix is independent of Mentor's support list and uses native registry identities.
FAMILIES = (
    ("attack_block", ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "IRON_WAVE", "BODY_SLAM"], []),
    ("strength", ["INFLAME", "TWIN_STRIKE", "STRIKE_IRONCLAD", "ANGER"], ["VAJRA"]),
    ("vulnerable", ["BASH", "UPPERCUT", "THUNDERCLAP", "STRIKE_IRONCLAD"], []),
    ("rage", ["RAGE", "TWIN_STRIKE", "IRON_WAVE", "BODY_SLAM"], []),
    ("exhaust_choice", ["FEEL_NO_PAIN", "TRUE_GRIT", "BURNING_PACT", "DEFEND_IRONCLAD"], []),
    ("draw", ["SHRUG_IT_OFF", "POMMEL_STRIKE", "OFFERING", "BATTLE_TRANCE"], []),
    ("corruption", ["CORRUPTION", "DARK_EMBRACE", "BURNING_PACT", "DEFEND_IRONCLAD"], []),
    ("x_cost", ["BLOODLETTING", "DEFEND_IRONCLAD", "BODY_SLAM", "WHIRLWIND"], []),
    ("attack_threshold", ["ANGER", "ANGER", "STRIKE_IRONCLAD", "TWIN_STRIKE"], ["ORNAMENTAL_FAN"]),
    ("skill_threshold", ["DEFEND_IRONCLAD", "DEFEND_IRONCLAD", "RAGE", "SHRUG_IT_OFF"], ["LETTER_OPENER"]),
    ("manual_replay", ["STRIKE_IRONCLAD", "BURNING_PACT", "DEFEND_IRONCLAD", "ANGER"], ["KUNAI"]),
    ("cost_threshold", ["ANGER", "ANGER", "ANGER", "ANGER", "BLUDGEON"], ["BRILLIANT_SCARF"]),
)
# Extra cards create meaningful resource and target interactions without arbitrary state injection.
FILLER_CARDS = ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "ANGER", "IRON_WAVE", "TWIN_STRIKE", "BODY_SLAM")
# These known native potion identities vary both card effects and carried resources.
POTIONS = ("BLOCK_POTION", "ENERGY_POTION", "STRENGTH_POTION", "DEXTERITY_POTION",
           "VULNERABLE_POTION", "WEAK_POTION", "FIRE_POTION", "BLOOD_POTION", "DUPLICATOR")
# Keep the native process fresh for every setup and each worker's runtime files separate.
DEFAULT_WORKERS = 3
# Published evaluator contract bounds a continued candidate to eight explicit actions.
MAX_STEPS = 8
# Strict errors in these domains forbid comparing independently sampled paths as exact successors.
RANDOM_BOUNDARIES = ("unknown_draw_boundary", "random_outcome_boundary", "hand_generation_boundary")


def fixture(index, seed):
    """Create one diverse legal-intent setup, without consulting emulator coverage or outcomes."""
    rng = random.Random((seed << 32) + index)
    family, base, relics = FAMILIES[index % len(FAMILIES)]
    hand = list(base) + [rng.choice(FILLER_CARDS) for _ in range(9 - len(base))]
    upgraded = [{"card_id": name, "upgraded": bool(rng.getrandbits(1))} for name in hand]
    if family == "attack_threshold":
        relics = [["ORNAMENTAL_FAN"], ["KUNAI"], ["SHURIKEN"]][(index // len(FAMILIES)) % 3]
    relics = list(relics)
    if index % 5 == 0:
        relics.append("ODDLY_SMOOTH_STONE")
    potions = ["DUPLICATOR"] if family == "manual_replay" else ([rng.choice(POTIONS)] if index % 3 else [])
    # Homogeneous piles make the first draws comparable without sharing hidden RNG.
    draw = [rng.choice(("ANGER", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"))] * rng.choice((0, 3, 6))
    discard = ["ANGER"] * rng.choice((0, 2, 4)) if index % 4 == 0 else []
    return {"id": f"generated-{seed}-{index:05d}", "family": family, "generation_seed": seed,
            "index": index, "walk_seed": seed * 1_000_003 + index,
            "setup": {"hand": upgraded, "draw": draw, "discard": discard, "exhaust": [],
                "hp": rng.choice((24, 37, 60, 79)), "max_hp": 80,
                "energy": rng.choice((3, 6, 10, 15)), "relics": relics, "potions": potions,
                "encounter": "NIBBITS_WEAK" if index % 13 == 0 else "NIBBITS_NORMAL",
                "seed": f"offline-batch-{seed}-{index}"},
            "preferred_cards": list(base)}


def next_action(snapshot, rng, preferred, step):
    """Choose only currently native-legal visible actions, including explicit pending choices."""
    pending = snapshot.get("pending_choice")
    if pending is not None:
        candidates = pending.get("candidates", pending.get("options", []))
        if pending.get("min_select", 1) > 1 or not candidates:
            return None
        choice = rng.choice(candidates)
        return {"type": "select_card", "choice_index": choice.get("choice_index", choice.get("i", 0))}
    source = snapshot["public_state"]
    if not any(enemy.get("alive") for enemy in source["combat"]["enemies"]):
        return None
    cards = [row for row in source["combat"]["hand"] if row.get("playable") is True]
    potions = [row for row in source["run"]["potions"] if row.get("occupied") and row.get("usable")]
    if potions and (step == 0 or not cards):
        chosen = rng.choice(potions)
        action = {"type": "use_potion", "option_index": chosen["i"]}
    elif cards:
        rank = {name: index for index, name in enumerate(preferred)}
        chosen = min(cards, key=lambda row: rank.get(row["card_id"], len(rank))) if step < 3 else rng.choice(cards)
        action = {"type": "play_card", "card_index": chosen["i"]}
    else:
        return None
    if chosen.get("targets"):
        action["target_index"] = rng.choice(chosen["targets"])
    return action


def error_text(result):
    """Flatten only diagnostic data when classifying unsupported simulator boundaries."""
    return json.dumps(result.get("errors", result.get("reason", result.get("stop_reason", ""))), ensure_ascii=False)


def pending_draw_boundary(before, after):
    """Detect visible mixed-pile consumption during a choice, where fresh strict import is unavailable."""
    if before.get("pending_choice") is None:
        return False
    old, new = before["endpoint"]["piles"], after["endpoint"]["piles"]
    if not (new["draw"] < old["draw"] or new["discard"] < old["discard"]):
        return False
    combat = before["public_state"]["combat"]
    cards = combat.get("draw", [])
    # A surviving draw pile with unchanged discard cannot have consumed that discard's hidden order.
    if not (0 < new["draw"] < old["draw"] and new["discard"] >= old["discard"]):
        cards = cards + combat.get("discard", [])
    signatures = {json.dumps({key: card[key] for key in ("card_id", "card_ids", "upgraded", "affliction",
        "enchantment", "energy_cost", "star_cost", "dynamic_values", "effective_keywords") if key in card}, sort_keys=True)
        for card in cards}
    return len(signatures) > 1


def classified(comparison, strict=None):
    """Distinguish sampled ambiguity from numeric mismatches and partial comparison coverage."""
    result = deepcopy(comparison)
    text = error_text(strict or {})
    random_boundary = any(reason in text for reason in RANDOM_BOUNDARIES)
    if random_boundary:
        result["classification"] = "stochastic_boundary_incomparable"
        result["raw_comparison_status"] = result["status"]
        result["status"] = "incomparable"
        result["reason"] = text
    elif result.get("classification") in {"reward_cleanup_boundary", "native_victory_cleanup_boundary", "native_corpse_cleanup_boundary"}:
        result["boundary_kind"] = result["classification"]
        result["raw_comparison_status"] = result["status"]
        result["status"] = "incomparable"
        result["classification"] = "boundary_incomparable"
    elif result["status"] == "mismatch":
        result["classification"] = "supported_but_wrong"
    elif result["status"] in {"matched", "partial_match"}:
        result["classification"] = "matched_complete" if result["status"] == "matched" else "matched_available_fields"
    elif result["status"] in {"unsupported", "needs_decision"}:
        result["classification"] = "unsupported_or_unobserved_dependency"
    else:
        result["classification"] = "backend_error_or_budget"
    return result


def run_case(client, oracle, case, *, prescribed=None):
    """Audit one native-reachable path using retained continuations and stable reimports."""
    started = time.perf_counter()
    row = {"id": case["id"], "fixture": deepcopy(case), "steps": [], "actions": []}
    phase = "initialization"
    try:
        before = oracle.init(case["setup"])
        row["native_initial"] = before
        root = before["public_state"]
        imported = client.request({"op": "inspect", "public_state": root})
        row["import_response"] = imported
        row["import_comparison"] = (import_comparison(root, imported["state"])
            if isinstance(imported.get("state"), dict) else {"status": "unsupported", "reason": imported.get("errors")})
        simulated, planned = imported.get("state"), []
        continuation_boundary = None
        rng = random.Random(case["walk_seed"])
        for index in range(len(prescribed) if prescribed is not None else MAX_STEPS):
            action = prescribed[index] if prescribed is not None else next_action(before, rng, case["preferred_cards"], index)
            if action is None:
                row["stop_reason"] = "no_native_player_action_or_supported_choice"
                break
            phase = "native_action"
            after = oracle.step(action)
            row["actions"].append(action)
            step = {"action_index": index, "action": action, "native": after}
            phase = "comparison"
            strict = None
            if pending_draw_boundary(before, after):
                continuation_boundary = "pending_choice_consumed_mixed_public_piles_unknown_draw_order"
                step["comparison_boundary"] = {"reason": continuation_boundary,
                    "source": "visible_native_pile_changes_not_a_host_determinism_claim"}
            if before.get("pending_choice") is None:
                strict = client.request({"op": "simulate", "public_state": before["public_state"], "actions": [action]})
                step["strict_response"] = strict
                fresh = evaluate(client, before["public_state"], [action], case["id"] + ":fresh")
                step["stable_reimport"] = classified(sample_comparison(fresh, after), strict)
                step["stable_reimport_sample"] = fresh
            else:
                step["stable_reimport"] = {"status": "incomparable", "classification": "boundary_incomparable",
                    "reason": "pending_choice_is_not_a_stable_import_root"}
            if any(reason in error_text(strict or {}) for reason in RANDOM_BOUNDARIES):
                continuation_boundary = "earlier_random_boundary_requires_actual_new_observation"
            if continuation_boundary or not isinstance(simulated, dict):
                step["plan"] = {"status": "incomparable" if continuation_boundary else "unsupported",
                                "classification": "boundary_incomparable" if continuation_boundary else "unsupported_import",
                                "reason": continuation_boundary or "initial_import_unsupported"}
            else:
                try:
                    planned.append(converted_action(action, simulated))
                    sample = evaluate(client, root, planned, case["id"] + ":continuation")
                    step["sample"] = sample
                    step["plan"] = classified(sample_comparison(sample, after))
                    if sample["status"] not in {"completed", "needs_decision"}:
                        continuation_boundary = "earlier_unsupported_continuation"
                    simulated = sample.get("state", simulated)
                except (KeyError, IndexError, ValueError) as error:
                    continuation_boundary = str(error)
                    step["plan"] = {"status": "failed", "classification": "continuation_mapping_error", "reason": str(error)}
            row["steps"].append(step)
            before = after
            failures = [step[name] for name in ("plan", "stable_reimport")
                        if step[name].get("classification") == "supported_but_wrong"]
            if failures:
                row["first_failure"] = {"step": index, "differences": failures[0].get("differences", []),
                    "actions": deepcopy(row["actions"]), "minimization": "first_divergent_prefix"}
                # The first difference is evidence; later cascades should not inflate bug counts.
                break
        row["native_status"] = "completed"
    except Exception as error:
        row["native_status"] = "failed"
        row["error"] = str(error)
        row["failure_phase"] = phase
        row["classification"] = ("invalid_fixture" if phase == "initialization" and "timeout" not in str(error)
                                 else "oracle_timeout" if "native_oracle_timeout" in str(error)
                                 else "oracle_error" if phase == "native_action" else "backend_error")
    row["elapsed_seconds"] = time.perf_counter() - started
    return row


def write_case(path, row):
    """Atomically retain compressed complete native evidence for resume and failure reduction."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
        json.dump(row, stream, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def read_case(path):
    """Read one complete compressed case without modifying the original evidence."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def brief(row):
    """Summarize a case's real denominators without suppressing partial or unsupported outcomes."""
    available, unavailable = Counter(), Counter()
    comparisons = [row.get("import_comparison", {})] + [step[name] for step in row["steps"]
        for name in ("plan", "stable_reimport")]
    for comparison in comparisons:
        available.update(re.sub(r"\[\d+\]", "[*]", field) for field in comparison.get("compared_fields", []))
        unavailable.update(re.sub(r"\[\d+\]", "[*]", item["field"]) for item in comparison.get("unavailable_fields", []))
    return {"id": row["id"], "family": row["fixture"]["family"], "native": row["native_status"],
            "steps": len(row["steps"]), "elapsed_seconds": row["elapsed_seconds"],
            "classification": row.get("classification"),
            "import": row.get("import_comparison", {}).get("status", "unavailable"),
            **{name: dict(Counter(step[name]["status"] for step in row["steps"]))
               for name in ("plan", "stable_reimport")},
            "comparison_categories": dict(Counter(comparison.get("classification", "root_import") for comparison in comparisons)),
            "field_coverage": {"available": dict(available), "unavailable": dict(unavailable)},
            "first_failure": row.get("first_failure")}


def shard(cases, exe, checkout, output, resume):
    """Run one worker with a persistent pure host and fresh native process per setup."""
    sys.path.insert(0, str(ROOT / "experiments/native_oracle"))
    from native_oracle import NativeOracle
    directory = Path(output)
    client = JsonlProcess([str(exe)])
    records = []
    progress = directory / f"progress-{os.getpid()}.jsonl"
    try:
        with NativeOracle(checkout, runtime_dir=directory / "runtime" / f"worker-{os.getpid()}") as oracle:
            for case in cases:
                path = directory / "cases" / (case["id"] + ".json.gz")
                reused = False
                if resume and path.exists():
                    try:
                        row = read_case(path)
                        reused = True
                    except (OSError, ValueError, EOFError):
                        row = run_case(client, oracle, case)
                        write_case(path, row)
                else:
                    row = run_case(client, oracle, case)
                    write_case(path, row)
                summary = brief(row)
                summary["reused"] = reused
                records.append(summary)
                with progress.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(summary, ensure_ascii=False) + "\n")
                if not reused and row.get("classification") == "backend_error":
                    # A timed-out JSONL response must not become the next case's response.
                    client.close()
                    client = JsonlProcess([str(exe)])
    finally:
        client.close()
    return records


def manifest(exe, checkout, seed, count, workers):
    """Freeze actual binaries, test contract, generator, and requested workload before execution."""
    paths = [Path(exe), Path(__file__), Path(__file__).with_name("parity_contract.py"),
             Path(__file__).with_name("native_parity.py"),
             Path(__file__).with_name("mentor_production_parity.py"),
             ROOT / "experiments/native_oracle/native_oracle.py",
             Path(checkout) / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll",
             Path(checkout) / "lib/sts2.dll"]
    return {"contract_version": CONTRACT_VERSION, "generation_seed": seed, "requested_cases": count,
            "workers": workers, "max_steps": MAX_STEPS, "families": [row[0] for row in FAMILIES],
            "artifacts": {str(path.resolve()): file_hash(path) for path in paths},
            "scope": "synthetic_native_player_turn_trajectories_no_live_game_or_hidden_policy_state"}


def aggregate(records):
    """Keep total attempts, native-valid cases, successors, and comparison coverage separate."""
    result = {"attempted_setups": len(records), "native_completed_setups": sum(row["native"] == "completed" for row in records),
              "reused_setups": sum(bool(row.get("reused")) for row in records),
              "native_case_seconds_sum": sum(row["elapsed_seconds"] for row in records),
              "native_successors": sum(row["steps"] for row in records),
              "native": dict(Counter(row["native"] for row in records)),
              "failure_categories": dict(Counter(row["classification"] for row in records if row["classification"])),
              "families": dict(Counter(row["family"] for row in records)),
              "import": dict(Counter(row["import"] for row in records)),
              "cases_with_first_mismatch": sum(bool(row["first_failure"]) for row in records)}
    for channel in ("plan", "stable_reimport"):
        counts = Counter()
        for row in records:
            counts.update(row[channel])
        result[channel] = dict(counts)
        result[channel + "_comparable_successors"] = sum(counts[key] for key in ("matched", "partial_match", "mismatch"))
        result[channel + "_fully_compared_matches"] = counts["matched"]
    result["root_supported_setups"] = sum(result["import"].get(key, 0) for key in ("matched", "partial_match", "mismatch"))
    result["root_support_rate_of_attempted"] = result["root_supported_setups"] / len(records) if records else None
    result["partial_coverage_explanation"] = {
        "card_dynamic_values": "Host does not expose native dynamic preview values; null stays unknown.",
        "power_details": "Unreviewed display counters and debuff metadata stay unknown, even when amount matches.",
        "pending_choice": "Native selector exposes generic kind; candidate instance attributes may be unavailable.",
        "manual_count_while_pending": "An unfinished play has no completed manual count in the host snapshot.",
        "policy": "partial_match means all available fields agree, never complete state equivalence."}
    result["comparison_categories"] = dict(sum((Counter(row["comparison_categories"]) for row in records), Counter()))
    result["field_coverage"] = {name: dict(sum((Counter(row["field_coverage"][name]) for row in records), Counter()))
                                for name in ("available", "unavailable")}
    result["field_coverage_scope"] = "field occurrences; list indices are grouped as [*], unknowns stay separate"
    return result


def minimize_case(case_path, exe, checkout, output, attempts):
    """Delete action chunks only when a fresh native replay preserves the same first mismatch."""
    sys.path.insert(0, str(ROOT / "experiments/native_oracle"))
    from native_oracle import NativeOracle
    original = read_case(case_path)
    failure = original.get("first_failure")
    if not failure:
        raise ValueError("Only an observed numeric mismatch can be minimized")
    field = failure["differences"][0]["field"]
    actions = deepcopy(failure["actions"])
    client = JsonlProcess([str(exe)])
    evidence, used = [], 0
    artifacts = {str(Path(path).resolve()): file_hash(path) for path in (exe, Path(__file__),
        Path(checkout) / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll")}
    try:
        with NativeOracle(checkout, runtime_dir=Path(output) / "minimize-runtime") as oracle:
            baseline = run_case(client, oracle, original["fixture"], prescribed=actions)
            if not any(item["field"] == field for item in baseline.get("first_failure", {}).get("differences", [])):
                raise ValueError("Original numeric difference no longer reproduces with the selected artifacts")
            width = max(1, len(actions) // 2)
            while width and used < attempts:
                changed = False
                for offset in range(0, len(actions), width):
                    candidate = actions[:offset] + actions[offset + width:]
                    if not candidate or used >= attempts:
                        continue
                    used += 1
                    row = run_case(client, oracle, original["fixture"], prescribed=candidate)
                    repeated = row.get("first_failure", {}).get("differences", [])
                    keep = any(item["field"] == field for item in repeated)
                    evidence.append({"actions": candidate, "retained": keep, "native_status": row["native_status"],
                                     "failure": row.get("first_failure"), "error": row.get("error")})
                    if keep:
                        actions = candidate
                        changed = True
                        break
                if not changed:
                    width //= 2
    finally:
        client.close()
    result = {"source": str(case_path), "fixture": original["fixture"], "target_field": field,
              "artifacts_before": artifacts, "artifacts_unchanged": all(file_hash(path) == value for path, value in artifacts.items()),
              "baseline_reproduced": True,
              "original_actions": failure["actions"], "reduced_actions": actions, "attempts": evidence,
              "scope": "bounded_action_deletion_preserves_native_reachability_not_global_minimality"}
    destination = Path(output) / (original["id"] + "-minimized.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    """Run or resume an auditable batch, or minimize an existing failed native trajectory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--oracle-checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=DEFAULT_WORKERS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--minimize", type=Path)
    parser.add_argument("--minimize-attempts", type=int, default=12)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.minimize:
        result = minimize_case(args.minimize, args.exe, args.oracle_checkout, args.output, args.minimize_attempts)
        print(json.dumps({"reduced_actions": result["reduced_actions"], "attempts": len(result["attempts"])}, ensure_ascii=False))
        return
    if args.cases < 1:
        parser.error("--cases must be positive")
    (args.output / "cases").mkdir(exist_ok=True)
    identity = manifest(args.exe, args.oracle_checkout, args.seed, args.cases, args.workers)
    path = args.output / "manifest.json"
    if path.exists():
        if not args.resume or json.loads(path.read_text(encoding="utf-8")) != identity:
            raise ValueError("Use a new output directory; resume requires the same artifacts and workload")
    else:
        path.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    cases = [fixture(index, args.seed) for index in range(args.cases)]
    started, records = time.perf_counter(), []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(shard, cases[index::args.workers], str(args.exe.resolve()),
            str(args.oracle_checkout.resolve()), str(args.output.resolve()), args.resume) for index in range(args.workers)]
        for future in as_completed(futures):
            records.extend(future.result())
    current = manifest(args.exe, args.oracle_checkout, args.seed, args.cases, args.workers)
    result = {"manifest": identity, "artifacts_unchanged": current == identity,
              "elapsed_seconds": time.perf_counter() - started, "summary": aggregate(records),
              "cases": sorted(records, key=lambda row: row["id"])}
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "elapsed_seconds": result["elapsed_seconds"],
                      "artifacts_unchanged": result["artifacts_unchanged"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

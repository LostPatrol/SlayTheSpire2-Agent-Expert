"""Run frozen full-policy cells and compare reachable PPO boundaries to a pristine executable."""

import argparse
import ctypes
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib

# Preserve a stable character order independently of file-system enumeration.
CHARACTERS = ("ironclad", "silent", "defect", "regent", "necrobinder")
# This source pin is separate from the production vendor's reviewed working changes.
REFERENCE_PIN = "28429066d91ed05c10b7b57597fc624196d197a4"
# Numerical tolerances apply only to floats; discrete tensors and picks must match exactly.
FLOAT_TOLERANCE = 1e-6


def sha256(path):
    """Hash an actual artifact rather than a model or executable filename."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    """Persist readable evidence with real newlines and no lossy string conversion."""
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clean_environment():
    """Remove inherited policy overrides while retaining ordinary toolchain environment."""
    env = dict(os.environ)
    removed = sorted(key for key in env if key.startswith("STS2"))
    for key in removed:
        del env[key]
    return env, removed


def process_cpu(process):
    """Read only this experiment child's Windows CPU accounting before its handle closes."""
    if os.name != "nt":
        return None
    values = [ctypes.c_uint64() for _ in range(4)]
    get_times = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    get_times.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    get_times.restype = ctypes.c_int
    if not get_times(int(process._handle), *(ctypes.byref(value) for value in values)):
        return {"unavailable_error": ctypes.get_last_error()}
    return {"kernel_seconds": values[2].value / 10_000_000,
            "user_seconds": values[3].value / 10_000_000}


def models(root):
    """Resolve exactly the manifest's five shipped graphs and verify their bytes."""
    manifest = root / "champions.toml"
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    result = {}
    for character in CHARACTERS:
        row = data[character]
        path = (root / row["onnx"]).resolve()
        digest = sha256(path)
        if digest.lower() != row["sha256"].lower():
            raise ValueError(f"Champion artifact mismatch: {character}")
        result[character] = {"path": str(path), "sha256": digest, "features": row.get("features", "")}
    return result


def invoke(executable, request, directory, timeout):
    """Keep every timeout, process failure and malformed response as an attempted cell."""
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "request.json", request)
    env, removed = clean_environment()
    started = time.monotonic()
    record = {"request": request, "removed_environment_keys": removed, "status": "not_started"}
    stdout, stderr = b"", b""
    try:
        process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env)
        try:
            stdout, stderr = process.communicate((json.dumps(request) + "\n").encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            # This handle identifies only the child launched for this exact evidence directory.
            process.kill()
            stdout, stderr = process.communicate()
            record["process_cpu"] = process_cpu(process)
            raise subprocess.TimeoutExpired(str(executable), timeout, output=stdout, stderr=stderr)
        record["process_cpu"] = process_cpu(process)
        record["returncode"] = process.returncode
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip().startswith("{")]
        if process.returncode:
            record["status"] = "process_error"
        elif len(parsed) != 1:
            record["status"] = "response_count_error"
        else:
            response = parsed[0]
            write_json(directory / "response.json", response)
            record["response_path"] = str((directory / "response.json").resolve())
            record["status"] = "completed" if response.get("ok") else "response_error"
            for key in ("outcome", "won", "final_hp", "act_index", "adapter_error", "combat", "source_hashes"):
                record[key] = response.get(key)
            record["action_count"] = len(response.get("steps", []))
            record["upstream_steps"] = response.get("upstream_steps")
            record["decision_count"] = len(response.get("decisions", []))
            record["terminal"] = response.get("outcome") in ("won", "lost")
            if "outcome" in response and not record["terminal"]:
                record["status"] = "noncompletion"
    except subprocess.TimeoutExpired as error:
        record["status"] = "timeout"
        stdout, stderr = error.stdout or b"", error.stderr or b""
    except Exception as error:
        record["status"] = "driver_error"
        record["error"] = f"{type(error).__name__}: {error}"
    (directory / "stdout.jsonl").write_bytes(stdout)
    (directory / "stderr.log").write_bytes(stderr)
    record["wall_seconds"] = time.monotonic() - started
    write_json(directory / "attempt.json", record)
    return record


def freeze(executable, output):
    """Copy the executable before launching cells so a later build cannot change the panel."""
    output.mkdir(parents=True, exist_ok=False)
    target = output / executable.name
    shutil.copy2(executable, target)
    shutil.copy2(Path(__file__), output / "validation-driver.py")
    source = Path(__file__).parents[1] / "mentor_simulation/host/src/alignment.rs"
    if source.is_file():
        shutil.copy2(source, output / "alignment-source.rs")
    provenance_path = executable.parent / "source-provenance.json"
    provenance = None
    if provenance_path.is_file():
        candidate = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
        if candidate.get("executable_sha256", "").lower() == sha256(target):
            provenance = candidate
            shutil.copy2(provenance_path, output / "source-provenance.json")
    compiled_alignment = next((row["sha256"].lower() for row in (provenance or {}).get("host_sources", [])
                               if row["path"] == "alignment.rs"), None)
    write_json(output / "artifact.json", {"input": str(executable.resolve()), "frozen": str(target.resolve()),
               "sha256": sha256(target), "alignment_source_sha256": sha256(source) if source.is_file() else None,
               "alignment_source_scope": "working_tree_snapshot_not_independent_build_evidence",
               "compiled_alignment_source_sha256": compiled_alignment,
               "working_alignment_matches_build_provenance": (sha256(source) == compiled_alignment)
                   if source.is_file() and compiled_alignment else None,
               "matching_executable_source_provenance_copied": provenance is not None,
               "compiler_profile": {"opt_level": 2, "lto": False, "codegen_units": 16},
               "reference_pin": REFERENCE_PIN})
    return target


def run_panel(args):
    """Run declared same-seed arms without replacing failed cells or changing their budgets."""
    model_map = models(args.models.resolve())
    host = freeze(args.host.resolve(), args.output.resolve())
    requests = []
    for mode in args.modes.split(","):
        for character in args.characters.split(","):
            for seed in args.seeds:
                weight = model_map[character]
                request = {"op": "alignment_run", "seed": seed, "character": character,
                           "model_path": weight["path"], "features": weight["features"],
                           "mode": mode, "execution_profile": args.profile, "ascension": args.ascension,
                           "act1": "overgrowth", "simulations": args.simulations,
                           "boss_simulations": args.boss_simulations, "max_steps": args.max_steps,
                           "livelock_stall_limit":args.stall_limit,
                           "timeout_ms": int(args.timeout * 1000), "rollout_policy": args.rollout,
                           "action_prior": args.prior, "value_model": args.value,
                           "search_profile": args.search_profile,"capture_internal_roots":args.capture_internal_roots}
                request["engine_stage_profile"]=args.stage_profile or ("native_ui" if mode=="public" else "served")
                if args.potion_swap is not None:
                    request["interactive_potion_swap"]=args.potion_swap=="on"
                if args.uncertainty_worlds is not None:
                    request["public_uncertainty"]={"worlds":args.uncertainty_worlds,"full_unlock_catalogue":args.full_unlock}
                requests.append((f"{mode}-{character}-{seed}", request))
    write_json(args.output / "panel.json", {"models": model_map, "requests": requests,
               "scope": "offline_simulator_full_run_not_native_game_win_rate", "jobs": args.jobs,
               "hard_timeout_seconds": args.timeout,"cooperative_timeout_seconds":args.timeout,
               "max_steps":args.max_steps,"livelock_stall_limit":args.stall_limit})
    records = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {executor.submit(invoke, host, request, args.output / name, args.timeout): name
                   for name, request in requests}
        for future in as_completed(pending):
            record = future.result()
            record["cell"] = pending[future]
            records.append(record)
            with (args.output / "attempts.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(record["cell"], record["status"], record.get("outcome"), round(record["wall_seconds"], 2), flush=True)
    write_json(args.output / "summary.json", summarize(records))


def summarize(records):
    """Keep completion, strength and adapter coverage denominators separate."""
    groups = defaultdict(list)
    for record in records:
        request = record["request"]
        groups[(request.get("mode"), request.get("execution_profile"))].append(record)
    result = {"attempts": len(records), "status": dict(Counter(r["status"] for r in records)), "arms": [],
              "scope": "simulator_only; structural boundary coverage is separate from full-run strength"}
    for (mode, profile), rows in groups.items():
        result["arms"].append({"mode": mode, "profile": profile, "attempts": len(rows),
            "status": dict(Counter(row["status"] for row in rows)),
            "terminal": sum(bool(row.get("terminal")) for row in rows),
            "wins": sum(row.get("outcome") == "won" for row in rows),
            "losses": sum(row.get("outcome") == "lost" for row in rows),
            "reported_work": reported_work(rows),
            "adapter_boundary_coverage": boundary_coverage(rows)})
    return result


def reported_work(records):
    """Aggregate available counters without converting uninstrumented or timed-out work to zero."""
    result = {}
    for metric in ("simulations_spent", "actual_rollouts", "depth_cap_hits", "belief_unsupported_cuts"):
        values = [row.get("combat", {}).get(metric)
                  for row in records if isinstance(row.get("combat"), dict)]
        known = [value for value in values if isinstance(value, (int, float))]
        result[metric] = {"sum_reported": sum(known) if known else None,
                          "known_cells": len(known), "unknown_cells": len(records) - len(known)}
    reasons = Counter()
    cpu_values = []
    for row in records:
        combat = row.get("combat") or {}
        reasons.update(combat.get("belief_cut_reasons") or {})
        cpu = row.get("process_cpu") or {}
        if "kernel_seconds" in cpu and "user_seconds" in cpu:
            cpu_values.append(cpu["kernel_seconds"] + cpu["user_seconds"])
    result["belief_cut_reasons"] = dict(reasons)
    result["process_cpu_seconds"] = {"sum_reported": sum(cpu_values) if cpu_values else None,
                                     "known_cells": len(cpu_values),
                                     "unknown_cells": len(records) - len(cpu_values)}
    result["sum_wall_seconds"] = sum(row["wall_seconds"] for row in records)
    result["timing_scope"] = "per-cell wall sums under declared concurrency; not an isolated speed benchmark"
    return result


def public_index_actions(decision):
    """Map compact imported enemy/hand indexes back to their recorded public handles."""
    actions = deepcopy(decision.get("imported_legal_actions"))
    snapshot = decision.get("imported_snapshot") or {}
    if not isinstance(actions, list):
        return None
    for action in actions:
        for field, rows, handle in (("target_index", snapshot.get("enemies"), "i"),
                                    ("hand_idx", snapshot.get("hand"), "card_index")):
            index = action.get(field)
            if index is None:
                continue
            if not isinstance(index, int) or not isinstance(rows, list) or not 0 <= index < len(rows):
                return None
            public = rows[index].get(handle)
            if not isinstance(public, int):
                return None
            action[field] = public
    return actions


def actual_public_index_actions(decision):
    """Apply a declared simulator-to-native roster map, keeping older sparse-index studies explicit."""
    actions = deepcopy(decision.get("actual_legal_actions"))
    mapping = decision.get("actual_enemy_slot_map")
    if not isinstance(actions, list):
        return None
    if mapping is None:
        return actions
    if not isinstance(mapping, list):
        return None
    slots = {row["core_index"]: row["public_index"] for row in mapping}
    for action in actions:
        target = action.get("target_index")
        if target is not None:
            if target not in slots:
                return None
            action["target_index"] = slots[target]
    return actions


def meta_action_comparison(decision):
    """Compare local ActionDesc rows; native handles still require scene-specific mapping."""
    sides = [decision.get(key) for key in ("upstream_diagnostic", "public_diagnostic")]
    if any(not isinstance(side, dict) or side.get("ok") is not True for side in sides):
        return {"available": False, "reason": "missing_or_failed_neural_diagnostic"}
    selected, distributions = [], []
    for side in sides:
        actions, probs, pick = (side.get(key) for key in ("actions", "probs", "grouped_argmax"))
        if (not isinstance(actions, list) or not actions
                or not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(actions)
                or not isinstance(probs, list) or len(probs) != len(actions)):
            return {"available": False, "reason": "invalid_action_or_probability_shape"}
        keys = [json.dumps(action, sort_keys=True, separators=(",", ":")) for action in actions]
        if len(set(keys)) != len(keys):
            return {"available": False, "reason": "duplicate_action_desc_needs_instance_disambiguation"}
        selected.append(actions[pick])
        distributions.append(dict(zip(keys, probs)))
    same_candidates = distributions[0].keys() == distributions[1].keys()
    return {"available": True, "selected_action_desc_equal": selected[0] == selected[1],
            "upstream_selected": selected[0], "public_selected": selected[1],
            "candidate_action_desc_set_equal": same_candidates,
            "max_probability_difference_by_local_action_desc": (max(abs(distributions[0][key] - distributions[1][key])
                for key in distributions[0]) if same_candidates else None),
            "scope": "first scored neural subdecision; local wire indices can differ from native handles; descriptor equality is not native action equivalence"}


def boundary_coverage(records):
    """Count every attempted boundary, keeping absent diagnostics and import failures visible."""
    counts, errors, phases = Counter(), Counter(), Counter()
    legal_first_differences = []
    native_first_differences = []
    meta_descriptor_differences = []
    for record in records:
        if not record.get("response_path"):
            counts["cell_without_response"] += 1
            continue
        response = json.loads(Path(record["response_path"]).read_text(encoding="utf-8"))
        if response.get("adapter_error"):
            errors[response["adapter_error"]] += 1
        first_legal_difference = True
        first_native_difference = True
        for ordinal, decision in enumerate(response.get("decisions", [])):
            kind = decision.get("kind", "unknown")
            counts[kind] += 1
            if kind == "meta":
                phases[decision.get("phase", "unknown")] += 1
                comparison = decision.get("comparison", {})
                valid = comparison.get("diagnostics_available") is True
                counts["meta_diagnostics_available"] += valid
                counts["meta_diagnostics_unavailable"] += not valid
                if valid:
                    counts["meta_encoder_equal"] += not comparison.get("encoder_difference_paths")
                    for key in ("candidate_order_equal", "logits_equal", "probs_equal", "group_pick_equal"):
                        counts["meta_" + key] += comparison.get(key) is True
                counts["meta_full_obs_structurally_equal"] += not comparison.get("full_obs_difference_paths")
                descriptors = meta_action_comparison(decision)
                counts["meta_action_desc_diagnostics_available"] += descriptors["available"]
                counts["meta_action_desc_diagnostics_unavailable"] += not descriptors["available"]
                if descriptors["available"]:
                    counts["meta_selected_action_desc_equal"] += descriptors["selected_action_desc_equal"]
                    counts["meta_selected_action_desc_different"] += not descriptors["selected_action_desc_equal"]
                    counts["meta_candidate_action_desc_set_equal"] += descriptors["candidate_action_desc_set_equal"]
                    if not descriptors["selected_action_desc_equal"]:
                        meta_descriptor_differences.append({"response_path": record["response_path"],
                            "decision_ordinal": ordinal, "phase": decision.get("phase"), "comparison": descriptors})
            elif kind == "combat_import":
                counts["combat_import_failed"] += bool(decision.get("import_errors"))
                counts["combat_import_blocked"] += bool(decision.get("support", {}).get("blocking_reasons"))
                counts["combat_without_selected_public_action"] += decision.get("selected_public_action") is None
                native_actual = decision.get("actual_native_legal_actions")
                native_imported = decision.get("imported_native_legal_actions")
                native_available = (isinstance(native_actual, list) and isinstance(native_imported, list)
                                    and all(isinstance(action, dict) for action in native_actual + native_imported))
                if native_available:
                    counts["combat_native_action_order_exact"] += native_actual == native_imported
                    counts["combat_native_action_order_different"] += native_actual != native_imported
                    if native_actual != native_imported and first_native_difference:
                        native_first_differences.append({"response_path": record["response_path"],
                            "decision_ordinal": ordinal, "floor": decision.get("floor"),
                            "actual_native": native_actual, "imported_native": native_imported})
                        first_native_difference = False
                else:
                    counts["combat_native_action_order_unavailable"] += 1
                actual = decision.get("actual_legal_actions")
                imported = decision.get("imported_legal_actions")
                if isinstance(actual, list) and isinstance(imported, list):
                    counts["combat_legal_index_lists_comparable"] += 1
                    counts["combat_legal_index_lists_exact"] += actual == imported
                    counts["combat_legal_index_lists_different"] += actual != imported
                    normalized = public_index_actions(decision)
                    normalized_actual = actual_public_index_actions(decision)
                    if normalized is None or normalized_actual is None:
                        counts["combat_legal_public_indexes_unavailable"] += 1
                    else:
                        counts["combat_legal_public_indexes_exact"] += normalized_actual == normalized
                        counts["combat_legal_public_indexes_different"] += normalized_actual != normalized
                    if actual != imported and first_legal_difference:
                        legal_first_differences.append({"response_path": record["response_path"],
                            "decision_ordinal": ordinal, "floor": decision.get("floor"),
                            "actual": actual, "imported": imported,
                            "actual_with_public_indexes": normalized_actual,
                            "imported_with_public_indexes": normalized})
                        first_legal_difference = False
                else:
                    counts["combat_legal_index_lists_unavailable"] += 1
    return {"counts": dict(counts), "meta_phases": dict(phases), "cell_halt_reasons": dict(errors),
            "combat_legal_first_difference_per_cell": legal_first_differences,
            "combat_native_action_first_difference_per_cell": native_first_differences,
            "meta_selected_action_desc_differences": meta_descriptor_differences,
            "legal_comparison_scope": "raw order plus mapped public enemy/hand handles; selection option order is unchanged; not a proof of card-instance mechanics"}


def numeric_difference(left, right):
    """Require matching structure and report the maximum active numeric difference."""
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return None
        values = [numeric_difference(left[k], right[k]) for k in left]
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return None
        values = [numeric_difference(a, b) for a, b in zip(left, right)]
    elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(left - right)
    else:
        return 0 if left == right else None
    return None if any(v is None for v in values) else max(values, default=0)


def diagnostics(args):
    """Compare both sides of actual reachable adapter decisions to independent pristine inference."""
    args.output.mkdir(parents=True, exist_ok=False)
    model_map = models(args.models.resolve())
    host = freeze(args.host.resolve(), args.output / "host")
    reference = freeze(args.reference_probe.resolve(), args.output / "reference")
    selected, counts = [], Counter()
    for path in args.runs.rglob("response.json"):
        response = json.loads(path.read_text(encoding="utf-8"))
        character = response.get("character", "").lower()
        for index, decision in enumerate(response.get("decisions", [])):
            if decision.get("kind") != "meta":
                continue
            boundaries = [("upstream", decision.get("upstream", {}).get("obs"), decision.get("upstream", {}).get("action_descs"), None)]
            public = decision.get("production", {})
            boundaries.append(("production", public.get("obs"), public.get("actions"), None))
            for world in public.get("uncertainty", {}).get("worlds", []):
                boundaries.append((f"production_world_{world['ordinal']}", world.get("obs"), public.get("actions"), world))
            for side, obs, actions, world in boundaries:
                key = (character, decision.get("phase"), side)
                if (world is None and (not obs or not actions)) or counts[key] >= args.per_surface:
                    continue
                counts[key] += 1
                weight = model_map[character]
                selected.append({"source": str(path.resolve()), "decision": index, "side": side,"world":world,
                    "request": {"obs": obs, "actions": actions, "character": character,
                                "features": weight["features"], "model_path": weight["path"]}})
    write_json(args.output / "selection.json", {"rule": "first N observed nonempty boundaries per character/phase/side",
               "per_surface": args.per_surface, "selected": len(selected), "counts": {str(k): v for k, v in counts.items()},
               "models": model_map, "boundaries": selected,
               "scope":"independent_pristine_NN_on_actual_boundary_payloads_not_independent_counterfactual_rule_validation"})
    results = []
    for index, boundary in enumerate(selected):
        directory = args.output / f"boundary-{index:04d}"
        directory.mkdir()
        request = boundary["request"]
        reference_record = invoke(reference, request, directory / "reference", args.timeout)
        host_record = invoke(host, {**request, "op": "network_diagnostics"}, directory / "production", args.timeout)
        result = {"index": index, "boundary": boundary, "reference_status": reference_record["status"],
                  "production_status": host_record["status"], "equal": False}
        if reference_record.get("response_path") and host_record.get("response_path"):
            ref = json.loads(Path(reference_record["response_path"]).read_text(encoding="utf-8"))
            prod = json.loads(Path(host_record["response_path"]).read_text(encoding="utf-8"))
            result["encoder_equal"] = ref.get("features") == prod.get("encoder")
            result["shape_equal"] = ref.get("network", {}).get("shape") == prod.get("shape")
            result["logits_max_abs_difference"] = numeric_difference(ref.get("network", {}).get("logits"), prod.get("raw_logits"))
            result["probs_max_abs_difference"] = numeric_difference(ref.get("network", {}).get("probs"), prod.get("probs"))
            result["value_abs_difference"] = numeric_difference(ref.get("network", {}).get("value"), prod.get("value"))
            result["pick_equal"] = ref.get("network", {}).get("grouped_argmax") == prod.get("grouped_argmax")
            result["equal"] = (ref.get("ok") is True and prod.get("ok") is True and
                result["encoder_equal"] and result["shape_equal"] and result["pick_equal"] and
                all(result[key] is not None and result[key] <= FLOAT_TOLERANCE for key in
                    ("logits_max_abs_difference", "probs_max_abs_difference", "value_abs_difference")))
        results.append(result)
        write_json(directory / "comparison.json", result)
        print("boundary", index, result["equal"], flush=True)
    write_json(args.output / "comparisons.json", results)
    write_json(args.output / "mixture-means.json", mixture_means(results, args.output))
    write_json(args.output / "summary.json", {"attempted": len(results), "equal": sum(r["equal"] for r in results),
        "not_equal_or_failed": sum(not r["equal"] for r in results), "float_tolerance": FLOAT_TOLERANCE})


def mixture_means(results, output):
    """Verify complete neural mixture arithmetic without claiming independent world-rule generation."""
    groups = defaultdict(list)
    for result in results:
        boundary = result["boundary"]
        if boundary.get("world") is not None:
            groups[(boundary["source"], boundary["decision"])].append(result)
    comparisons = []
    for (source, decision), rows in groups.items():
        root = json.loads(Path(source).read_text(encoding="utf-8"))["decisions"][decision]["production"]
        count = root["uncertainty"]["sample_count"]
        complete = len(rows) == count and all(row["equal"] for row in rows)
        comparison = {"source": source, "decision": decision, "expected_worlds": count,
                      "attempted_worlds": len(rows), "complete_independent_NN": complete, "equal": False,
                      "scope": "independent_pristine_NN_mean_and_event_argmax; world_rules_not_independently_verified"}
        if complete:
            networks = [json.loads((output / f"boundary-{row['index']:04d}/reference/response.json").read_text(encoding="utf-8"))["network"] for row in rows]
            probabilities = [sum(net["probs"][index] for net in networks) / count for index in range(len(networks[0]["probs"]))]
            value = sum(net["value"] for net in networks) / count
            comparison["probability_max_abs_difference"] = numeric_difference(probabilities, [candidate["probability"] for candidate in root["candidates"]])
            comparison["value_abs_difference"] = abs(value - root["value"])
            # This mixture is an event surface: all candidates have distinct EventChoice groups.
            comparison["event_pick_equal"] = max(range(len(probabilities)), key=probabilities.__getitem__) == root["grouped_argmax"]
            comparison["equal"] = (comparison["probability_max_abs_difference"] is not None and
                comparison["probability_max_abs_difference"] <= FLOAT_TOLERANCE and
                comparison["value_abs_difference"] <= FLOAT_TOLERANCE and comparison["event_pick_equal"])
        comparisons.append(comparison)
    return comparisons


def contracts(args):
    """Consume actual production C# DTO serialization without relabelling it as native gameplay."""
    host = freeze(args.host.resolve(), args.output.resolve())
    model_map = models(args.models.resolve())
    results = []
    for name in args.scenes:
        source = args.fixtures / f"contract-{name}.json"
        envelope = json.loads(source.read_text(encoding="utf-8-sig"))
        state = envelope["public_state"]
        if "public_history" in envelope:
            state["public_history"] = envelope["public_history"]
        character = state["run"]["character_id"].lower()
        weight = model_map[character]
        request = {"public_state": state, "model_path": weight["path"], "features": weight["features"]}
        for key in ("public_map", "selection_context", "public_uncertainty"):
            if key in envelope:
                request[key] = envelope[key]
        if state.get("screen") == "COMBAT" or (state.get("screen") == "CARD_SELECTION" and
                state.get("selection", {}).get("underlying_screen") == "COMBAT"):
            request.update(op="search", simulations=16, max_rollout_depth=12, seed=719, timeout_ms=10000)
        else:
            request["op"] = "policy_evaluate"
        record = invoke(host, request, args.output / name, args.timeout)
        shutil.copy2(source, args.output / name / "producer-fixture.json")
        record.update(scene=name, producer_fixture=str(source.resolve()), producer_sha256=sha256(source),
                      producer_scope=envelope.get("metadata"))
        results.append(record)
        print(name, record["status"], flush=True)
    write_json(args.output / "attempts.json", results)
    write_json(args.output / "summary.json", {"attempted": len(results), "status": dict(Counter(row["status"] for row in results)),
               "scope": "actual_CSharp_DTO_and_production_compactor_contract_synthetic_objects_not_native_capture"})


def components(args):
    """Compare actual pristine rollout/prior/value arithmetic on the same declared wire states."""
    host = freeze(args.host.resolve(), args.output.resolve())
    reference = freeze(args.reference_perf.resolve(), args.output / "reference")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    results = []
    for character in CHARACTERS:
        state = deepcopy(fixture)
        state["player"]["class"] = character
        if character == "regent":
            state["player"]["stars"] = 3
        if character == "necrobinder":
            state["player"]["osty"] = {"alive": True, "hp": 10, "max_hp": 15, "block": 2}
        for room in ("common", "elite", "boss"):
            directory = args.output / f"{character}-{room}"
            directory.mkdir()
            shared = {"seed": 719, "character": character, "room_type": room, "wire_state": state}
            ref_request = {**shared, "op": "network_diagnostics"}
            prod_request = {**shared, "op": "alignment_run", "diagnostic": "combat_networks",
                "network_context": {"room_type": room, "act_index": 1, "is_last_act": False, "awaiting_second_boss": False}}
            ref_attempt = invoke(reference, ref_request, directory / "reference", args.timeout)
            prod_attempt = invoke(host, prod_request, directory / "production", args.timeout)
            result = {"character": character, "room": room, "equal": False,
                      "reference_status": ref_attempt["status"], "production_status": prod_attempt["status"]}
            if ref_attempt.get("response_path") and prod_attempt.get("response_path"):
                ref = json.loads(Path(ref_attempt["response_path"]).read_text(encoding="utf-8"))
                prod = json.loads(Path(prod_attempt["response_path"]).read_text(encoding="utf-8"))
                if ref.get("ok") is True and prod.get("ok") is True:
                    left, right = ref["diagnostics"], prod["network_identity"]["diagnostics"]
                    result["component_max_abs_differences"] = {key: numeric_difference(left.get(key), right.get(key))
                        for key in ("wide_features", "root_leaf_value", "root_prior", "rollout")}
                    result["equal"] = all(value is not None and value <= FLOAT_TOLERANCE for value in result["component_max_abs_differences"].values())
                    result["reference_source_hashes"] = ref["source_hashes"]
                    result["production_source_hashes"] = prod["source_hashes"]
            results.append(result)
            write_json(directory / "comparison.json", result)
            print(character, room, result["equal"], flush=True)
    write_json(args.output / "comparisons.json", results)
    write_json(args.output / "summary.json", {"attempted": len(results), "equal": sum(row["equal"] for row in results),
        "not_equal_or_failed": sum(not row["equal"] for row in results), "float_tolerance": FLOAT_TOLERANCE,
        "scope": "same_synthetic_wire_component_math_and_room_gates_not_search_trajectory_or_native_correctness",
        "fixture_sha256": sha256(args.fixture)})


def main():
    """Expose bounded panel and independent same-payload diagnostic commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--host", type=Path, required=True)
    run.add_argument("--models", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--seeds", type=int, nargs="+", default=[1458832210])
    run.add_argument("--characters", default=",".join(CHARACTERS))
    run.add_argument("--modes", default="raw,public")
    run.add_argument("--profile", choices=("pristine", "corrected"), default="corrected")
    run.add_argument("--ascension", type=int, default=0)
    run.add_argument("--simulations", type=int, default=1000)
    run.add_argument("--boss-simulations", type=int, default=4000)
    run.add_argument("--timeout", type=float, default=600)
    run.add_argument("--max-steps", type=int, default=100000)
    run.add_argument("--stall-limit", type=int, default=256)
    run.add_argument("--jobs", type=int, default=2)
    run.add_argument("--rollout", choices=("upstream", "heuristic"), default="upstream")
    run.add_argument("--prior", choices=("upstream", "off"), default="upstream")
    run.add_argument("--value", choices=("texel", "learned"), default="texel")
    run.add_argument("--search-profile", choices=("upstream", "base"), default="upstream")
    run.add_argument("--capture-internal-roots", action="store_true")
    run.add_argument("--uncertainty-worlds", type=int)
    run.add_argument("--full-unlock", action="store_true")
    run.add_argument("--stage-profile", choices=("served","native_ui"))
    run.add_argument("--potion-swap", choices=("on","off"))
    run.set_defaults(handler=run_panel)
    diag = sub.add_parser("diagnostics")
    for key in ("host", "reference-probe", "models", "runs", "output"):
        diag.add_argument("--" + key, type=Path, required=True)
    diag.add_argument("--per-surface", type=int, default=1)
    diag.add_argument("--timeout", type=float, default=60)
    diag.set_defaults(handler=diagnostics)
    contract = sub.add_parser("contracts")
    for key in ("host", "models", "fixtures", "output"):
        contract.add_argument("--" + key, type=Path, required=True)
    contract.add_argument("--timeout", type=float, default=30)
    contract.add_argument("--scenes", nargs="+", default=["combat", "selection", "selection-confirmation", "event", "map", "shop", "rest", "reward"])
    contract.set_defaults(handler=contracts)
    component = sub.add_parser("components")
    for key in ("host", "reference-perf", "fixture", "output"):
        component.add_argument("--" + key, type=Path, required=True)
    component.add_argument("--timeout", type=float, default=30)
    component.set_defaults(handler=components)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

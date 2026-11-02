"""Run frozen pristine native-UI reference cells with retained process failures and CPU evidence."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess

import validation

# The declaration fixes the shared 25-cell panel independently of a phase's execution subset.
PANEL_SEEDS = tuple(range(1458832210, 1458832215))
# Each Rust child is bounded by wall time; failures stay in the original denominator.
DEFAULT_TIMEOUT = 600


def identity(reference):
    """Read the untouched reference checkout identity before and after a panel."""
    def read(*args):
        """Return a read-only git query with no terminal-dependent formatting."""
        return subprocess.check_output(["git", "-C", str(reference), *args], text=True,
                                       encoding="utf-8").strip()
    return {"commit": read("rev-parse", "HEAD"), "tree": read("rev-parse", "HEAD^{tree}"),
            "status": read("status", "--porcelain").splitlines()}


def freeze(executable, output, build_project):
    """Freeze only this reference binary and its independently recorded build inputs."""
    provenance = json.loads((build_project / "provenance.json").read_text(encoding="utf-8"))
    source = Path(__file__).with_name("reference_native_ui.rs")
    if validation.sha256(source) != provenance["driver_source_sha256"]:
        raise RuntimeError("Driver source differs from the prepared compilation input")
    output.mkdir(parents=True, exist_ok=False)
    target = output / executable.name
    shutil.copy2(executable, target)
    for name in ("reference_native_ui.rs", "prepare_reference_native_ui.py", "run_reference_native_ui.py",
                 "validation.py"):
        shutil.copy2(Path(__file__).with_name(name), output / name)
    shutil.copy2(build_project / "provenance.json", output / "source-provenance.json")
    shutil.copy2(build_project / "Cargo.lock", output / "Cargo.lock")
    shutil.copy2(build_project / "Cargo.toml", output / "Cargo.toml")
    env, removed = validation.clean_environment()
    metadata = subprocess.run([str(target)], input=b'{"op":"metadata"}\n', capture_output=True,
                              env=env, timeout=30, check=True)
    validation.write_json(output / "metadata.json", json.loads(metadata.stdout))
    validation.write_json(output / "artifact.json", {"input": str(executable), "frozen": str(target),
        "sha256": validation.sha256(target), "reference_pin": validation.REFERENCE_PIN,
        "driver_sha256": validation.sha256(output / "reference_native_ui.rs"),
        "build_main_sha256": validation.sha256(build_project / "src/main.rs"),
        "removed_environment_keys": removed,
        "invoke_driver_sha256": validation.sha256(output / "validation.py"),
        "cargo_lock_sha256": validation.sha256(output / "Cargo.lock"),
        "compiler_profile": {"rustc": "1.99.1", "opt_level": 2, "lto": False,
                             "codegen_units": 16, "panic": "unwind"},
        "engine_stage_profile": "native_ui", "model_backend": "upstream Tract CPU ONNX",
        "policy": "original MctsCombatPolicy<PpoPolicy> and drive_full_run",
        "recorder": "original RecordingPolicy Detail::Full; telemetry only"})
    return target


def summary(records):
    """Report all attempts without promoting failures or timeouts to terminal losses or wins."""
    terminals = [row for row in records if row.get("terminal")]
    return {"attempted": len(records), "status_counts": dict(Counter(row["status"] for row in records)),
        "terminal": len(terminals), "wins": sum(row.get("outcome") == "won" for row in terminals),
        "losses": sum(row.get("outcome") == "lost" for row in terminals),
        "wall_seconds_sum": sum(row["wall_seconds"] for row in records),
        "cpu_user_seconds_sum": sum(row.get("process_cpu", {}).get("user_seconds", 0) for row in records),
        "cpu_kernel_seconds_sum": sum(row.get("process_cpu", {}).get("kernel_seconds", 0) for row in records)}


def main():
    """Run one declared execution phase and persist each completed attempt immediately."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--build-project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    parser.add_argument("--jobs", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    reference = args.reference.resolve()
    before = identity(reference)
    if before["commit"] != validation.REFERENCE_PIN or before["status"]:
        raise RuntimeError("Native-UI reference is not the clean pin")
    prepared = json.loads((args.build_project / "provenance.json").read_text(encoding="utf-8"))
    if any(validation.sha256(reference / relative) != digest
           for relative, digest in prepared["inputs_sha256"].items()):
        raise RuntimeError("Reference build inputs differ from their recorded hashes")
    output = args.output.resolve()
    host = freeze(args.host.resolve(), output, args.build_project.resolve())
    model_map = validation.models(reference / "sts2-pycli/data/ppo")
    requests = []
    for seed in args.seeds:
        for character in validation.CHARACTERS:
            model = model_map[character]
            request = {"op": "alignment_run", "mode": "raw", "seed": seed,
                "character": character, "ascension": 0, "act1": "overgrowth",
                "model_path": model["path"], "model_sha256": model["sha256"], "features": model["features"],
                "simulations": 1000, "boss_simulations": 4000, "max_steps": 100000,
                "livelock_stall_limit": 256, "timeout_ms": args.timeout * 1000,
                "engine_stage_profile": "native_ui", "interactive_potion_swap": True,
                "direct_event_relic_grant": False, "execution_profile": "original_full_run",
                "rollout_depth": 200, "rollout_policy": "upstream", "action_prior": "upstream",
                "value_model": "texel", "search_profile": "upstream", "hidden_info": "BeliefDraw",
                "terminal_eval_mode": "RichBoth", "leaf": "TexelWide"}
            requests.append((f"reference-{character}-{seed}", request))
    validation.write_json(output / "panel.json", {"models": model_map, "requests": requests,
        "declared_main_panel_seeds": PANEL_SEEDS, "declared_main_panel_cells": 25,
        "timeout_seconds": args.timeout, "jobs": args.jobs, "reference_before": before,
        "started_at": datetime.now().astimezone().isoformat(),
        "information_scope": "Upstream complete RunManager; public production uses 32-world event prior",
        "game_scope": "Upstream rule-engine runs, not native game win rate"})
    records = []
    with (output / "attempts.jsonl").open("x", encoding="utf-8") as ledger:
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {executor.submit(validation.invoke, host, request,
                output / cell, args.timeout): cell for cell, request in requests}
            for future in as_completed(futures):
                record = future.result()
                record["cell"] = futures[future]
                validation.write_json(output / record["cell"] / "attempt.json", record)
                records.append(record)
                ledger.write(json.dumps(record, ensure_ascii=False) + "\n")
                ledger.flush()
                print(json.dumps({"character": record["request"]["character"], "seed": record["request"]["seed"],
                    "status": record["status"], "outcome": record.get("outcome"),
                    "wall_seconds": record["wall_seconds"]}), flush=True)
    records.sort(key=lambda row: (row["request"]["seed"], row["request"]["character"]))
    after = identity(reference)
    models_after = validation.models(reference / "sts2-pycli/data/ppo")
    validation.write_json(output / "summary.json", {**summary(records), "reference_after": after,
        "reference_identity_unchanged": before == after,
        "model_identity_unchanged": model_map == models_after, "records": records})
    if before != after or model_map != models_after:
        raise RuntimeError("Reference checkout changed during execution")


if __name__ == "__main__":
    main()

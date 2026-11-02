"""Run the isolated managed simulator, public replay checks, and search comparisons."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import AbstractContextManager
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from queue import Empty, Queue
import statistics
import subprocess
import sys
import threading
import time

from scenarios import PROJECT_ROOT, diagnostic_cases

# This host has no reference to the production bridge or installed game binaries.
EXPERIMENT_ROOT = Path(__file__).resolve().parent
HOST_PROJECT = EXPERIMENT_ROOT / "host/CombatSimulation.Host.csproj"
HOST_DLL = EXPERIMENT_ROOT / "host/bin/Release/net9.0/CombatSimulation.Host.dll"
DOTNET = PROJECT_ROOT / ".tools/dotnet/dotnet.exe"
DEFAULT_RUN = PROJECT_ROOT / "runs/cdd55ffd-7149-4d2f-944d-02136cd18ca0"
DEFAULT_RESULTS = EXPERIMENT_ROOT / "results"
# Limits constrain this experiment's current-turn action graph, not a complete game.
DEFAULT_DEPTH = 6
DEFAULT_BEAM_WIDTH = 16
# A wider beam is measured in the same process and repeat rotation as the default beam.
WIDE_BEAM_WIDTH = 64
DEFAULT_EXHAUSTIVE_LIMIT = 20_000
DEFAULT_REPEATS = 5
REQUEST_TIMEOUT_SECONDS = 60
SCORE_TOLERANCE = 1e-7
STRATEGIES = ("greedy", "beam", "beam64", "exhaustive")


def build_host():
    """Compile the managed host without invoking any upstream deployment target."""
    subprocess.run(
        [str(DOTNET), "build", str(HOST_PROJECT), "-c", "Release", "--nologo"],
        cwd=PROJECT_ROOT, check=True,
    )


class SimulatorClient(AbstractContextManager):
    """Keep one JSONL simulator process alive to separate startup from warm queries."""

    def __init__(self):
        """Start the managed host with bounded reads and separately drained diagnostics."""
        self.started_at = time.perf_counter()
        # Fix JIT mode only in this child process so cross-suite tier promotion is not a timing confound.
        runtime_environment = os.environ.copy()
        runtime_environment["DOTNET_TieredCompilation"] = "0"
        self.process = subprocess.Popen(
            [str(DOTNET), str(HOST_DLL)], cwd=EXPERIMENT_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            env=runtime_environment,
        )
        self.lines = Queue()
        self.errors = []
        self.first_response_elapsed_ms = None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        """Forward one complete response line at a time without an unbounded wait."""
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read_stderr(self):
        """Drain host diagnostics so a verbose failure cannot block its stdout."""
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def request(self, request):
        """Return a decoded response and its total IPC roundtrip duration."""
        started = time.perf_counter()
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=REQUEST_TIMEOUT_SECONDS)
        except Empty as exc:
            raise RuntimeError("Simulator request timed out") from exc
        if line is None:
            raise RuntimeError(f"Simulator exited: {self.errors[-5:]}")
        try:
            result = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON host output: {line[:300]}") from exc
        result["roundtrip_ms"] = (time.perf_counter() - started) * 1000
        if self.first_response_elapsed_ms is None:
            self.first_response_elapsed_ms = (time.perf_counter() - self.started_at) * 1000
        return result

    def __exit__(self, exc_type, exc_value, traceback):
        """Close the offline process, including when an experiment fails."""
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()
        return False


def write_result(path, data):
    """Save inspectable experiment evidence as UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def distribution(values):
    """Report latency dispersion without claiming a hardware-independent guarantee."""
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered), "min": min(ordered), "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], "max": max(ordered),
    }


def environment_metadata():
    """Bind local measurements to their runtime and vendored rule-core revision."""
    origin = EXPERIMENT_ROOT / "third_party/evangambit/ORIGIN.json"
    sources = [EXPERIMENT_ROOT / name for name in ("run.py", "replay.py", "scenarios.py")]
    sources.extend(sorted((EXPERIMENT_ROOT / "host").glob("*.cs")))
    sources.append(HOST_PROJECT)
    return {
        "platform": platform.platform(), "architecture": platform.machine(),
        "python": platform.python_version(),
        "dotnet_sdk": subprocess.check_output([str(DOTNET), "--version"], text=True).strip(),
        "host_runtime_environment": {"DOTNET_TieredCompilation": "0"},
        "rule_core": json.loads(origin.read_text(encoding="utf-8-sig")) if origin.exists() else None,
        "experiment_sources_sha256": {
            str(path.relative_to(EXPERIMENT_ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
    }


def write_benchmark_csv(path, rows):
    """Export one comparable row per case and strategy for quick local inspection."""
    columns = ("case", "strategy", "score", "regret", "support", "full_legal_coverage",
               "skipped", "terminal_plans", "nodes", "median_search_ms", "p95_search_ms", "median_roundtrip_ms")
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for case in rows:
            for strategy, result in case["strategies"].items():
                writer.writerow({
                    "case": case["id"], "strategy": strategy, "score": (result.get("best") or {}).get("score"),
                    "regret": result.get("regret_to_exhaustive"), "support": result.get("support", {}).get("level"),
                    "full_legal_coverage": result.get("full_legal_coverage"), "skipped": result.get("skipped"),
                    "terminal_plans": result.get("metrics", {}).get("terminal_plans"),
                    "nodes": result.get("metrics", {}).get("nodes"),
                    "median_search_ms": result["timing"]["search_ms"]["median"],
                    "p95_search_ms": result["timing"]["search_ms"]["p95"],
                    "median_roundtrip_ms": result["timing"]["roundtrip_ms"]["median"],
                })


def search_request(state, strategy, args, *, depth=None):
    """Use the same search limits and scoring defaults for every strategy."""
    return {
        "op": "search", "public_state": state, "strategy": "beam" if strategy == "beam64" else strategy,
        "max_depth": depth or args.max_depth, "beam_width": WIDE_BEAM_WIDTH if strategy == "beam64" else args.beam_width,
        "exhaustive_limit": args.exhaustive_limit, "seed": 0,
    }


def benchmark(client, args, *, corpus=False):
    """Compare tactical score and warm runtime against a thresholded exhaustive oracle."""
    cases = diagnostic_cases()
    if corpus:
        from replay import extract_cases

        extracted = extract_cases(args.run_dir)
        # The first accepted decision of every observed combat turn forms an unfiltered sample.
        first_by_turn = {}
        for case in extracted["cases"]:
            metadata = case["metadata"]
            key = (metadata["floor"], metadata["turn"])
            first_by_turn.setdefault(key, {
                "id": f"floor{key[0]}_turn{key[1]}", "state": case["before"],
                "purpose": "First accepted play of this recorded turn; no policy outcome inferred",
            })
        cases = list(first_by_turn.values())
    if not cases:
        raise RuntimeError("No benchmark positions were found")
    reused = client.first_response_elapsed_ms is not None
    first = client.request({"op": "inspect", "public_state": cases[0]["state"], "seed": 0})
    cold_ms = client.first_response_elapsed_ms
    if not first.get("ok"):
        raise RuntimeError(f"Initial public import failed: {first}")
    # JIT each strategy before measuring warm requests; retain startup separately.
    for strategy in STRATEGIES:
        client.request(search_request(cases[0]["state"], strategy, args))
    rows = []
    for case in cases:
        row = {"id": case["id"], "purpose": case["purpose"], "strategies": {},
               "public_state_sha256": hashlib.sha256(json.dumps(
                   case["state"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()}
        samples_by_strategy = {strategy: [] for strategy in STRATEGIES}
        # Rotate execution order across repeats to reduce short-term scheduling bias.
        for repeat in range(args.repeats):
            offset = repeat % len(STRATEGIES)
            for strategy in STRATEGIES[offset:] + STRATEGIES[:offset]:
                samples_by_strategy[strategy].append(client.request(search_request(
                    case["state"], strategy, args, depth=case.get("max_depth"))))
        for strategy in STRATEGIES:
            samples = samples_by_strategy[strategy]
            # Output actions/state once; timing arrays preserve variability for review.
            representative = samples[-1]
            scores = [(sample.get("best") or {}).get("score") for sample in samples]
            if any(score != scores[0] for score in scores):
                raise RuntimeError(f"Non-repeatable deterministic search: {case['id']} {strategy}")
            representative["timing"] = {
                "roundtrip_ms": distribution([sample["roundtrip_ms"] for sample in samples]),
                "search_ms": distribution([sample.get("metrics", {}).get("elapsed_ms", 0) for sample in samples]),
            }
            row["strategies"][strategy] = representative
        oracle = row["strategies"]["exhaustive"]
        if oracle.get("ok") and not oracle.get("skipped") and oracle.get("best"):
            optimum = oracle["best"]["score"]
            for strategy in STRATEGIES:
                candidate = row["strategies"][strategy]
                if candidate.get("best"):
                    candidate["regret_to_exhaustive"] = optimum - candidate["best"]["score"]
        rows.append(row)
        print(f"bench {case['id']}: " + ", ".join(
            f"{name}={'skip' if result.get('skipped') else (result.get('best') or {}).get('score', 'error')}"
            for name, result in row["strategies"].items()), flush=True)
    summary = {}
    for strategy in STRATEGIES:
        values = [row["strategies"][strategy] for row in rows]
        comparable = [item for item in values if "regret_to_exhaustive" in item]
        nontrivial_ids = {row["id"] for row in rows if not row["strategies"]["exhaustive"].get("skipped")
                         and row["strategies"]["exhaustive"].get("metrics", {}).get("terminal_plans", 0) > 1}
        nontrivial = [row["strategies"][strategy] for row in rows if row["id"] in nontrivial_ids]
        summary[strategy] = {
            "cases": len(values), "ok": sum(bool(item.get("ok")) for item in values),
            "skipped": sum(bool(item.get("skipped")) for item in values),
            "support": dict(Counter(item.get("support", {}).get("level", "unknown") for item in values)),
            "full_legal_coverage": sum(bool(item.get("full_legal_coverage")) for item in values),
            "oracle_comparable": len(comparable),
            "matches_oracle_score": sum(abs(item["regret_to_exhaustive"]) <= SCORE_TOLERANCE for item in comparable),
            "nontrivial_oracle_cases": len(nontrivial),
            "nontrivial_matches_oracle_score": sum(abs(item.get("regret_to_exhaustive", math.inf)) <= SCORE_TOLERANCE
                                                   for item in nontrivial),
            "mean_regret": statistics.mean(item["regret_to_exhaustive"] for item in comparable) if comparable else None,
            "median_search_ms_by_case": distribution([item["timing"]["search_ms"]["median"] for item in values]),
            "median_roundtrip_ms_by_case": distribution([item["timing"]["roundtrip_ms"]["median"] for item in values]),
        }
    result = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_metadata(),
        "scope": "Recorded public turns" if corpus else "Curated diagnostic public positions",
        "interpretation": "Current-turn supported action graph; heuristic scores, not full-game win rates",
        "source_run": args.run_dir.name if corpus else None,
        "selection_rule": "First accepted play before per (floor, turn), without support-based filtering" if corpus else "All diagnostic_cases()",
        "limits": {"max_depth": args.max_depth, "beam_width": args.beam_width,
                   "wide_beam_width": WIDE_BEAM_WIDTH,
                   "exhaustive_limit": args.exhaustive_limit, "repeats": args.repeats},
        "repeat_order": "Strategies rotate per repeat within each position; one shared host",
        "cold_start_and_first_import_ms": cold_ms, "summary": summary, "cases": rows,
        "host_reused_before_suite": reused, "suite_first_inspect_roundtrip_ms": first["roundtrip_ms"],
    }
    stem = "corpus-benchmark" if corpus else "benchmark"
    write_result(args.output / f"{stem}.json", result)
    write_benchmark_csv(args.output / f"{stem}.csv", rows)
    return result


def replay(client, args):
    """Compare all accepted stable public replay candidates, retaining coverage failures."""
    from replay import compare_prediction, extract_cases

    corpus = extract_cases(args.run_dir)
    rows = []
    for case in corpus["cases"]:
        imported = client.request({"op": "inspect", "public_state": case["before"], "seed": 0})
        import_comparison = compare_prediction(imported["state"], case["before"]) if imported.get("ok") else None
        prediction = client.request({
            "op": "simulate", "public_state": case["before"], "actions": [case["action"]], "seed": 0,
        })
        compared = compare_prediction(prediction["state"], case["after"]) if prediction.get("ok") else None
        rows.append({
            "id": case.get("id", case["action_id"]), "action_id": case["action_id"],
            "card_id": case["metadata"]["card_id"], "action": case["action"], "metadata": case["metadata"],
            "ok": prediction.get("ok", False), "support": prediction.get("support"),
            "errors": prediction.get("errors", []), "comparison": compared,
            "prediction": prediction.get("state"),
            "import_comparison": import_comparison,
        })
    summary = {
        "candidates": len(rows), "simulated": sum(item["ok"] for item in rows),
        "support": dict(Counter((item.get("support") or {}).get("level", "unknown") for item in rows)),
        "matched": sum(bool(item["comparison"] and item["comparison"]["matches"]) for item in rows),
        "mismatched": sum(bool(item["comparison"] and not item["comparison"]["matches"]) for item in rows),
        "not_simulated": sum(not item["ok"] for item in rows),
        "import_identity_matches": sum(bool(item["import_comparison"] and item["import_comparison"]["matches"])
                                       for item in rows),
        "mismatch_fields": dict(Counter(
            difference["field"] for item in rows if item["comparison"]
            for difference in item["comparison"]["differences"])),
    }
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "environment": environment_metadata(),
              "run": args.run_dir.name, "extraction": {key: value for key, value in corpus.items() if key != "cases"},
              "summary": summary, "cases": rows}
    write_result(args.output / "replay.json", result)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return result


def main():
    """Expose a reproducible offline CLI without importing the live controller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("benchmark", "corpus-benchmark", "replay", "request", "all", "build"))
    parser.add_argument("--input", type=Path, help="One JSON request for command=request")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--beam-width", type=int, default=DEFAULT_BEAM_WIDTH)
    parser.add_argument("--exhaustive-limit", type=int, default=DEFAULT_EXHAUSTIVE_LIMIT)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if min(args.max_depth, args.beam_width, args.exhaustive_limit, args.repeats) <= 0:
        parser.error("Limits and repeats must be positive")
    if args.build or args.command == "build" or not HOST_DLL.exists():
        build_host()
    if args.command == "build":
        return
    with SimulatorClient() as client:
        if args.command in ("benchmark", "all"):
            benchmark(client, args)
        if args.command in ("corpus-benchmark", "all"):
            benchmark(client, args, corpus=True)
        if args.command in ("replay", "all"):
            replay(client, args)
        if args.command == "request":
            if not args.input:
                parser.error("request needs --input")
            print(json.dumps(client.request(json.loads(args.input.read_text(encoding="utf-8"))),
                             ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

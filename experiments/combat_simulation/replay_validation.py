"""Re-audit immutable native trajectories against a revised contract without rerunning the oracle."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import time

from batch_validation import aggregate, brief, read_case, run_case, write_case
from mentor_production_parity import file_hash
from native_parity import JsonlProcess


class RecordedOracle:
    """Supply only previously recorded native states for the exact recorded setup and actions."""

    def __init__(self, evidence):
        """Retain immutable evidence and begin at its original public root."""
        self.evidence = evidence
        self.index = 0

    def init(self, setup):
        """Reject a changed setup rather than inventing a new native root."""
        if setup != self.evidence["fixture"]["setup"]:
            raise ValueError("Recorded oracle setup mismatch")
        return self.evidence["native_initial"]

    def step(self, action):
        """Return the next original successor only when the action is unchanged."""
        step = self.evidence["steps"][self.index]
        if action != step["action"]:
            raise ValueError("Recorded oracle action mismatch")
        self.index += 1
        return step["native"]


def mechanism_coverage(paths):
    """Describe actual native fixture diversity without calling generated cases full-rule coverage."""
    observed = {name: set() for name in ("cards", "relics", "potions", "enemies", "encounters")}
    for path in paths:
        evidence = read_case(path)
        setup = evidence["fixture"]["setup"]
        for pile in ("hand", "draw", "discard", "exhaust"):
            for card in setup.get(pile, []):
                observed["cards"].add(card if isinstance(card, str) else card["card_id"])
        for name in ("relics", "potions"):
            observed[name].update(setup.get(name, []))
        observed["encounters"].add(setup.get("encounter"))
        for enemy in evidence.get("native_initial", {}).get("endpoint", {}).get("enemies", []):
            observed["enemies"].add(enemy.get("enemy_id", enemy.get("id")))
    return {name: {"count": len(values), "identities": sorted(value for value in values if value is not None)}
            for name, values in observed.items()}


def replay_shard(paths, executable, output):
    """Recompute every host response with the original scenario seeds and original native path."""
    client = JsonlProcess([executable])
    records = []
    try:
        for path in paths:
            original = read_case(Path(path))
            if original.get("native_status") != "completed":
                row = original
                row["replay_skipped"] = "native_evidence_incomplete"
            else:
                row = run_case(client, RecordedOracle(original), original["fixture"], prescribed=original["actions"])
            row["native_evidence_source"] = {"path": path, "sha256": file_hash(path),
                                              "original_successors": len(original["steps"])}
            write_case(Path(output) / "cases" / Path(path).name, row)
            records.append(brief(row))
    finally:
        client.close()
    return records


def main():
    """Preserve old evidence and publish revised comparison results with independent fingerprints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    sources = sorted(args.source.joinpath("cases").glob("*.json.gz"))
    inputs = [args.exe, Path(__file__), Path(__file__).with_name("batch_validation.py"),
              Path(__file__).with_name("parity_contract.py"), Path(__file__).with_name("mentor_production_parity.py"),
              Path(__file__).with_name("native_parity.py"),
              args.source / "manifest.json", args.source / "summary.json"]
    artifacts = {str(path.resolve()): file_hash(path) for path in inputs}
    started, records = time.perf_counter(), []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(replay_shard, [str(path.resolve()) for path in sources[index::args.workers]],
            str(args.exe.resolve()), str(args.output.resolve())) for index in range(args.workers)]
        for future in futures:
            records.extend(future.result())
    result = {"scope": "revised_host_replay_of_recorded_native_trajectories_no_new_native_successors",
        "source": str(args.source.resolve()), "artifacts": artifacts,
        "artifacts_unchanged": all(file_hash(path) == digest for path, digest in artifacts.items()),
        "elapsed_seconds": time.perf_counter() - started, "summary": aggregate(records),
        "fixture_mechanism_coverage": mechanism_coverage(sources),
        "cases": sorted(records, key=lambda row: row["id"])}
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

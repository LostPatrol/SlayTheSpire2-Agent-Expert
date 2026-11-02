"""Compare frozen whole-run panels while retaining every incomplete attempt."""

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil

from validation import sha256, write_json

# All primary comparison arms use the deployed upstream budget and stopping limits.
PRIMARY_CONTROLS = {"ascension": 0, "act1": "overgrowth", "simulations": 1000,
                    "boss_simulations": 4000, "max_steps": 100000,
                    "livelock_stall_limit": 256, "timeout_ms": 600000,
                    "rollout_policy": "upstream", "action_prior": "upstream",
                    "value_model": "texel", "search_profile": "upstream"}


def read_json(path):
    """Read evidence without modifying its source bytes."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_rows(path):
    """Retain all JSONL attempts, including process timeouts and errors."""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]


def source_pointer(path):
    """Bind every cited source to its current bytes."""
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}


def cell_key(row):
    """Pair only the same role, seed and ascension."""
    request = row.get("request", row)
    return request["character"].lower(), int(request["seed"]), int(request["ascension"])


def outcome(row):
    """Require an observed terminal before treating a panel row as a win or loss."""
    value = row.get("outcome")
    if "request" not in row:
        return value
    if row.get("status") == "completed" and row.get("terminal") and value in ("won", "lost"):
        return value
    return value if value not in (None, "won", "lost") else row["status"]


def load_panels(directories, stage_profile):
    """Check frozen artifacts, model bytes and primary requested controls."""
    indexed, sources, model_hashes = {}, [], {}
    hashes = set()
    expected_flags = {"direct_event_relic_grant": stage_profile == "served",
                      "interactive_potion_swap": stage_profile == "native_ui"}
    for directory in directories:
        directory = directory.resolve()
        metadata = read_json(directory / "panel.json")
        artifact = read_json(directory / "artifact.json")
        frozen = Path(artifact["frozen"])
        if sha256(frozen) != artifact["sha256"].lower():
            raise ValueError(f"Frozen executable changed: {directory}")
        hashes.add(artifact["sha256"].lower())
        for character, model in metadata["models"].items():
            digest = sha256(model["path"])
            if digest != model["sha256"].lower():
                raise ValueError(f"Model bytes changed: {character}")
            if character in model_hashes and model_hashes[character] != digest:
                raise ValueError(f"Mixed model bytes: {character}")
            model_hashes[character] = digest
        requests = dict(metadata["requests"])
        attempts = read_rows(directory / "attempts.jsonl")
        if len(attempts) != len(requests):
            raise ValueError(f"Missing or duplicate panel attempts: {directory}")
        for attempt in attempts:
            key = cell_key(attempt)
            request = attempt["request"]
            if key in indexed or request != requests[attempt["cell"]]:
                raise ValueError(f"Duplicate cell or changed request: {key}")
            wanted = {**PRIMARY_CONTROLS, "engine_stage_profile": stage_profile,
                      "interactive_potion_swap": expected_flags["interactive_potion_swap"]}
            if any(request.get(field) != value for field, value in wanted.items()):
                raise ValueError(f"Different primary conditions: {key}")
            if attempt.get("response_path"):
                response = read_json(attempt["response_path"])
                if response.get("stage_flags") != expected_flags:
                    raise ValueError(f"Different actual stage flags: {key}")
            indexed[key] = {"row": attempt, "source": source_pointer(directory / "attempts.jsonl")}
        sources.append({"panel": source_pointer(directory / "panel.json"),
                        "artifact": source_pointer(directory / "artifact.json"),
                        "attempts": source_pointer(directory / "attempts.jsonl"),
                        "source_provenance": source_pointer(directory / "source-provenance.json")
                            if (directory / "source-provenance.json").is_file() else None,
                        "jobs": metadata["jobs"]})
    if len(hashes) != 1:
        raise ValueError("A final comparison arm must use one frozen executable")
    return indexed, sources, model_hashes


def main():
    """Publish a complete paired table without replacing primary timeouts with retries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, nargs="+", required=True)
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--reference-panels", type=Path, nargs="+")
    reference.add_argument("--reference-jsonl", type=Path)
    parser.add_argument("--legacy-control-evidence", type=Path, nargs="+")
    parser.add_argument("--stage-profile", choices=("served", "native_ui"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current, current_sources, current_models = load_panels(args.current, args.stage_profile)
    if args.reference_panels:
        controls, reference_sources, reference_models = load_panels(args.reference_panels, args.stage_profile)
        if current_models != reference_models:
            raise ValueError("Reference and current champion bytes differ")
    else:
        if args.stage_profile != "served" or not args.legacy_control_evidence:
            parser.error("Legacy arena rows require served conditions and their prior control audit")
        legacy_models = {}
        for path in args.legacy_control_evidence:
            evidence = read_json(path)
            if not isinstance(evidence, dict) or "executable" not in evidence:
                continue
            if sha256(evidence["executable"]) != evidence["sha256"].lower():
                raise ValueError(f"Legacy reference executable changed: {path}")
            for model in evidence.get("champions", evidence.get("models", [])):
                digest = sha256(model["reference_copy"])
                if digest != model["expected_sha256"].lower():
                    raise ValueError(f"Legacy reference model changed: {model['character']}")
                legacy_models[model["character"]] = digest
        if legacy_models != current_models:
            raise ValueError("Legacy invocation evidence must prove the same five champion bytes")
        controls = {}
        for row in read_rows(args.reference_jsonl):
            key = cell_key(row)
            if key in controls or row.get("policy") != "ppo+eval-cheap" or row.get("act1") != "overgrowth":
                raise ValueError(f"Duplicate or differently configured legacy cell: {key}")
            controls[key] = {"row": row, "source": source_pointer(args.reference_jsonl)}
        reference_sources = [source_pointer(args.reference_jsonl)] + [
            source_pointer(path) for path in args.legacy_control_evidence]
    if set(current) != set(controls):
        raise ValueError("Both arms must contain exactly the same attempted cells")
    pairs = []
    for key in sorted(current):
        treatment, control = current[key], controls[key]
        left, right = outcome(control["row"]), outcome(treatment["row"])
        terminal = left in ("won", "lost") and right in ("won", "lost")
        pairs.append({"character": key[0], "seed": key[1], "ascension": key[2],
                      "reference_outcome": left, "current_outcome": right,
                      "both_terminal": terminal,
                      "equal_if_both_terminal": left == right if terminal else None,
                      "reference": control, "current": treatment})
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(Path(__file__), args.output / "compare-driver.py")
    write_json(args.output / "paired-cells.json", pairs)
    write_json(args.output / "summary.json", {
        "attempted_pairs": len(pairs), "stage_profile": args.stage_profile,
        "reference_outcomes": dict(Counter(row["reference_outcome"] for row in pairs)),
        "current_outcomes": dict(Counter(row["current_outcome"] for row in pairs)),
        "common_terminal_pairs": sum(row["both_terminal"] for row in pairs),
        "common_terminal_transitions": dict(Counter(
            row["reference_outcome"] + "->" + row["current_outcome"]
            for row in pairs if row["both_terminal"])),
        "incomplete_pairs": sum(not row["both_terminal"] for row in pairs),
        "champion_hashes": current_models, "requested_primary_controls": PRIMARY_CONTROLS,
        "current_sources": current_sources, "reference_sources": reference_sources,
        "comparison_driver": source_pointer(args.output / "compare-driver.py"),
        "scope": "paired offline simulator outcomes; complete public priors and original full-state "
                 "information can differ; source repairs can change semantics; no native-game "
                 "win-rate, formal noninferiority or wall-time efficiency claim"})
    print(args.output.resolve())


if __name__ == "__main__":
    main()

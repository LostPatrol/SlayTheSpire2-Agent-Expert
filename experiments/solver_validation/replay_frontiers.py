"""Replay literal public frontiers without colliding across source versions."""

import argparse
import json
from pathlib import Path
import shutil

from validation import freeze, invoke, sha256, write_json


def main():
    """Keep one uniquely named input and response for every requested source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--sources", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--simulations", type=int, default=16)
    parser.add_argument("--max-rollout-depth", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    host = freeze(args.host.resolve(), args.output.resolve())
    shutil.copy2(Path(__file__), args.output / "replay-driver.py")
    rows = []
    for ordinal, source in enumerate(args.sources):
        source = source.resolve()
        digest = sha256(source)
        frontier = json.loads(source.read_text(encoding="utf-8-sig"))
        decision = frontier["last_decision"]
        projection = decision["projection"]
        original = frontier["attempt"]["request"]
        request = {"op": "search", "public_state": projection["public_state"],
                   "public_map": projection.get("public_map", {}),
                   "simulations": args.simulations,
                   "max_rollout_depth": args.max_rollout_depth,
                   "timeout_ms": int(args.timeout * 1000),
                   "rollout_policy": original.get("rollout_policy", "upstream"),
                   "action_prior": original.get("action_prior", "upstream"),
                   "value_model": original.get("value_model", "texel"),
                   "search_profile": original.get("search_profile", "upstream")}
        # The ordinal also preserves duplicate sources as separate attempts.
        version = source.parent.parent.name
        name = f"{ordinal:03d}-{version}-{source.stem}-{digest[:12]}"
        directory = args.output / name
        attempt = invoke(host, request, directory, args.timeout)
        shutil.copy2(source, directory / "original-frontier.json")
        response = (json.loads(Path(attempt["response_path"]).read_text(encoding="utf-8"))
                    if attempt.get("response_path") else {})
        row = {"source": str(source), "source_sha256": digest,
               "directory": str(directory.resolve()), "original_request": original,
               "input_public_state_equal": request["public_state"] == projection["public_state"],
               "status": attempt["status"], "ok": response.get("ok"),
               "blocking": response.get("support", {}).get("blocking_reasons", []),
               "actions": response.get("best", {}).get("native_actions")
                   if isinstance(response.get("best"), dict) else None}
        write_json(directory / "source-result.json", row)
        rows.append(row)
        print(name, row["status"], row["ok"], row["blocking"], flush=True)
    write_json(args.output / "summary.json", {
        "attempted": len(rows), "available": sum(row["ok"] is True for row in rows),
        "unavailable_or_failed": sum(row["ok"] is not True for row in rows), "rows": rows,
        "scope": "literal public-root admission under declared diagnostic search settings; "
                 "not an unchanged full-run outcome or original search-statistics replay"})


if __name__ == "__main__":
    main()

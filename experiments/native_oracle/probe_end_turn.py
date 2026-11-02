"""Explore single-dispatch native turn completion without accepting it as validated oracle evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from native_oracle import DEFAULT_CHECKOUT, NativeOracle

# This small probe spans simple and multi-enemy scheduling, not full-game coverage.
ENCOUNTERS = ("NIBBITS_WEAK", "NIBBITS_NORMAL", "EXOSKELETONS_WEAK")


def main():
    """Record only public snapshots; the ordinary step API still rejects end_turn."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    adapter = args.checkout / "src/Sts2Headless/RunSimulator.cs"
    if "native_end_turn_not_settled_no_retry" not in adapter.read_text(encoding="utf-8"):
        raise SystemExit("Prepare the no-retry oracle adapter before probing end_turn")
    provenance = json.loads((args.checkout / "evidence/provenance.json").read_text(encoding="utf-8"))
    assembly = args.checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll"
    if (provenance.get("adapter_sha256") != hashlib.sha256(adapter.read_bytes()).hexdigest()
            or provenance.get("assembly_sha256") != hashlib.sha256(assembly.read_bytes()).hexdigest()):
        raise SystemExit("Build provenance does not match the inspected no-retry oracle")
    records = []
    with NativeOracle(args.checkout, runtime_dir=args.output.parent / "turn-probe-runtime") as oracle:
        for encounter in ENCOUNTERS:
            record = {"encounter": encounter, "validated": False}
            try:
                record["initial"] = oracle.init({"hand": ["DEFEND_IRONCLAD"],
                    "draw": ["STRIKE_IRONCLAD"] * 5, "discard": [], "exhaust": [], "encounter": encounter})
                # This diagnostic uses the repaired original handler, never a replacement combat loop.
                response = oracle.request({"cmd": "action", "action": "end_turn"})
                record["response_type"] = response.get("type")
                record["after"] = oracle.request({"cmd": "oracle_snapshot"})
                record["round_advanced"] = record["after"]["public_state"]["turn"] > record["initial"]["public_state"]["turn"]
                record["status"] = "observed_single_dispatch_completion" if record["round_advanced"] else "no_round_advance"
            except Exception as error:
                record.update(status="unsupported", error=str(error))
            records.append(record)
            print(encounter, record["status"], record.get("error", ""), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"end_turn_validated": False,
        "limitations": ["Godot/scheduling stubs remain", "No true-client phase calibration",
                        "No explicit native draw-transaction completion signal observed"],
        "cases": records}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

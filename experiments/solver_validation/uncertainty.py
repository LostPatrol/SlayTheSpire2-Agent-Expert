"""Freeze the host and reuse the existing native finite-order probability validator."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

# The original validator owns native setup, probability enumeration and statistical checks.
ROOT = Path(__file__).resolve().parents[2]


def main():
    """Keep both validation evidence and native runtime files under a new study directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--oracle-checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=240)
    parser.add_argument("--seed", type=int, default=99173)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = args.output.resolve() / "mentor-simulation.exe"
    shutil.copy2(args.exe, frozen)
    command = [sys.executable, str(ROOT / "experiments/combat_simulation/uncertainty_validation.py"),
        "--exe", str(frozen), "--oracle-checkout", str(args.oracle_checkout.resolve()),
        "--output", str(args.output.resolve() / "result.json"), "--samples", str(args.samples),
        "--seed", str(args.seed)]
    raise SystemExit(subprocess.run(command, check=False).returncode)


if __name__ == "__main__":
    main()

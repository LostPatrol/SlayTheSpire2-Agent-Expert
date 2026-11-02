"""Freeze two independent runtime roots for a same-model high/low live comparison.

This tool never launches a game, calls a provider, or reads/copies save progress.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


# Runtime identities differ; all remaining policy and provider settings must match.
ARM_IDENTITIES = {"high": ("2026102901", 18865, 18866), "low": ("2026102902", 18965, 18966)}
# Freeze only the runtime package, audited engine artifacts, and provenance inputs.
FROZEN_DIRECTORIES = (
    "src/sts2_agent", "local/simulation/mentor", "experiments/mentor_simulation/host/src",
    "experiments/mentor_simulation/patches",
)
# These files support schema validation and reproducible artifact records.
FROZEN_FILES = (
    "configs/agent.schema.json", "configs/bridge.settings.json", "uv.lock", "pyproject.toml",
    "experiments/mentor_simulation/host/Cargo.lock", "experiments/mentor_simulation/host/Cargo.toml",
)
# Probe imports and immutable resources only; it never constructs a live session or HTTP request.
VERIFY_SCRIPT = """
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'src'))
import sts2_agent
from sts2_agent.config import load_config
from sts2_agent.knowledge import KnowledgeBase
from sts2_agent.strategies import StrategyLibrary
from sts2_agent.simulation import CombatSimulator
from sts2_agent.dashboard import WEB_DIRECTORY
assert Path(sts2_agent.__file__).resolve().is_relative_to(root / 'src')
config = load_config('configs/agent.json', root)
knowledge = KnowledgeBase(root)
strategies = StrategyLibrary(root)
simulator = CombatSimulator(config, root)
identity = simulator.rule_identity()
assert knowledge.status == 'ready', knowledge.warning
assert strategies.sections, strategies.warning
assert identity.get('artifact_sha256')
assert WEB_DIRECTORY.resolve().is_relative_to(root / 'src')
assert (WEB_DIRECTORY / 'index.html').is_file()
assert Path(config['provider']['api_key_file']).is_absolute()
simulator.close()
print(json.dumps({'root': str(root), 'package': sts2_agent.__file__,
    'knowledge_snapshot': knowledge.snapshot_id, 'strategy_sections': len(strategies.sections),
    'simulation_identity': identity, 'reasoning_effort': config['provider']['reasoning_effort']}))
"""


def write_json(path: Path, payload: dict) -> None:
    """Write a newly prepared public configuration or freeze manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    """Hash copied public artifacts without reading credentials or save files."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_file(source: Path, target: Path) -> None:
    """Copy one selected runtime dependency into a new root."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def normalized_config(config: dict) -> dict:
    """Remove only experimental effort and process binding from equality checks."""
    result = deepcopy(config)
    result["provider"].pop("reasoning_effort")
    result["bridge"].pop("base_url")
    return result


def freeze_common(source: Path, target: Path) -> dict:
    """Freeze one complete source snapshot and detect source changes during the copy."""
    selected = [source / name for name in FROZEN_FILES]
    for name in FROZEN_DIRECTORIES:
        selected.extend(path for path in (source / name).rglob("*")
                        if path.is_file() and "__pycache__" not in path.parts
                        and path.suffix != ".pyc")
    pointer = source / "knowledge/current.json"
    snapshot = json.loads(pointer.read_text(encoding="utf-8"))["snapshot_id"]
    if not isinstance(snapshot, str) or Path(snapshot).name != snapshot or snapshot in {".", ".."}:
        raise ValueError("Invalid knowledge snapshot identifier")
    selected.extend([pointer, source / f"knowledge/snapshots/{snapshot}/sources.lock.json",
                     source / f"knowledge/snapshots/{snapshot}/entities.jsonl"])
    selected.extend((source / "knowledge/strategies").glob("*.json"))
    originals = {path.relative_to(source).as_posix(): sha256(path) for path in selected}
    for relative in originals:
        copy_file(source / relative, target / relative)
    if any(sha256(source / name) != digest or sha256(target / name) != digest
           for name, digest in originals.items()):
        raise RuntimeError("Sources changed during snapshot; keep this evidence and prepare a new destination.")
    return originals


def write_launchers(root: Path, python: Path, dashboard_port: int, launcher: Path | None) -> None:
    """Create arm-local dashboard entrypoints and an explicit native-launch boundary."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    bootstrap = '''"""Run the frozen package with its own ledger and dashboard port."""
import sys
from pathlib import Path

# This arm's source takes precedence over the shared environment's editable install.
RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT / "src"))
from sts2_agent.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--root", str(RUNTIME_ROOT), "--config", "configs/agent.json",
                "dashboard", "--port", "DASHBOARD_PORT", "--no-browser", *sys.argv[1:]]
    raise SystemExit(main())
'''.replace("DASHBOARD_PORT", str(dashboard_port))
    (scripts / "dashboard.py").write_text(bootstrap, encoding="utf-8")
    quoted_python = str(python).replace("'", "''")
    (scripts / "start-dashboard.ps1").write_text(
        "# Start this arm's frozen dashboard without launching a native game.\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"& '{quoted_python}' (Join-Path $PSScriptRoot 'dashboard.py') @args\n"
        "exit $LASTEXITCODE\n", encoding="utf-8")
    if launcher:
        copy_file(launcher, scripts / "start-game.ps1")
    else:
        (scripts / "start-game.ps1").write_text(
            "# Native clients are explicitly started by the dual-client experiment launcher.\n"
            "throw 'Start this arm with experiments/dual_client/start-client.ps1; "
            "the dashboard can then attach to its healthy bridge.'\n", encoding="utf-8")


def prepare(args: argparse.Namespace) -> dict:
    """Create distinct fresh arm directories and validate the frozen runtime without live I/O."""
    source = args.source.resolve()
    destination = args.destination.resolve()
    if destination == source or not destination.is_relative_to(source / "local"):
        raise ValueError("Destination must be a new directory under this project's local directory.")
    if destination.exists() and not destination.is_dir():
        raise FileExistsError("Destination is not a directory.")
    if any((destination / name).exists() for name in ("high", "low", "experiment.json")):
        raise FileExistsError("An arm or experiment manifest exists; preparation never overwrites prior evidence.")
    python = source / ".venv/Scripts/python.exe"
    if not python.is_file():
        raise FileNotFoundError("Project Python environment is missing.")
    base_path = Path(args.config)
    if not base_path.is_absolute():
        base_path = source / base_path
    config = json.loads(base_path.read_text(encoding="utf-8-sig"))
    if config.get("simulation", {}).get("backend") != "mentor" or not config["simulation"].get("enabled"):
        raise ValueError("This comparison requires the enabled Mentor backend.")
    key_path = Path(config["provider"]["api_key_file"])
    config["provider"]["api_key_file"] = str((source / key_path).resolve())
    config["bridge"]["expected_pid"] = None
    machine = json.loads((source / "configs/machine.json").read_text(encoding="utf-8"))
    identities = {
        "high": (args.high_client_id, args.high_bridge_port, args.high_dashboard_port),
        "low": (args.low_client_id, args.low_bridge_port, args.low_dashboard_port),
    }
    clients = [values[0] for values in identities.values()]
    ports = [port for values in identities.values() for port in values[1:]]
    if len(set(clients)) != 2 or any(not value.isdecimal() or value == machine["client_id"] for value in clients):
        raise ValueError("Two distinct numeric client IDs, separate from the main client, are required.")
    if len(set(ports)) != 4 or any(not 1 <= port <= 65535 or port in {machine["api_port"], 18766} for port in ports):
        raise ValueError("Four distinct experiment ports, separate from the main ports, are required.")
    destination.mkdir(parents=True, exist_ok=True)
    high = destination / "high"
    common_hashes = freeze_common(source, high)
    shutil.copytree(high, destination / "low")
    results = {}
    configurations = []
    for arm, (client_id, bridge_port, dashboard_port) in identities.items():
        root = destination / arm
        arm_config = deepcopy(config)
        arm_config["provider"]["reasoning_effort"] = arm
        arm_config["bridge"]["base_url"] = f"http://127.0.0.1:{bridge_port}"
        write_json(root / "configs/agent.json", arm_config)
        write_json(root / "configs/machine.json", {**machine, "client_id": client_id, "api_port": bridge_port})
        write_launchers(root, python, dashboard_port, args.game_launcher)
        (root / "runs").mkdir()
        (root / "local/simulation-budgets").mkdir(parents=True)
        configurations.append(normalized_config(arm_config))
        checked = subprocess.run([str(python), "-B", "-c", VERIFY_SCRIPT, str(root)],
                                 cwd=root, capture_output=True, text=True, check=True)
        results[arm] = {"root": str(root), "client_id": client_id, "bridge_port": bridge_port,
                        "dashboard_port": dashboard_port, "validation": json.loads(checked.stdout)}
        write_json(root / "runtime-freeze.json", {
            "arm": arm, "source_root": str(source), "prepared_utc": datetime.now(timezone.utc).isoformat(),
            "frozen_artifacts": common_hashes, "config_sha256": sha256(root / "configs/agent.json"),
            "shared_python": str(python), "progress": "Not copied or inspected by this tool.",
        })
    if configurations[0] != configurations[1]:
        raise RuntimeError("Arm configurations differ beyond effort and bridge endpoint.")
    # Both writers must be acquirable together because their ledgers belong to different roots.
    lock_probe = """
import sys
from pathlib import Path
high, low = (Path(value).resolve() for value in sys.argv[1:])
sys.path.insert(0, str(high / 'src'))
from sts2_agent.telemetry import WriterLock
assert (high / 'local').resolve() != (low / 'local').resolve()
with WriterLock(high / 'local/controller.lock'), WriterLock(low / 'local/controller.lock'):
    with WriterLock(high / 'local/dashboard.lock'), WriterLock(low / 'local/dashboard.lock'):
        pass
"""
    subprocess.run([str(python), "-B", "-c", lock_probe, str(high), str(destination / "low")],
                   cwd=destination, capture_output=True, text=True, check=True)
    write_json(destination / "experiment.json", {"arms": results, "policy_difference": "reasoning_effort only",
               "independent_writer_locks_verified": True,
               "progress_copy_required": True, "shared_python": str(python)})
    return results


def main() -> int:
    """Parse explicit paths and create a non-overwriting isolated comparison snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--config", default="configs/a0-simulation.json")
    parser.add_argument("--game-launcher", type=Path, help="Optional root-aware start-game.ps1 copied to each arm")
    for arm, (client_id, bridge, dashboard) in ARM_IDENTITIES.items():
        parser.add_argument(f"--{arm}-client-id", default=client_id)
        parser.add_argument(f"--{arm}-bridge-port", type=int, default=bridge)
        parser.add_argument(f"--{arm}-dashboard-port", type=int, default=dashboard)
    args = parser.parse_args()
    print(json.dumps(prepare(args), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

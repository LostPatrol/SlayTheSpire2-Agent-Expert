"""Stage only the upstream Python modules and registries used by the persistent mirror."""

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys

# These original modules form the mirror, native-state translation, and card-choice import closure.
PYTHON_FILES = (
    "combat_mirror.py", "realgame_adapter.py", "realgame_mcts.py", "realgame_random.py",
    "realgame_bridge.py", "runtime_paths.py", "sts2_client.py", "sts2_hint/__init__.py",
    "sts2_hint/_annotate.py", "sts2_hint/adapter.py", "sts2_hint/canonical_maps.py",
    "sts2_hint/errors.py", "sts2_hint/recommender.py", "sts2_hint/schema.py",
)
# The original canonical maps load exactly these four localization registries.
REGISTRY_FILES = ("cards.json", "powers.json", "relics.json", "potions.json")


def source_files(source: Path) -> list[tuple[Path, Path]]:
    """Keep the upstream relative data layout required by its unchanged runtime_paths module."""
    hub = source / "sts2-pycli"
    return ([(hub / "python" / name, Path("python") / name) for name in PYTHON_FILES]
            + [(hub / "localization_eng" / name, Path("localization_eng") / name)
               for name in REGISTRY_FILES])


def stage(source: Path, destination: Path) -> dict:
    """Copy the small import closure and report actual file identities and explicit model location."""
    inventory = []
    for path, relative in source_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copy2(path, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Runtime file copy differs: {relative}")
        inventory.append({"path": relative.as_posix(), "source": str(path.resolve()),
                          "bytes": target.stat().st_size, "sha256": digest})
    result = {"files": inventory, "python_path": "python", "extension": "mentor_native.pyd",
              "module_alias": {"sts2_rs": "mentor_native"}, "ppo_model_dir": "models/ppo",
              "model_path_contract": "Pass the resolved ppo_model_dir as request.model_dir; Python's executable directory is not the extension directory.",
              "source_layout": "original_modules_and_four_localization_registries"}
    (destination / "runtime-manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def inspect_native(destination: Path) -> dict:
    """Verify packaged imports and query the actual native library without the vendor import path."""
    sys.path.insert(0, str(destination.resolve()))
    sys.path.insert(0, str((destination / "python").resolve()))
    native = importlib.import_module("mentor_native")
    sys.modules["sts2_rs"] = native
    for name in ("combat_mirror", "realgame_adapter", "realgame_mcts"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Import escaped the runtime package: {name}")
    if not callable(importlib.import_module("realgame_mcts").prepare_card_select_mirror):
        raise ValueError("Missing the shared upstream card-choice preparation function")
    metadata = json.loads(native.simulation_handle('{"op":"metadata"}'))
    if not metadata.get("ok") or not metadata.get("rule_snapshot"):
        raise ValueError("Native extension did not report its compiled rule identity")
    return metadata


def main() -> None:
    """Expose runtime input listing, packaging, and import validation to PowerShell builds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--list-inputs", action="store_true")
    arguments = parser.parse_args()
    if arguments.list_inputs:
        print(json.dumps([str(path.resolve()) for path, _ in source_files(arguments.source)]))
        return
    if arguments.destination is None:
        parser.error("--destination is required when staging the runtime")
    result = stage(arguments.source, arguments.destination)
    result["native_metadata"] = inspect_native(arguments.destination)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

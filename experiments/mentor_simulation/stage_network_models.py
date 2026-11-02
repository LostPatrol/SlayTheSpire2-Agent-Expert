"""Package pinned upstream network assets with verified champion identities."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tomllib


def stage(source: Path, destination: Path) -> dict:
    """Verify every source before copying the deployment's complete model inventory."""
    ppo_root = source / "sts2-pycli/data/ppo"
    manifest_path = ppo_root / "champions.toml"
    champions = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    assets = []
    for character, champion in champions.items():
        if not isinstance(champion, dict) or "onnx" not in champion:
            continue
        path = ppo_root / champion["onnx"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != champion["sha256"]:
            raise ValueError(f"Champion identity mismatch: {character}")
        assets.append((path, Path("ppo") / path.name, {"character": character,
            "features": champion.get("features", ""), "sha256": digest}))
    if len(assets) != 5:
        raise ValueError("Expected the five pinned character champions")
    for group in ("rollout_nets", "prior_nets", "value_policy_net"):
        for path in sorted((source / "sts2-rs/data" / group).glob("*.json")):
            assets.append((path, Path(group) / path.name,
                {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    # The committed leaf asset is a legacy trained model, not a current-schema champion.
    if not any(relative == Path("value_policy_net/h16.json") for _, relative, _ in assets):
        raise ValueError("Missing the trained legacy value model")
    inventory = []
    for path, relative, metadata in assets:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != metadata["sha256"]:
            raise ValueError(f"Deployed model copy differs: {relative}")
        inventory.append({"path": relative.as_posix(), "source": str(path.resolve()),
            "bytes": target.stat().st_size, **metadata})
    shutil.copy2(manifest_path, destination / "ppo/champions.toml")
    result = {"models": inventory, "champion_manifest_sha256":
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    (destination / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    """Expose asset staging to the existing PowerShell release build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(stage(arguments.source, arguments.destination)))


if __name__ == "__main__":
    main()

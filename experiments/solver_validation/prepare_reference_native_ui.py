"""Prepare an isolated native-UI driver without changing the pinned upstream checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

# This clean reference is independent of reviewed production source repairs.
PIN = "28429066d91ed05c10b7b57597fc624196d197a4"
# The expected tree detects an unintended commit or checkout substitution.
TREE = "259a26a48a90402ab6bc507f616cb1bd9b2cbbe9"


def git_read(reference, *args):
    """Read reference identity without changing its index or working tree."""
    return subprocess.check_output(["git", "-C", str(reference), *args], text=True,
                                   encoding="utf-8").strip()


def sha256(path):
    """Hash a concrete source or lock file for independent build evidence."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """Copy only the caller-owned driver and retain the upstream dependency lock."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference, output = args.reference.resolve(), args.output.resolve()
    commit = git_read(reference, "rev-parse", "HEAD")
    tree = git_read(reference, "rev-parse", "HEAD^{tree}")
    status = git_read(reference, "status", "--porcelain")
    if commit != PIN or tree != TREE or status:
        raise RuntimeError("Reference must remain the clean pinned upstream tree")
    output.mkdir(parents=True, exist_ok=False)
    (output / "src").mkdir()
    source = Path(__file__).with_name("reference_native_ui.rs")
    shutil.copy2(source, output / "src/main.rs")
    crates = reference / "sts2-rs/crates"
    manifest = '[package]\nname="upstream-reference-native-ui"\nversion="0.1.0"\nedition="2021"\n\n[workspace]\n\n[dependencies]\n'
    for name in ("sts2-core", "sts2-content", "sts2-runai", "sts2-mcts"):
        manifest += f'{name}={{path={json.dumps((crates / name).as_posix())}}}\n'
    manifest += 'serde_json="1"\n\n[profile.release]\nopt-level=2\nlto=false\ncodegen-units=16\npanic="unwind"\n'
    (output / "Cargo.toml").write_text(manifest, encoding="utf-8")
    lock = reference / "sts2-rs/Cargo.lock"
    shutil.copy2(lock, output / "Cargo.lock")
    inputs = {str(path.relative_to(reference)).replace("\\", "/"): sha256(path)
              for path in (reference / "sts2-rs").rglob("*")
              if path.is_file() and (path.suffix in (".rs", ".json", ".csv", ".toml") or path.name == "Cargo.lock")}
    provenance = {"upstream_commit": commit, "upstream_tree": tree, "status": [],
                  "driver_source_sha256": sha256(source), "original_lock_sha256": sha256(lock),
                  "inputs_sha256": inputs, "core_modified": False, "models_modified": False}
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()

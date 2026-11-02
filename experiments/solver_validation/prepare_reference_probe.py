"""Prepare a standalone diagnostics executable without changing the pristine reference tree."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

# The reference source is pinned independently of vendor working-tree fixes.
PIN = "28429066d91ed05c10b7b57597fc624196d197a4"


def main():
    """Require a clean pin and generate only the caller-owned probe project."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-value-compat-patch", action="store_true")
    args = parser.parse_args()
    reference = args.reference.resolve()
    commit = subprocess.check_output(["git", "-C", str(reference), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
    status = subprocess.check_output(["git", "-C", str(reference), "status", "--porcelain"], text=True, encoding="utf-8").strip()
    expected="M sts2-rs/crates/sts2-mcts/src/value_net.rs"
    permitted_patch=args.allow_value_compat_patch and status==expected
    if commit != PIN or (status and not permitted_patch):
        raise RuntimeError("Reference must be the clean pinned upstream tree")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "src").mkdir()
    source = Path(__file__).with_name("reference_probe.rs")
    shutil.copy2(source, args.output / "src/main.rs")
    crates = reference / "sts2-rs/crates"
    manifest = '[package]\nname="upstream-reference-probe"\nversion="0.1.0"\nedition="2021"\n\n[workspace]\n\n[dependencies]\n'
    for name in ("sts2-core", "sts2-content", "sts2-runai", "sts2-mcts", "sts2-wire"):
        manifest += f'{name}={{path={json.dumps((crates / name).as_posix())}}}\n'
    manifest += 'serde_json="1"\n\n[features]\nlegacy-prefix=[]\n\n[profile.release]\nopt-level=2\nlto=false\ncodegen-units=16\npanic="unwind"\n'
    (args.output / "Cargo.toml").write_text(manifest, encoding="utf-8")
    shutil.copy2(reference / "sts2-rs/Cargo.lock", args.output / "Cargo.lock")
    provenance = {"upstream_commit": commit, "upstream_tree": subprocess.check_output([
        "git", "-C", str(reference), "rev-parse", "HEAD^{tree}"], text=True, encoding="utf-8").strip(),
        "clean": not status,"value_compat_variant":permitted_patch,
        "source_diff":subprocess.check_output(["git","-C",str(reference),"diff","--","sts2-rs/crates/sts2-mcts/src/value_net.rs"],text=True,encoding="utf-8"),
        "probe_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()

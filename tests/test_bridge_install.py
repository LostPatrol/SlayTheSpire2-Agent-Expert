"""Verify that a prepared bridge update keeps a recoverable copy without touching game saves."""

from pathlib import Path
import shutil
import subprocess

import pytest

# The installer is tested only against disposable package directories.
INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install-built-bridge.ps1"
# Preserve the complete existing mod package when replacing its DLL.
PACKAGE_FILES = ("STS2AIAgent.dll", "STS2AIAgent.pck", "mod_id.json")


def test_prepared_bridge_installs_once_and_retains_previous_package(tmp_path):
    """A second install is a no-op, and unrelated save bytes survive both invocations."""
    powershell = shutil.which("pwsh")
    if not powershell:
        powershell = str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe")
    if not Path(powershell).is_file():
        pytest.skip("PowerShell 7 is required for the native package installer")
    stage = tmp_path / "vendor/STS2-Agent/build/mods/STS2AIAgent"
    installed = tmp_path / "game/mods"
    stage.mkdir(parents=True)
    installed.mkdir(parents=True)
    for name in PACKAGE_FILES:
        (stage / name).write_bytes(b"new-" + name.encode())
        (installed / name).write_bytes(b"old-" + name.encode())
    save = tmp_path / "game/save.keep"
    save.write_bytes(b"existing-progress")
    command = [powershell, "-NoProfile", "-File", str(INSTALLER),
               "-ProjectRoot", str(tmp_path), "-GameRoot", str(tmp_path / "game")]
    subprocess.run(command, capture_output=True, check=True, timeout=15)
    subprocess.run(command, capture_output=True, check=True, timeout=15)
    backups = list((tmp_path / "local/mod-backups").iterdir())
    assert len(backups) == 1
    for name in PACKAGE_FILES:
        assert (installed / name).read_bytes() == b"new-" + name.encode()
        assert (backups[0] / name).read_bytes() == b"old-" + name.encode()
    assert save.read_bytes() == b"existing-progress"

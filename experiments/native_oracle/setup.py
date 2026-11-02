"""Prepare a pinned headless native oracle using local game DLL copies only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from native_oracle import DEFAULT_CHECKOUT, DOTNET, ROOT

# This pre-beta adapter needs two documented API initialization/name adjustments for v0.107.1.
CLI_COMMIT = "d11aa883b582dd68bd39b331f3370746b30d447e"
# The current upstream default branch was also evaluated, but targets v0.111.0.
INSPECTED_HEAD = "c9083faae643e7955ba6663de561dee84aa9abb0"
# Native assets are never copied into this publishable source directory.
DEPENDENCIES = (
    "sts2.dll", "SmartFormat.dll", "SmartFormat.ZString.dll", "Sentry.dll",
    "Steamworks.NET.dll", "MonoMod.Backports.dll", "MonoMod.ILHelpers.dll", "0Harmony.dll", "System.IO.Hashing.dll",
)


def command(args, cwd):
    """Run a setup command without changing global settings or invoking shell scripts."""
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, text=True,
                            encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Setup command failed ({result.returncode}):\n{result.stdout[-6000:]}\n{result.stderr[-2000:]}")
    return result.stdout


def replace_once(text, old, new):
    """Apply one reviewed source adaptation without silently accepting an unknown upstream layout."""
    if new in text:
        return text
    if old not in text:
        raise RuntimeError("Pinned native adapter source does not match the reviewed setup replacement")
    return text.replace(old, new, 1)


def file_hash(path):
    """Fingerprint local dependencies without reading or distributing their contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(checkout: Path, game_root: Path):
    """Build copied native dependencies and explicit test controls without patching installed files."""
    checkout = checkout.resolve()
    game_lib = game_root.resolve() / "data_sts2_windows_x86_64"
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        command(["git", "clone", "--depth", "1", "https://github.com/wuhao21/sts2-cli.git", checkout], ROOT)
        command(["git", "fetch", "--depth", "1", "origin", CLI_COMMIT], checkout)
        command(["git", "checkout", "--detach", CLI_COMMIT], checkout)
    revision = command(["git", "rev-parse", "HEAD"], checkout).strip()
    if revision != CLI_COMMIT:
        raise RuntimeError("Existing checkout uses another ref; select an unused --checkout directory")
    lib = checkout / "lib"
    lib.mkdir(exist_ok=True)
    for filename in DEPENDENCIES:
        source = game_lib / filename
        if not source.is_file():
            raise RuntimeError(f"Local native dependency missing: {source}")
        destination = lib / filename
        if not destination.exists() or file_hash(source) != file_hash(destination):
            shutil.copy2(source, destination)
    simulator = checkout / "src/Sts2Headless/RunSimulator.cs"
    source = simulator.read_text(encoding="utf-8")
    adaptations = [
        ("public class RunSimulator", "public partial class RunSimulator"),
        ('                        if (model != null) list.Add(model.ToMutable());',
         '                        if (model != null)\n                        {\n'
         '                            // Native relic hooks require the owner even in synthetic fixtures.\n'
         '                            var mutable = model.ToMutable();\n'
         '                            mutable.Owner = player;\n                            list.Add(mutable);\n                        }'),
        ("RunManager.Instance.SetUpSavedSinglePlayer(_runState, save);",
         "RunManager.Instance.SetUpSavedSingleplayer(_runState, save).GetAwaiter().GetResult();"),
        ("        TestMode.IsOn = true;",
         "        TestMode.IsOn = true;\n        // v0.107.1 requires model discovery initialized; TestMode skips installed mods.\n"
         "        MegaCrit.Sts2.Core.Modding.ModManager.Initialize(\n"
         "            new MegaCrit.Sts2.Core.Modding.ModManagerFileIo(), null, null).GetAwaiter().GetResult();"),
        ("                if (neutralizeOnPlay != null)",
         "                // Oracle mode must execute native card effects without replacements.\n"
         "                if (neutralizeOnPlay != null && Environment.GetEnvironmentVariable(\"STS2_ORACLE\") != \"1\")"),
        ("            if (afterPotions.Contains(potion))\n            {",
         "            if (afterPotions.Contains(potion))\n            {\n"
         "                if (Environment.GetEnvironmentVariable(\"STS2_ORACLE\") == \"1\")\n"
         "                    return Error(\"native_potion_not_consumed\");"),
        ('            Log($"Use potion failed: {ex.Message}");',
         '            Log($"Use potion failed: {ex.Message}");\n'
         '            if (Environment.GetEnvironmentVariable("STS2_ORACLE") == "1")\n'
         '                return ErrorWithTrace("Native potion failed", ex);'),
        ('        // Second fallback: if still stuck after SuppressYield window, cancel and retry.',
         '        // Oracle diagnostics must never cancel/re-dispatch or manufacture a terminal outcome.\n'
         '        if (Environment.GetEnvironmentVariable("STS2_ORACLE") == "1"\n'
         '            && CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)\n'
         '            return Error("native_end_turn_not_settled_no_retry");\n\n'
         '        // Second fallback: if still stuck after SuppressYield window, cancel and retry.'),
        ('        public void ResolvePending(IEnumerable<CardModel> selected)\n        {\n'
         '            _pendingTcs?.TrySetResult(selected);\n            PendingOptions = null;\n            _pendingTcs = null;\n        }',
         '        /// <summary>Detach the old prompt before native continuations can open another one.</summary>\n'
         '        public void ResolvePending(IEnumerable<CardModel> selected)\n        {\n'
         '            var completion = _pendingTcs;\n            PendingOptions = null;\n            _pendingTcs = null;\n'
         '            // Completing this task may synchronously install the next prompt.\n'
         '            completion?.TrySetResult(selected);\n        }'),
        ('        public void CancelPending()\n        {\n'
         '            _pendingTcs?.TrySetResult(Array.Empty<CardModel>());\n            PendingOptions = null;\n            _pendingTcs = null;\n        }',
         '        /// <summary>Close only this prompt, preserving any prompt opened by its continuation.</summary>\n'
         '        public void CancelPending()\n        {\n'
         '            var completion = _pendingTcs;\n            PendingOptions = null;\n            _pendingTcs = null;\n'
         '            completion?.TrySetResult(Array.Empty<CardModel>());\n        }'),
    ]
    for old, new in adaptations:
        source = replace_once(source, old, new)
    if simulator.read_text(encoding="utf-8") != source:
        simulator.write_text(source, encoding="utf-8")
    program = checkout / "src/Sts2Headless/Program.cs"
    source = program.read_text(encoding="utf-8")
    source = replace_once(source, '        switch (cmdType)\n        {',
        '        switch (cmdType)\n        {\n'
        '            case "oracle_setup":\n                return sim.OracleSetup(cmd.GetProperty("setup"));\n'
        '            case "oracle_snapshot":\n                return sim.OracleSnapshot();\n'
        '            case "oracle_step":\n                return sim.OracleStep(cmd.GetProperty("action"));')
    if program.read_text(encoding="utf-8") != source:
        program.write_text(source, encoding="utf-8")
    control_source = Path(__file__).with_name("OracleControl.cs")
    control_target = simulator.with_name("OracleControl.cs")
    if not control_target.exists() or file_hash(control_source) != file_hash(control_target):
        shutil.copy2(control_source, control_target)
    output = command([DOTNET, "build", simulator.with_name("Sts2Headless.csproj"), "--nologo"], checkout)
    evidence = checkout / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "build.log").write_text(output, encoding="utf-8")
    release = json.loads((game_root / "release_info.json").read_text(encoding="utf-8"))
    provenance = {
        "repository": "https://github.com/wuhao21/sts2-cli", "commit": CLI_COMMIT,
        "inspected_head": INSPECTED_HEAD, "license": "MIT", "native_release_info": release,
        "original_game_dll_sha256": file_hash(game_lib / "sts2.dll"),
        "copied_game_dll_sha256": file_hash(lib / "sts2.dll"), "game_dll_il_modified": False,
        "adapter_sha256": file_hash(simulator), "oracle_control_sha256": file_hash(simulator.with_name("OracleControl.cs")),
        "assembly_sha256": file_hash(checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll"),
        "semantics": "native game DLL with Godot stubs and upstream scheduling/localization Harmony patches",
        "oracle_exclusions": ["Neutralize replacement disabled", "manual potion consumption fallbacks fail explicitly",
                              "end-turn diagnostics fail before cancellation/retry/forced terminal; not GUI-validated",
                              "shop/event/full-run compatibility paths not used as combat oracle evidence"],
    }
    (evidence / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps(provenance, indent=2))


def main():
    """Accept local dependency locations; never discover or read a player's save directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--game-root", type=Path, default=Path(r"D:\Steam\steamapps\common\Slay the Spire 2"))
    args = parser.parse_args()
    prepare(args.checkout, args.game_root)


if __name__ == "__main__":
    main()

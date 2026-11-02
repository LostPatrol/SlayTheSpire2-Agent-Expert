"""Stage an isolated upstream C# harness with unmodified local DLLs and strict recovery checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

# Only an explicitly provided source library is copied; no installed game or save is modified.
ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    """Bind staged sources and native assemblies to exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """Copy upstream harness sources, disable semantic replacement, and compile locally."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-lib", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--osty-fixture", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    upstream = ROOT / "vendor/spire-mentor/sts2-pycli"
    for project in ("Sts2Headless", "GodotStubs"):
        shutil.copytree(upstream / "src" / project, args.output / "src" / project,
                        ignore=shutil.ignore_patterns("bin", "obj"))
    library = args.output / "lib/v0.107.1"
    library.mkdir(parents=True)
    hashes = {}
    for source in args.source_lib.glob("*.dll"):
        destination = library / source.name
        shutil.copy2(source, destination)
        hashes[source.name] = {"source": str(source.resolve()), "sha256": digest(source),
                               "copied_sha256": digest(destination)}
    localization = args.source_lib / "localization_eng"
    if localization.is_dir():
        shutil.copytree(localization, library / "localization_eng")
    harness = args.output / "src/Sts2Headless/RunSimulator.cs"
    source = harness.read_text(encoding="utf-8")
    original_hash = digest(harness)
    replacements = {
        "if (neutralizeOnPlay != null)":
            'if (Environment.GetEnvironmentVariable("STS2_VALIDATION_STRICT") != "1" && neutralizeOnPlay != null)',
        'Log("EndTurn stuck, cancelling and retrying with SuppressYield...");':
            '// Strict validation rejects unfinished work before any cancellation or re-dispatch.\n'
            '                if (Environment.GetEnvironmentVariable("STS2_VALIDATION_STRICT") == "1")\n'
            '                    throw new InvalidOperationException("strict_validation_unsettled_end_turn");\n'
            '                Log("EndTurn stuck, cancelling and retrying with SuppressYield...");',
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError("Upstream source changed; strict staging anchor is ambiguous: " + old)
        source = source.replace(old, new)
    if args.osty_fixture:
        anchor = '            Log($"SetPlayer: hp={player.Creature?.CurrentHp} gold={player.Gold} relics={player.Relics.Count} deck={player.Deck?.Cards?.Count}");'
        if source.count(anchor) != 1:
            raise RuntimeError("Osty fixture insertion anchor changed")
        injection = '''            // Isolated native fixture setup; all attack and power hooks still use native commands.
            if (args.TryGetValue("osty_strength", out var strengthElement))
            {
                if (player.Osty == null) return Error("validation_osty_missing_play_bodyguard_first");
                var pet = player.Osty;
                SetField(pet, "_maxHp", args["osty_max_hp"].GetInt32());
                SetField(pet, "_currentHp", args["osty_hp"].GetInt32());
                PowerCmd.Apply<StrengthPower>(new ThrowingPlayerChoiceContext(), pet,
                    strengthElement.GetInt32(), player.Creature, null).GetAwaiter().GetResult();
                return new Dictionary<string, object?> { ["type"]="ok",
                    ["osty_hp"]=pet.CurrentHp, ["osty_max_hp"]=pet.MaxHp,
                    ["osty_strength"]=pet.Powers.OfType<StrengthPower>().Sum(power=>power.Amount) };
            }
'''
        source = source.replace(anchor, injection + anchor)
    harness.write_text(source, encoding="utf-8")
    manifest = {"native_dlls": hashes, "game_dll_modified": False,
        "upstream_source_sha256": original_hash, "strict_source_sha256": digest(harness),
        "strict_environment": {"STS2_VALIDATION_STRICT": "1"},
        "changes": ["Neutralize.OnPlay replacement disabled", "end_turn recovery throws before cancel/retry"],
        "osty_fixture": args.osty_fixture,
        "scope": "Godot stubs and upstream scheduling/localization patches remain"}
    (args.output / "provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    command = [str(ROOT / ".tools/dotnet/dotnet.exe"), "build",
               str(args.output / "src/Sts2Headless/Sts2Headless.csproj"), "--nologo"]
    with (args.output / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"headless build failed: {result.returncode}; inspect build.log")
    print(args.output.resolve())


if __name__ == "__main__":
    main()

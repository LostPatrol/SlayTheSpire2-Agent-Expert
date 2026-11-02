"""Freeze approved Spire Codex static references, preserving source files and licenses."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


# This reviewed public repository revision is independent of its mutable hosted API.
REVISION = "2af6543ca7a6c76acfde6be582651c7c05e88e50"
# Only normal-play entity files enter the imported corpus; runs and developer tools do not.
KINDS = ("cards", "characters", "relics", "potions", "monsters", "encounters", "events",
         "powers", "keywords", "glossary", "orbs", "enchantments", "afflictions",
         "intents", "acts", "ascensions", "rest_site_options")
# These mechanics articles are developer/testing tools, not normal gameplay references.
EXCLUDED_MECHANICS = frozenset({"dev-console", "autoslay", "the-deprived"})
# An internal contradictory move pattern is excluded until independently corrected.
CONFLICTS = {("monsters", "CALCIFIED_CULTIST"): "attack_pattern description says always Incantation, "
             "but the state list also includes Dark Strike and omits transitions."}
# Common entity fields are kept verbatim; presentation assets and dialogue are not model knowledge.
FIELDS = {"cards": ("cost", "star_cost", "is_x_cost", "is_x_star_cost", "type", "rarity", "color",
                    "target", "upgrade_description", "keywords", "powers_applied", "upgrade", "vars"),
          "characters": ("starting_hp", "starting_gold", "max_energy", "orb_slots", "starting_deck", "starting_relics"),
          "relics": ("rarity", "pool", "notes"), "potions": ("rarity", "target", "pool"),
          "monsters": ("type", "min_hp", "max_hp", "min_hp_ascension", "max_hp_ascension",
                       "moves", "innate_powers", "attack_pattern"),
          "encounters": ("room_type", "is_weak", "act", "monsters"),
          "events": ("type", "act", "preconditions", "options", "pages", "relics"),
          "powers": ("type", "stack_type", "allow_negative"), "orbs": (),
          "enchantments": ("extra_card_text", "card_type", "applicable_to", "is_stackable"),
          "afflictions": ("extra_card_text", "is_stackable"), "keywords": (), "glossary": ("category",),
          "intents": (), "acts": ("index", "num_rooms", "bosses", "events", "encounters"),
          "ascensions": ("level",), "rest_site_options": ()}
# Remove markup used by the game's card renderer while preserving dynamic template braces.
MARKUP = re.compile(r"\[/?(?:[a-zA-Z_]+)(?:=[^\]]+)?\]")


def git(repository: Path, *args: str) -> bytes:
    """Read immutable objects directly, avoiding dirty working-tree content."""
    return subprocess.check_output(["git", "-C", str(repository), *args])


def encoded(value: object) -> bytes:
    """Write stable UTF-8 JSON for reproducible hashes."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def clean(value: object) -> object:
    """Strip visual tags recursively without resolving unknown dynamic values."""
    if isinstance(value, str):
        return MARKUP.sub("", value)
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if item is not None}
    return value


def import_snapshot(root: Path, repository: Path) -> dict:
    """Import only fixed stable files, retaining all unknowns and build evidence."""
    if git(repository, "rev-parse", REVISION).decode().strip() != REVISION:
        raise ValueError("Requested source revision is not available")
    source_files = {}

    def read(path: str) -> bytes:
        """Retain each original source object once for attribution and exact reimport."""
        content = git(repository, "show", f"{REVISION}:{path}")
        source_files[path] = content
        return content

    evidence_path = "extraction-manifests/public-stable.json.gz"
    evidence = json.loads(gzip.decompress(read(evidence_path)))
    # Compatibility comes from the published configuration, not private deployment evidence.
    config = json.loads((root / "configs/a0-simulation.json").read_text(encoding="utf-8"))
    game_version = config["bridge"]["expected_game_version"]
    machine = json.loads((root / "configs/machine.json").read_text(encoding="utf-8"))
    # The installed Steam manifest supplies build evidence without touching game state.
    game_directory = Path(machine.get("game_root", "D:/Steam/steamapps/common/Slay the Spire 2"))
    steam_manifest = game_directory.parent.parent / "appmanifest_2868840.acf"
    manifest_text = steam_manifest.read_text(encoding="utf-8") if steam_manifest.exists() else ""
    match = re.search(r'"buildid"\s+"(\d+)"', manifest_text)
    local_build = match[1] if match else None
    matched = bool(local_build and str(evidence.get("build_id")) == local_build)
    verification = "source_build_matched" if matched else "source_version_unverified"
    entries = []
    conflicts = []
    for kind in KINDS:
        path = f"data/eng/{kind}.json"
        rows = json.loads(read(path))
        translated = {row["id"]: row for row in json.loads(read(f"data/zhs/{kind}.json"))}
        for row in rows:
            identifier = row["id"]
            if identifier in {"DEPRECATED_ACT", "DEPRIVED"} or row.get("multiplayer_only"):
                continue
            properties = {field: clean(row[field]) for field in FIELDS[kind] if row.get(field) is not None}
            if kind == "events":
                # Narrative follow-up pages are browser material; choice consequences are sufficient model context.
                properties["pages"] = [{"id": page.get("id"), "options": clean(page.get("options", []))}
                                       for page in (row.get("pages") or []) if page.get("options")]
            conflict = CONFLICTS.get((kind, identifier))
            if conflict:
                properties.pop("attack_pattern", None)
                properties["conflict_notice"] = conflict
                conflicts.append({"fact_id": f"{kind}.{identifier}.attack_pattern", "reason": conflict,
                                  "status": "conflicting", "action": "excluded_field"})
            description = clean(row.get("description") or "")
            entry = {"fact_id": f"{kind}.{identifier}", "kind": kind, "entity_id": identifier,
                     "name": row.get("name", identifier),
                     "aliases": [translated[identifier]["name"]] if translated.get(identifier, {}).get("name") else [],
                     "description": description, "properties": properties,
                     "conditions": {"game_version": game_version if matched else None,
                                    "build_id": evidence.get("build_id"), "channel": "stable",
                                    "ascension": "Use explicit normal/ascension variants; live observation wins."},
                     "source": {"source_id": "spire_codex", "revision": REVISION, "path": path,
                                "sha256": hashlib.sha256(source_files[path]).hexdigest()},
                     "admissibility": "allowed", "verification": verification,
                     "allowed_uses": ["retrieval", "strategy_reference"]}
            raw = row.get("description_raw")
            if raw:
                entry["description_template"] = clean(raw)
            if kind in {"powers", "enchantments", "afflictions", "orbs"} and raw:
                entry["sample_description"] = entry.pop("description")
                entry["description"] = clean(raw)
                entry["dynamic_values"] = "Template variables and sample defaults are not current values. "
                entry["dynamic_values"] += "Use observed_amounts and current public text; unresolved values remain unknown."
            if "{" in description:
                conflicts.append({"fact_id": entry["fact_id"], "status": "incomplete",
                                  "reason": "Unresolved placeholder retained, never filled with zero."})
            entries.append(entry)
    for raw_path in git(repository, "ls-tree", "-r", "--name-only", REVISION, "data/mechanics_pages").decode().splitlines():
        slug = Path(raw_path).stem
        if slug in EXCLUDED_MECHANICS or not raw_path.endswith(".md"):
            continue
        raw = read(raw_path).decode("utf-8")
        title = re.search(r"^title:\s*(.+)$", raw, re.MULTILINE)
        entries.append({"fact_id": f"mechanics.{slug}", "kind": "mechanics", "entity_id": slug,
                        "name": title[1] if title else slug, "aliases": [], "description": raw,
                        "properties": {}, "conditions": {"game_version": None, "channel": "shared_prose"},
                        "source": {"source_id": "spire_codex", "revision": REVISION, "path": raw_path,
                                   "sha256": hashlib.sha256(source_files[raw_path]).hexdigest()},
                        "admissibility": "allowed", "verification": "unversioned_reference",
                        "allowed_uses": ["retrieval", "strategy_reference"]})
    for path in ("LICENSE.md", "API_TERMS.md", "README.md", "data/mechanics_constants.json"):
        read(path)
    entries.sort(key=lambda entry: entry["fact_id"])
    corpus = "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                     for entry in entries).encode("utf-8")
    corpus_hash = hashlib.sha256(corpus).hexdigest()
    snapshot_id = f"spire-codex-{game_version}-{REVISION[:8]}-{corpus_hash[:10]}"
    target = root / "knowledge/snapshots" / snapshot_id
    if target.exists():
        print(f"Snapshot already exists: {snapshot_id}", file=sys.stderr)
        (root / "knowledge/current.json").write_bytes(encoded({"snapshot_id": snapshot_id}))
        return json.loads((target / "sources.lock.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True)
    for path, content in source_files.items():
        destination = target / "raw" / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (target / "entities.jsonl").write_bytes(corpus)
    (target / "conflicts.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in conflicts), encoding="utf-8")
    lock = {"snapshot_id": snapshot_id, "generated_utc": datetime.now(timezone.utc).isoformat(),
            "target": {"game": "STS2", "version": game_version, "build_id": local_build,
                       "channel": "stable", "mode": "single_player", "rules_mods": []},
            "source": {"name": "Spire Codex", "repository": "https://github.com/ptrlrd/spire-codex",
                       "revision": REVISION, "license": "PolyForm Noncommercial 1.0.0",
                       "notice": "Copyright © 2025-present Peter Lord and Spire Codex contributors. Game content © Mega Crit.",
                       "build_evidence": {"path": evidence_path, "build_id": evidence.get("build_id"),
                                          "local_manifest": str(steam_manifest), "matched": matched}},
            "verification": verification, "corpus_sha256": corpus_hash,
            "coverage": dict(sorted(Counter(entry["kind"] for entry in entries).items())),
            "conflict_count": len(conflicts), "excluded_mechanics": sorted(EXCLUDED_MECHANICS),
            "allowed_uses": ["retrieval", "strategy_reference"],
            "limitations": ["Matching extraction build does not independently validate parser rules.",
                            "Mechanics prose is not versioned and is only available in the human browser.",
                            "Static sample Power counts never replace current observed amounts.",
                            "No calculator or action-validation permission is granted by this corpus."],
            "files": [{"path": path, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                      for path, content in sorted(source_files.items())]}
    (target / "sources.lock.json").write_bytes(encoded(lock))
    (root / "knowledge/current.json").write_bytes(encoded({"snapshot_id": snapshot_id}))
    return lock


def main() -> None:
    """Offer one reproducible offline import command using the existing project environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = import_snapshot(root, (args.source or root / "vendor/spire-codex").resolve())
    print(json.dumps({key: result[key] for key in ("snapshot_id", "verification", "coverage", "conflict_count")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Import complete strategy sections from the frozen public Spire Codex Git object."""

import hashlib
import json
from pathlib import Path
import re
import subprocess


# Keep prose provenance separate from the build-matched mechanics snapshot.
REVISION = "2af6543ca7a6c76acfde6be582651c7c05e88e50"
# These guides cover general decisions and each supported character without importing run data.
GUIDE_IDS = {"builds-guide", "new-players-guide", "understanding-encounters",
             "ironclad-tier-list", "silent-tier-list", "defect-tier-list",
             "necrobinder-tier-list", "regent-tier-list"}


def split_sections(guide: dict) -> list[dict]:
    """Preserve each level-two section, including all nested headings and complete paragraphs."""
    content = guide["content"]
    headings = list(re.finditer(r"^## (.+)$", content, re.MULTILINE))
    introduction = content[:headings[0].start()].strip() if headings else ""
    result = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        title = heading.group(1)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        character = guide.get("character")
        if guide["id"] == "builds-guide":
            character = title.split(":", 1)[0].lower()
        result.append({"id": guide["id"] + "/" + slug, "guide_id": guide["id"],
            "title": title, "guide_title": guide["title"], "author": guide.get("author"),
            "date": guide.get("date"), "updated": guide.get("updated"),
            "character": character, "tags": guide.get("tags", []),
            "guide_difficulty": guide.get("difficulty"), "ascension": None,
            "introduction": introduction, "markdown": content[heading.start():end].strip(),
            "verification": "unversioned_strategy_reference",
            "source": {"repository": "https://github.com/ptrlrd/spire-codex",
                "path": "data/guides.json", "revision": REVISION,
                "url": "https://spire-codex.com/guides/" + guide["slug"]}})
    return result


def main() -> None:
    """Write a reproducible local corpus while leaving frozen mechanics snapshots untouched."""
    root = Path(__file__).resolve().parents[1]
    raw = subprocess.check_output(["git", "-C", str(root / "vendor/spire-codex"),
                                   "show", REVISION + ":data/guides.json"])
    guides = [guide for guide in json.loads(raw) if guide["id"] in GUIDE_IDS]
    sections = [section for guide in guides for section in split_sections(guide)]
    target = root / "knowledge/strategies"
    target.mkdir(parents=True, exist_ok=True)
    corpus = (json.dumps(sections, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (target / "sections.json").write_bytes(corpus)
    (target / "guides.json").write_text(json.dumps(guides, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    manifest = {"schema_version": 1, "source_revision": REVISION,
        "source_path": "data/guides.json", "source_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus).hexdigest(),
        "license": "PolyForm Noncommercial 1.0.0", "guide_count": len(guides),
        "section_count": len(sections), "verification": "unversioned_strategy_reference",
        "notice": "Copyright © 2025-present Peter Lord and Spire Codex contributors. "
                  "Game content © Mega Crit. Community prose is advice, not verified game rules."}
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    print(json.dumps({"guides": len(guides), "sections": len(sections)}))


if __name__ == "__main__":
    main()

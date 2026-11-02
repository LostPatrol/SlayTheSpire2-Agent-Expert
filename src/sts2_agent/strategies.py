"""Retrieve attributed strategy prose separately from exact rules and current game state."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


# Guide names remain independent of the active character and ascension configuration.
CHARACTER_ALIASES = {"铁甲战士": "ironclad", "静默猎手": "silent", "故障机器人": "defect",
                     "亡灵契约师": "necrobinder", "储君": "regent"}
# Whole sections are selected by decision context, never clipped halfway through a rule or example.
SCENE_SECTIONS = {
    "REWARD": ["Deckbuilding Basics", "When to Skip Cards", "Ideal Deck Size",
               "Three Questions to Ask Before Taking a Card"],
    "SHOP": ["Three Questions to Ask Before Taking a Card", "When to Skip Cards",
             "Don't Be Afraid to Use Them", "Make Room for New Potions"],
    "MAP": ["Why Pathing Matters", "Balancing Risk and Reward", "Think About the Next Challenge"],
    "REST": ["Resting: Staying Alive", "Upgrading Cards: Growing Stronger",
             "Balancing Risk and Reward", "Look for Your Best Upgrade Each Run"],
    "COMBAT": ["Don't Be Afraid to Use Them", "When Potions Are Most Valuable", "Reading Enemy Intents"],
}
# Search aliases make the Chinese spectator vocabulary useful against English guide originals.
SEARCH_ALIASES = {"选牌": "taking card deck", "跳过": "skip", "牌组规模": "deck size",
                  "用药": "potion", "药水": "potion", "路线": "pathing map", "营火": "campfire",
                  "构筑": "build", "铁甲": "ironclad", "精英": "elite"}
# This is a search result limit, not an automatic truncation of section bodies.
MAX_SEARCH_RESULTS = 20
# Source guide ratings can include multiplayer and later patches; current mechanics always win.
STRATEGY_USAGE = (
    "Community strategy reference, not verified calculator rules or mandatory card rankings. "
    "Use the author/date/character metadata; guide difficulty is reader level, not ascension. "
    "Guides may describe other versions, multiplayer samples, or unavailable cards. Check live rules. "
    "Solve current and near-term threats before investing in later encounters; do not force archetypes. "
    "Statistics are observational community ratings, not causal win probabilities for this run."
)


def _character(value: object) -> str | None:
    """Normalize observed character IDs or localized names without inventing an active class."""
    if not isinstance(value, str):
        return None
    return CHARACTER_ALIASES.get(value, value.lower().replace("_", ""))


class StrategyLibrary:
    """Load frozen complete sections for automatic context and optional read-only retrieval."""

    def __init__(self, root: str | Path):
        """Load one local strategy corpus; missing advice never disables base gameplay."""
        self.root = Path(root)
        self.sections = []
        self.manifest = {}
        self.warning = None
        try:
            directory = self.root / "knowledge/strategies"
            self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            raw = (directory / "sections.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.manifest["corpus_sha256"]:
                raise ValueError("Strategy corpus differs from its import manifest")
            self.sections = json.loads(raw)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.warning = str(error)

    def lookup(self, identifier: str) -> dict | None:
        """Return one complete section, or all sections of an explicitly requested guide."""
        section = next((entry for entry in self.sections if entry["id"] == identifier), None)
        if section is not None:
            return deepcopy(section)
        entries = [entry for entry in self.sections if entry["guide_id"] == identifier]
        return {"guide_id": identifier, "sections": deepcopy(entries), "usage": STRATEGY_USAGE} if entries else None

    def search(self, query: str = "", character: str | None = None, limit: int = 10) -> list[dict]:
        """Search complete section text and attribution, filtering only explicitly requested class."""
        query = str(query).strip().lower()
        terms = re.findall(r"[\w'-]+", SEARCH_ALIASES.get(query, query))
        selected_character = _character(character)
        ranked = []
        for entry in self.sections:
            if selected_character and entry.get("character") not in {None, selected_character}:
                continue
            title = " ".join([entry["id"], entry["title"], *entry["tags"]]).lower()
            body = (title + " " + entry["markdown"]).lower()
            score = sum((4 if term in title else 1) for term in terms if term in body)
            if not terms or score:
                ranked.append((-score, entry["id"], entry))
        ranked.sort(key=lambda item: item[:2])
        return deepcopy([entry for _, _, entry in ranked[:max(1, min(int(limit), MAX_SEARCH_RESULTS))]])

    def context_for_model(self, observation: dict) -> dict:
        """Select intact relevant sections and a matching build overview for strategic decisions."""
        state = observation.get("state", observation)
        run = state.get("run", {})
        character = _character(run.get("character_id") or run.get("character"))
        screen = state.get("screen")
        titles = SCENE_SECTIONS.get(screen, [])
        selected = [entry for entry in self.sections if entry["title"] in titles
                    and entry.get("character") in {None, character}]
        if screen in {"REWARD", "SHOP"} and character:
            selected.extend(entry for entry in self.sections if entry["guide_id"] == "builds-guide"
                            and entry.get("character") == character)
        return {"status": "ready" if self.sections else "unavailable", "usage": STRATEGY_USAGE,
                "source_revision": self.manifest.get("source_revision"), "warning": self.warning,
                "screen": screen, "character": character, "ascension": run.get("ascension"),
                "sections": deepcopy(selected)}

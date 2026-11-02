"""Read frozen public references and select rules related to observed entities."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


# Snapshot files are never refreshed during a provider's lifetime.
DEFAULT_POINTER = "knowledge/current.json"
# Reviewed public reward chains are additive references, never edits to frozen source entities.
RELATED_POINTER = "knowledge/strategies/relations.json"
# These public identifiers connect observed entities to approved static reference kinds.
ID_KINDS = {"card_id": "cards", "card_ids": "cards", "enemy_id": "monsters",
            "event_id": "events", "potion_id": "potions", "relic_id": "relics",
            "relic_ids": "relics", "preview_relic_ids": "relics",
            "power_id": "powers", "orb_id": "orbs",
            "enchantment_id": "enchantments", "affliction_id": "afflictions",
            "character_id": "characters", "boss_id": "encounters"}
# Current card restrictions and their rule dependencies precede background equipment.
KIND_PRIORITY = {"afflictions": -4, "keywords": -3, "enchantments": -2,
                 "powers": -8, "monsters": 2, "events": 3, "cards": 4,
                 "potions": 5, "orbs": 6, "glossary": 7, "relics": 12,
                 "characters": 13, "encounters": 14, "mechanics": 20}
# Named rules may link to other rules; never expand the entire entity corpus recursively.
RULE_KINDS = frozenset({"powers", "afflictions", "enchantments", "keywords", "glossary"})
# Card display compatibility reads visible named modifiers, rather than guessing hidden properties.
CARD_TEXT_RULE_KINDS = frozenset({"afflictions", "enchantments", "keywords"})
# Keep cycles, oversized descriptions and diagnostic output bounded independently of prompt limits.
MAX_DEPENDENCY_DEPTH = 4
MAX_OMISSION_DETAILS = 64
# Only mechanical glossary terms are expanded from prose; words such as Enemy/Deck are not dependencies.
TEXT_GLOSSARY_IDS = frozenset({"FATAL", "REPLAY", "STUN", "SUMMON", "FORGE", "CHANNELING", "EVOKE", "COOK"})
# Canonical comparison ignores separators, while retaining the original source identifier.
SEPARATORS = re.compile(r"[^A-Z0-9\u0080-\uffff]+")
# Compact power observations have an identifier followed by the current numeric amount.
POWER_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s+(-?\d+)(?:\s|$)")
# These reviewed keyword definitions explain normal hand cleanup and its common exceptions.
HAND_LIFECYCLE_KEYWORDS = ("RETAIN", "ETHEREAL", "EXHAUST")
# Baseline rule references precede entity-specific background without asserting application.
HAND_LIFECYCLE_PRIORITY = -100
# Version two adds persistent hand-lifecycle references to model deliveries.
MODEL_DELIVERY_VERSION = 2


def _key(value: str) -> str:
    """Normalize an entity alias without guessing across different reference kinds."""
    return SEPARATORS.sub("", str(value).upper())


def _dump(value: object) -> str:
    """Use a compact deterministic representation for prompt size accounting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class KnowledgeBase:
    """Load an immutable local snapshot; unavailable references do not stop gameplay."""

    def __init__(self, root: Path, snapshot_id: str | None = None):
        """Resolve the active pointer once and load only that snapshot's frozen corpus."""
        self.root = Path(root).resolve()
        self.snapshot_id = snapshot_id
        self._manifest = {}
        self._entries = []
        self._search_index = []
        self._index = {}
        self._rule_patterns = []
        self._relations = []
        self._dependency_cache = {}
        self.relation_warning = None
        self.warning = None
        self.status = "unavailable"
        try:
            if snapshot_id is None:
                pointer = json.loads((self.root / DEFAULT_POINTER).read_text(encoding="utf-8"))
                self.snapshot_id = pointer["snapshot_id"]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", str(self.snapshot_id)):
                raise ValueError("Invalid knowledge snapshot identifier")
            directory = self.root / "knowledge" / "snapshots" / self.snapshot_id
            self._manifest = json.loads((directory / "sources.lock.json").read_text(encoding="utf-8"))
            corpus = (directory / "entities.jsonl").read_bytes()
            if hashlib.sha256(corpus).hexdigest() != self._manifest["corpus_sha256"]:
                raise ValueError("Knowledge corpus differs from its frozen manifest")
            self._entries = [json.loads(line) for line in corpus.decode("utf-8").splitlines() if line]
            self._search_index = [(entry, _key(" ".join([entry["entity_id"], entry["name"],
                *entry.get("aliases", []), entry.get("description", "")]))) for entry in self._entries]
            for entry in self._entries:
                for alias in (entry["entity_id"], entry["name"], *entry.get("aliases", [])):
                    self._index[(entry["kind"], _key(alias))] = entry
                if (entry["kind"] in RULE_KINDS
                        and (entry["kind"] != "glossary" or entry["entity_id"] in TEXT_GLOSSARY_IDS)):
                    for name in (entry["name"], *entry.get("aliases", [])):
                        # CJK has no word boundaries; Latin names must match whole words.
                        pattern = (re.escape(name) if re.search(r"[\u3400-\u9fff]", name)
                                   else r"(?<![\w])" + re.escape(name) + r"(?![\w])")
                        self._rule_patterns.append((entry["kind"], entry["entity_id"],
                                                    re.compile(pattern, re.IGNORECASE)))
            self.status = "ready"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._entries = []
            self._search_index = []
            self._index = {}
            self._rule_patterns = []
            self.warning = f"Offline knowledge unavailable: {exc}"
        try:
            relation_path = self.root / RELATED_POINTER
            if self.status == "ready" and relation_path.exists():
                relation_data = json.loads(relation_path.read_text(encoding="utf-8"))
                if relation_data.get("snapshot_id") == self.snapshot_id:
                    self._relations = relation_data.get("relations", [])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.relation_warning = f"Optional related references unavailable: {exc}"

    def metadata(self) -> dict:
        """Describe provenance and limitations without including the reference corpus."""
        return {"status": self.status, "snapshot_id": self.snapshot_id,
                "target": deepcopy(self._manifest.get("target", {})),
                "source": deepcopy(self._manifest.get("source", {})),
                "coverage": deepcopy(self._manifest.get("coverage", {})),
                "verification": self._manifest.get("verification"),
                "allowed_uses": ["retrieval", "strategy_reference"],
                "warning": self.warning}

    def lookup(self, kind: str, entity_id: str) -> dict | None:
        """Resolve an exact ID/name alias; compact Power suffixes are explicitly supported."""
        kind = kind if kind.endswith("s") else {"enemy": "monsters", "power": "powers",
            "card": "cards", "event": "events", "relic": "relics", "potion": "potions",
            "orb": "orbs", "character": "characters", "keyword": "keywords"}.get(kind, kind)
        key = _key(entity_id)
        entry = self._index.get((kind, key))
        if entry is None and kind == "powers" and key.endswith("POWER"):
            entry = self._index.get((kind, key[:-5]))
        return deepcopy(entry) if entry else None

    def search(self, query: str = "", kind: str | None = None, limit: int = 20) -> list[dict]:
        """Search the offline public corpus for a human browsing the local dashboard."""
        needle = _key(query)
        matches = []
        for entry, text in self._search_index:
            if kind and entry["kind"] != kind:
                continue
            if not needle or needle in text:
                matches.append(entry)
        matches.sort(key=lambda entry: (_key(entry["entity_id"]) != needle,
                                       entry["kind"], entry["name"]))
        return deepcopy(matches[:max(1, min(int(limit), 100))])

    def lookup_detailed(self, kind: str, entity_id: str, include_related: bool = True,
                        depth: int = 3) -> dict:
        """Return the full frozen entry and optional public dependencies with explicit provenance."""
        entry = self.lookup(kind, entity_id)
        return {"status": "ready" if entry else "not_found", "entry": entry,
                "related": self.search_related(kind, entity_id, depth) if entry and include_related else [],
                "warning": self.relation_warning,
                "usage": "Related entries describe possible mechanics or rewards, not currently applied effects."}

    def search_related(self, kind: str, entity_id: str, depth: int = 3) -> list[dict]:
        """Traverse named public dependencies and reviewed reward chains without reading any run data."""
        root = self.lookup(kind, entity_id)
        if root is None:
            return []
        discovered = {root["fact_id"]: root}
        frontier = [root]
        for _ in range(max(0, min(int(depth), MAX_DEPENDENCY_DEPTH))):
            next_frontier = []
            for reference in frontier:
                def add_related(target_kind, identifier, **_metadata):
                    """Keep each dependency once and retain the edge that first made it relevant."""
                    entry = self.lookup(target_kind, identifier)
                    if entry and entry["fact_id"] not in discovered:
                        entry["related_from"] = reference["fact_id"]
                        evidence = [item for item in self._relations if item.get("from") == reference["fact_id"]
                                    and item.get("to") == entry["fact_id"]]
                        if evidence:
                            entry["relationship_evidence"] = deepcopy(evidence)
                        discovered[entry["fact_id"]] = entry
                        next_frontier.append(entry)

                def named_related(text, kinds, _priority, source, _depth=0, _rank=None):
                    """Reuse the same reviewed names as automatic model delivery."""
                    for target_kind, identifier, pattern in self._rule_patterns:
                        if target_kind in kinds and pattern.search(text):
                            add_related(target_kind, identifier)

                self._add_dependencies(reference, 0, 0, add_related, named_related)
                # Detailed lookup can expand encounter membership without making future Boss tactics resident.
                properties = reference.get("properties", {})
                for monster in properties.get("monsters", []):
                    if isinstance(monster, dict):
                        add_related("monsters", monster.get("id"))
                for card in properties.get("spawns_cards", []) or []:
                    add_related("cards", card.get("id") if isinstance(card, dict) else card)
            frontier = next_frontier
            if not frontier:
                break
        return [deepcopy(value) for key, value in discovered.items() if key != root["fact_id"]]

    def context_for_model(self, observation: dict) -> dict:
        """Keep every public-related rule; the model projection shares redundant metadata."""
        context = self.context(observation, max_entries=None, max_chars=None, include_hand_rules=True)
        context["delivery_version"] = MODEL_DELIVERY_VERSION
        return context

    def context(self, observation: dict, max_entries: int | None = 24,
                max_chars: int | None = 18000, *, include_hand_rules: bool = False) -> dict:
        """Select public-ID references; None budgets retain all related rules for the model."""
        result = {"snapshot_id": self.snapshot_id, "status": self.status,
                  "verification": self._manifest.get("verification"),
                  "usage": "Static reference only, not live state or validated calculator rules. "
                           "Observed card text, costs and Power amounts take precedence. "
                           "Upgrade dictionaries contain source deltas; upgrade_description is the final text. "
                           "Attack patterns describe possible rules, never the hidden current move. "
                           "Conditional/random transitions require public premises; otherwise remain uncertain.",
                  "entries": [], "omitted_related_entries": 0,
                  "omitted_entries": [], "omission_details_truncated": 0,
                  "omitted_fact_ids": [], "omitted_identities_truncated": 0,
                  "unresolved_references": [], "unresolved_reference_count": 0}
        if self.warning:
            result["warning"] = self.warning
            return result
        state = observation.get("state", observation)
        selected = {}
        sources = {}
        depths = {}
        unresolved = {}

        def add(kind: str, identifier: object, amount: object = None, priority: int = 0,
                source: str = "public_id", depth: int = 0, inherited_rank: int | None = None) -> None:
            """Deduplicate one public entity while preserving observed Power counters."""
            if not isinstance(identifier, str):
                return
            entry = self.lookup(kind, identifier)
            if not entry or entry.get("admissibility") != "allowed":
                # ID failures differ from known references dropped for budget; prose is never guessed.
                if source.rsplit(".", 1)[-1] in ID_KINDS or source.endswith(".id") or source.endswith(".powers"):
                    unresolved[(kind, identifier)] = {"kind": kind, "identifier": identifier,
                        "reason": "not_found" if not entry else "not_admissible", "related_from": source}
                return
            fact = entry["fact_id"]
            rank = KIND_PRIORITY.get(kind, 20) + priority if inherited_rank is None else inherited_rank
            if fact not in selected:
                selected[fact] = (rank, entry)
            elif rank < selected[fact][0]:
                selected[fact] = (rank, selected[fact][1])
            if source != fact:
                sources.setdefault(fact, set()).add(source)
            depths[fact] = min(depths.get(fact, depth), depth)
            if amount is not None:
                amounts = selected[fact][1].setdefault("observed_amounts", [])
                if amount not in amounts:
                    amounts.append(amount)

        def named_rules(text: str, kinds: frozenset, priority: int, source: str,
                        depth: int = 0, inherited_rank: int | None = None) -> None:
            """Resolve explicitly named mechanics, without treating their mention as live application."""
            for kind, identifier, pattern in self._rule_patterns:
                if kind in kinds and pattern.search(text):
                    add(kind, identifier, priority=priority, source=source, depth=depth,
                        inherited_rank=inherited_rank)

        def walk(value: object, parent: str = "", priority: int = 0, path: str = "state") -> None:
            """Read only recognized public entity fields and compact display collections."""
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ID_KINDS:
                        for identifier in child if isinstance(child, list) else [child]:
                            add(ID_KINDS[key], identifier, value.get("amount") if key == "power_id" else None,
                                priority, source=path + "." + key)
                    elif key == "id" and parent in {"powers", "orbs", "enchantments", "afflictions"}:
                        add(parent, child, value.get("amount") if parent == "powers" else None, priority,
                            source=path + ".id")
                    elif key == "id" and parent in {"affliction", "enchantment"}:
                        add(parent + "s", child, priority=priority, source=path + ".id")
                    elif key == "id" and parent == "event":
                        add("events", child, priority=priority, source=path + ".id")
                    elif key in {"keywords", "keywords_key"} and isinstance(child, list):
                        for keyword in child:
                            add("keywords", keyword, priority=priority, source=path + "." + key)
                            add("glossary", keyword, priority=priority, source=path + "." + key)
                    elif key == "line" and "card_id" in value and isinstance(child, str):
                        # Older bridges exposed modifiers only in the visible card line.
                        named_rules(child, CARD_TEXT_RULE_KINDS, priority, path + ".line")
                    next_priority = priority + (20 if key in {"deck", "draw", "discard", "exhaust", "piles"} else 0)
                    if key == "hand":
                        next_priority -= 10
                    walk(child, key, next_priority, path + "." + key)
            elif isinstance(value, list):
                for child in value:
                    walk(child, parent, priority, path)
            elif isinstance(value, str) and parent == "powers":
                match = POWER_LINE.match(value)
                if match:
                    add("powers", match[1], int(match[2]), priority, source=path)
                else:
                    add("powers", value, priority=priority, source=path)
            elif isinstance(value, str) and parent in {"relics", "orbs", "character"}:
                add("characters" if parent == "character" else parent, value, priority=priority, source=path)

        walk(state)
        if (include_hand_rules and isinstance(state, dict)
                and isinstance(state.get("combat"), dict)):
            # A missing keyword on an ordinary card cannot retrieve the default hand lifecycle.
            for keyword in HAND_LIFECYCLE_KEYWORDS:
                add("keywords", keyword, source="core_rules.hand_lifecycle",
                    inherited_rank=HAND_LIFECYCLE_PRIORITY)
            result["usage"] += (" Baseline hand-lifecycle keyword references explain possible rules; "
                                "their inclusion does not grant those keywords to any current card.")
        # Traverse rule dependencies to a fixed point within a small depth limit, including Power→Affliction.
        expanded = {}
        while True:
            pending = [(fact, rank, reference) for fact, (rank, reference) in selected.items()
                       if depths[fact] < MAX_DEPENDENCY_DEPTH
                       and expanded.get(fact, (1000, 1000)) > (rank, depths[fact])]
            if not pending:
                break
            for fact, rank, reference in sorted(pending):
                depth = depths[fact]
                expanded[fact] = (rank, depth)
                self._add_dependencies(reference, rank, depth, add, named_rules)

        entries = sorted(selected.values(), key=lambda item: (item[0], item[1]["fact_id"]))
        omitted = []
        # Reserve a small portion for explaining omitted rules rather than silently dropping them.
        diagnostic_reserve = min(2500, max_chars // 4) if max_chars is not None else 0
        entry_budget = max_chars - diagnostic_reserve if max_chars is not None else None
        for rank, entry in entries:
            candidate = deepcopy(entry)
            candidate.pop("aliases", None)
            candidate.pop("sample_description", None)
            if max_entries is None and max_chars is None:
                # Selection evidence explains relevance without implying a referenced rule is active.
                candidate["selection"] = {"priority": rank, "dependency_depth": depths[entry["fact_id"]],
                    "related_from": sorted(sources.get(entry["fact_id"], set()))}
            reason = None
            if max_entries is not None and len(result["entries"]) >= max(0, max_entries):
                reason = "entry_limit"
            else:
                result["entries"].append(candidate)
                if entry_budget is not None and len(_dump(result)) > entry_budget:
                    result["entries"].pop()
                    reason = "character_limit"
            if reason:
                fact = entry["fact_id"]
                omitted.append({"fact_id": fact, "reason": reason,
                                "related_from": sorted(sources[fact])[:2]})
        result["omitted_related_entries"] = len(omitted)
        # Compact identities survive even when there is no room for every diagnostic source path.
        for item in omitted[:MAX_OMISSION_DETAILS]:
            result["omitted_fact_ids"].append(item["fact_id"])
            if max_chars is not None and len(_dump(result)) > max_chars - 8:
                result["omitted_fact_ids"].pop()
                break
        result["omitted_identities_truncated"] = len(omitted) - len(result["omitted_fact_ids"])
        for item in omitted[:MAX_OMISSION_DETAILS]:
            result["omitted_entries"].append(item)
            if max_chars is not None and len(_dump(result)) > max_chars - 8:
                result["omitted_entries"].pop()
                break
        result["omission_details_truncated"] = len(omitted) - len(result["omitted_entries"])
        result["unresolved_reference_count"] = len(unresolved)
        for key in sorted(unresolved):
            if len(result["unresolved_references"]) >= MAX_OMISSION_DETAILS:
                break
            result["unresolved_references"].append(unresolved[key])
            if max_chars is not None and len(_dump(result)) > max_chars - 8:
                result["unresolved_references"].pop()
                break
        return result

    def _add_dependencies(self, reference: dict, rank: int, depth: int, add, named_rules) -> None:
        """Follow explicit source properties and named rules, never live hidden state."""
        source = reference["fact_id"]
        if source not in self._dependency_cache:
            dependencies = []

            def collect(kind, identifier, **_metadata):
                """Cache static edges only; live amounts and selection priority stay per request."""
                if (kind, identifier) not in dependencies:
                    dependencies.append((kind, identifier))

            def collect_names(text, kinds, *_metadata):
                """Resolve frozen reference prose using the same reviewed rule patterns."""
                for kind, identifier, pattern in self._rule_patterns:
                    if kind in kinds and pattern.search(text):
                        collect(kind, identifier)

            self._parse_dependencies(reference, 0, 0, collect, collect_names)
            self._dependency_cache[source] = dependencies
        for kind, identifier in self._dependency_cache[source]:
            add(kind, identifier, source=source, depth=depth + 1, inherited_rank=rank)

    def _parse_dependencies(self, reference: dict, rank: int, depth: int, add, named_rules) -> None:
        """Parse one frozen entity once; dependencies never contain observed amounts."""
        source = reference["fact_id"]
        # Dependencies inherit urgency, so hand modifiers never lose to background relics.
        def related(kind: str, identifier: object) -> None:
            """Keep one dependency in its referring entity's priority group."""
            add(kind, identifier, source=source, depth=depth + 1, inherited_rank=rank)

        properties = reference.get("properties", {})
        rule_text = " ".join(str(value) for value in [reference.get("description", ""),
            reference.get("description_template", ""), properties.get("extra_card_text", ""),
            properties.get("upgrade_description", "")])
        named_rules(rule_text,
                    RULE_KINDS, 0, source, depth + 1, rank)
        for keyword in properties.get("keywords", []):
            related("keywords", keyword)
            related("glossary", keyword)
        for power in properties.get("powers_applied", []):
            related("powers", power.get("power"))
        powers = list(properties.get("innate_powers", []))
        for move in properties.get("moves", []):
            powers.extend(move.get("powers", []))
        for power in powers:
            related("powers", power.get("power_id"))
        # Multiword reward names in source text can resolve without matching ordinary prose words.
        for entry in self._entries:
            if entry["kind"] not in {"cards", "relics"}:
                continue
            name = entry["name"]
            if len(name.split()) > 1:
                pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
            else:
                # One-word names need an explicit grant verb to avoid matching ordinary prose.
                pattern = (r"\b(?:add|obtain|gain|receive)\s+(?:\d+\s+)?(?:an?\s+)?"
                           r"(?:the card\s+)?" + re.escape(name) + r"(?!\w)")
            if re.search(pattern, rule_text, re.IGNORECASE):
                related(entry["kind"], entry["entity_id"])
        for relation in self._relations:
            if relation.get("from") == source:
                target_kind, identifier = relation["to"].split(".", 1)
                related(target_kind, identifier)
        if reference["kind"] == "events":
            # Event option names provide bounded reward links, without asserting random outcomes.
            properties = reference.get("properties", {})
            choices = _dump(properties.get("options", []))
            for entry in self._entries:
                if entry["kind"] not in {"cards", "relics", "potions"}:
                    continue
                name = entry["name"]
                if len(name.split()) < 2:
                    pattern = (r"\b(?:add|obtain|gain|receive)\s+(?:\d+\s+)?(?:an?\s+)?"
                               + re.escape(name) + r"(?!\w)")
                else:
                    pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
                if re.search(pattern, choices, re.IGNORECASE):
                    related(entry["kind"], entry["entity_id"])

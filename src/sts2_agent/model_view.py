"""Project reviewed public payloads into a smaller model-only view.

The full observation/journal remains available for audit; this module only changes
what is placed into the model request. Default is off for backward compatibility;
daily configs opt into "safe".
"""

from copy import deepcopy

# Supported model-view modes.
MODEL_VIEW_MODES = ("off", "safe")

# Controller state that cannot be acted on by the model.
OBSERVATION_STATE_DROP = frozenset({
    "action_readiness", "session", "supported", "protocol_version",
    "observation_schema_version", "glossary", "available_actions",
})

# Per-entry source/selection evidence is audit material, not game rules.
KNOWLEDGE_ENTRY_DROP = frozenset({
    "source", "fact_id", "entity_id", "verification", "admissibility",
    "allowed_uses", "conditions",
})

# Guide attribution is hoisted to the strategy scope once.
STRATEGY_SECTION_DROP = frozenset({
    "source", "id", "guide_id", "guide_title", "tags", "date", "updated",
    "guide_difficulty", "verification", "ascension", "character",
})

# Network/model provenance is audit-only; settings/usage/search budget stay.
ADVICE_PROVENANCE_DROP = frozenset({
    "rule_identity", "source_provenance", "model_identity", "public_state_hash",
    "decision_identity",
})


def _dict(value):
    """Return a dict or an empty one without changing caller data."""
    return value if isinstance(value, dict) else {}


def _list(value):
    """Return a list or an empty one without changing caller data."""
    return value if isinstance(value, list) else []


def _strip_none(value):
    """Drop null object members while preserving array positions."""
    if isinstance(value, dict):
        return {key: _strip_none(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_strip_none(child) for child in value]
    return value


def _knowledge_scope(knowledge):
    """Hoist one immutable source/version/verification scope."""
    entries = _list(knowledge.get("entries"))
    conditions = {}
    source = {}
    for entry in entries:
        entry = _dict(entry)
        if not conditions and isinstance(entry.get("conditions"), dict):
            conditions = entry["conditions"]
        if not source and isinstance(entry.get("source"), dict):
            source = entry["source"]
        if conditions and source:
            break
    scope = {
        "snapshot_id": knowledge.get("snapshot_id"),
        "game_version": conditions.get("game_version"),
        "build_id": conditions.get("build_id"),
        "channel": conditions.get("channel"),
        "ascension": conditions.get("ascension"),
        "source_revision": source.get("revision"),
        "verification": knowledge.get("verification"),
    }
    return {key: value for key, value in scope.items() if value is not None}


def _compact_knowledge(knowledge):
    """Keep rule content, drop per-entry provenance, and add one shared scope."""
    knowledge = deepcopy(_dict(knowledge))
    scope = _knowledge_scope(knowledge)
    entries = []
    for entry in _list(knowledge.get("entries")):
        entry = _dict(entry)
        entries.append({key: deepcopy(value) for key, value in entry.items()
                        if key not in KNOWLEDGE_ENTRY_DROP})
    if "entries" in knowledge:
        knowledge["entries"] = entries
    knowledge["scope"] = scope
    return knowledge


def _strategy_scope(strategy):
    """Keep one guide-corpus scope instead of repeating attribution per section."""
    scope = {
        "source_revision": strategy.get("source_revision"),
        "verification": "unversioned_strategy_reference",
    }
    return {key: value for key, value in scope.items() if value is not None}


def _compact_strategy(strategy):
    """Keep guide prose and introductions; drop repeated per-section metadata."""
    strategy = deepcopy(_dict(strategy))
    sections = []
    for section in _list(strategy.get("sections")):
        section = _dict(section)
        sections.append({key: deepcopy(value) for key, value in section.items()
                         if key not in STRATEGY_SECTION_DROP})
    if "sections" in strategy:
        strategy["sections"] = sections
    strategy["scope"] = _strategy_scope(strategy)
    return strategy


def _dedupe_advice_statistics(advice):
    """Remove exact duplicate evaluation/mcts statistics without guessing unequal values."""
    for name in ("strategies",):
        strategies = _dict(advice.get(name))
        for block in strategies.values():
            block = _dict(block)
            if block.get("evaluation") is not None and block.get("evaluation") == block.get("mcts"):
                block.pop("evaluation", None)
            for alternative in _list(block.get("alternatives")):
                alternative = _dict(alternative)
                if (alternative.get("evaluation") is not None
                        and alternative.get("evaluation") == alternative.get("mcts")):
                    alternative.pop("evaluation", None)
    return advice


def _compact_advice(advice):
    """Keep actionable search advice and semantics; drop network/audit provenance."""
    advice = deepcopy(_dict(advice))
    settings = _dict(advice.get("settings"))
    rule_identity = _dict(advice.get("rule_identity"))
    if not settings and isinstance(rule_identity.get("settings"), dict):
        # Preserve read_access even when the top-level settings block is absent.
        settings = deepcopy(rule_identity["settings"])
    # rule_snapshot identifies the rule set; it is semantic, not network provenance.
    if "rule_snapshot" not in advice and rule_identity.get("rule_snapshot") is not None:
        advice["rule_snapshot"] = deepcopy(rule_identity["rule_snapshot"])
    for key in ADVICE_PROVENANCE_DROP:
        advice.pop(key, None)
    if settings:
        advice["settings"] = settings
    return _dedupe_advice_statistics(advice)


def _compact_observation(observation):
    """Drop controller-only observation fields and exact duplicate pile views."""
    observation = deepcopy(_dict(observation))
    state = _dict(observation.get("state"))
    if state:
        for key in OBSERVATION_STATE_DROP:
            state.pop(key, None)
        run = _dict(state.get("run"))
        combat = _dict(state.get("combat"))
        if combat:
            combat.pop("action_readiness", None)
            state["combat"] = combat
        piles = _dict(run.get("piles"))
        if piles and combat and all(piles.get(zone) == combat.get(zone)
                                    for zone in ("draw", "discard", "exhaust")):
            run.pop("piles", None)
        if run:
            state["run"] = run
        observation["state"] = state
    advice = _dict(observation.get("simulation_advice"))
    if advice:
        observation["simulation_advice"] = _compact_advice(advice)
    return observation


def model_view(payload: object, mode: str = "off") -> object:
    """Return an isolated model-only payload view; never mutate the input."""
    if mode not in MODEL_VIEW_MODES:
        raise ValueError("Unsupported model view mode.")
    if mode == "off" or not isinstance(payload, dict):
        return payload
    result = deepcopy(payload)
    if isinstance(result.get("knowledge"), dict):
        result["knowledge"] = _compact_knowledge(result["knowledge"])
    if isinstance(result.get("strategy"), dict):
        result["strategy"] = _compact_strategy(result["strategy"])
    if isinstance(result.get("observation"), dict):
        result["observation"] = _compact_observation(result["observation"])
    return _strip_none(result)

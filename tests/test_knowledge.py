"""Check offline provenance, public-only selection and dynamic-value boundaries."""

import json
from pathlib import Path

import pytest

from sts2_agent.knowledge import KnowledgeBase


# Repository snapshots are intentionally tested as shipped, including source-build metadata.
ROOT = Path(__file__).resolve().parents[1]


def test_current_snapshot_explains_live_power_without_static_counter_substitution():
    """A current Sandpit amount takes precedence over the wiki's sample two turns."""
    knowledge = KnowledgeBase(ROOT)
    assert knowledge.metadata()["status"] == "ready"
    assert knowledge.metadata()["source"]["build_evidence"]["matched"]
    context = knowledge.context({"state": {"combat": {"player": {"powers": ["SANDPIT_POWER 1"]}}}})
    power = next(entry for entry in context["entries"] if entry["entity_id"] == "SANDPIT")
    assert power["observed_amounts"] == [1]
    assert "eaten and die" in power["description"]
    assert "sample_description" not in power
    assert "calculation" not in power["allowed_uses"]


def test_lookup_aliases_conflicts_and_excluded_tools():
    """Chinese names and compact IDs resolve, while contradictory patterns are withheld."""
    knowledge = KnowledgeBase(ROOT)
    relic = knowledge.lookup("relics", "BURNING_BLOOD")
    assert knowledge.lookup("relic", relic["aliases"][0])["entity_id"] == "BURNING_BLOOD"
    assert knowledge.lookup("power", "SANDPIT_POWER")["entity_id"] == "SANDPIT"
    assert "attack_pattern" not in knowledge.lookup("monsters", "CALCIFIED_CULTIST")["properties"]
    for tool in ("dev-console", "autoslay", "the-deprived"):
        assert knowledge.lookup("mechanics", tool) is None


def test_context_is_bounded_deterministic_and_related_to_public_observation():
    """Reference selection does not scan unrelated raw strings or return the entire corpus."""
    knowledge = KnowledgeBase(ROOT)
    observation = json.loads((ROOT / "tests/fixtures/public/combat.json").read_text(encoding="utf-8"))
    first = knowledge.context(observation, max_entries=4, max_chars=6500)
    assert 0 < len(first["entries"]) <= 4
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":"))) <= 6500
    assert first == knowledge.context(observation, max_entries=4, max_chars=6500)
    assert not knowledge.context({"unrelated": "SANDPIT_POWER 1"})["entries"]
    assert any(entry["entity_id"] == "VULNERABLE" for entry in knowledge.context(observation)["entries"])


def test_event_relic_preview_resolves_named_card_reward():
    """A previewed relic that grants a one-word card must deliver that card's rules."""
    knowledge = KnowledgeBase(ROOT)
    observation = {"state": {"screen": "EVENT", "event": {
        "id": "NONUPEIPE",
        "options": [{"i": 0, "preview_relic_ids": ["JEWELRY_BOX"],
                     "description": "获得1张神化"}]}}}
    context = knowledge.context(observation)
    entries = {entry["fact_id"]: entry for entry in context["entries"]}
    assert "relics.JEWELRY_BOX" in entries
    assert "cards.APOTHEOSIS" in entries
    assert "Innate" in entries["cards.APOTHEOSIS"]["properties"]["keywords"]


def test_snapshot_is_frozen_and_missing_selected_snapshot_does_not_fallback(tmp_path):
    """A process retains its corpus and never substitutes a different snapshot silently."""
    knowledge = KnowledgeBase(ROOT)
    snapshot_id = knowledge.snapshot_id
    assert KnowledgeBase(ROOT, snapshot_id).metadata() == knowledge.metadata()
    unavailable = KnowledgeBase(ROOT, "missing-reviewed-snapshot")
    assert unavailable.context({"power_id": "SANDPIT"})["entries"] == []
    assert unavailable.metadata()["status"] == "unavailable"
    assert unavailable.snapshot_id == "missing-reviewed-snapshot"
    assert KnowledgeBase(tmp_path).metadata()["status"] == "unavailable"


def test_actual_event_id_shape_explains_a_named_reward_and_hand_priority_wins():
    """Bridge event.id resolves, and an earlier deck occurrence cannot demote a hand card."""
    knowledge = KnowledgeBase(ROOT)
    references = knowledge.context({"event": {"id": "BYRDONIS_NEST"}})["entries"]
    assert any(entry["entity_id"] == "BYRDONIS_NEST" for entry in references)
    assert any(entry["entity_id"] == "BYRDONIS_EGG" for entry in references)
    state = {"run": {"deck": [{"card_id": "BASH"}]},
             "combat": {"hand": [{"card_id": "BASH"}, {"card_id": "ANGER"}]}}
    first = knowledge.context(state)["entries"]
    reordered = knowledge.context(dict(reversed(list(state.items()))))["entries"]
    assert first == reordered


@pytest.mark.parametrize("identifier", ["BOUND", "ENTANGLED", "GALVANIZED", "HEXED", "RINGING", "SMOG", "TAINTED"])
def test_all_structured_afflictions_reach_context_before_background(identifier):
    """Every shipped affliction is selected from the bridge's singular overlay shape."""
    knowledge = KnowledgeBase(ROOT)
    context = knowledge.context({"run": {"relic_ids": ["BURNING_BLOOD", "RED_MASK"]},
        "combat": {"hand": [{"card_id": "BASH", "affliction": {"affliction_id": identifier}}]}},
        max_entries=1)
    assert context["entries"][0]["fact_id"] == "afflictions." + identifier
    assert any(item["fact_id"] == "relics.BURNING_BLOOD" and item["reason"] == "entry_limit"
               for item in context["omitted_entries"])


@pytest.mark.parametrize("identifier,name,alias", [
    ("BOUND", "Bound", "魂缚"), ("ENTANGLED", "Entangled", "缠身"),
    ("GALVANIZED", "Galvanized", "流电"), ("HEXED", "Hexed", "邪咒"),
    ("RINGING", "Ringing", "昏眩"), ("SMOG", "Smog", "烟雾"), ("TAINTED", "Tainted", "污染")])
def test_legacy_visible_card_line_recovers_named_rule_without_scanning_unrelated_text(identifier, name, alias):
    """Old public snapshots have visible modifier text even before structured overlays existed."""
    knowledge = KnowledgeBase(ROOT)
    for line in ("Defend: Gain Block. " + name, "防御：获得格挡。 " + alias):
        context = knowledge.context({"combat": {"hand": [{"card_id": "DEFEND_IRONCLAD", "line": line}]}})
        assert "afflictions." + identifier in {item["fact_id"] for item in context["entries"]}
        assert not knowledge.context({"unrelated": line})["entries"]


def test_active_power_dependencies_beat_hand_background_without_legacy_line():
    """A Power that grants an affliction must include that affliction under a tight entry budget."""
    knowledge = KnowledgeBase(ROOT)
    context = knowledge.context({"combat": {"player": {"powers": ["CHAINS_OF_BINDING_POWER 3"]},
        "hand": [{"card_id": "BASH"}, {"card_id": "ANGER"}]}}, max_entries=2)
    assert {item["fact_id"] for item in context["entries"]} == {
        "powers.CHAINS_OF_BINDING", "afflictions.BOUND"}


def test_rule_graph_explains_other_powers_and_multihop_modifier_keywords():
    """Power prose and modifier rules traverse the same bounded dependency graph."""
    knowledge = KnowledgeBase(ROOT)
    context = knowledge.context({"combat": {"player": {"powers": ["GALVANIC_POWER 1", "MASTER_PLANNER_POWER 1"]},
        "hand": [{"card_id": "BASH", "affliction": {"id": "HEXED"},
                  "enchantment": {"enchantment_id": "ROYALLY_APPROVED"}}]}})
    facts = {item["fact_id"] for item in context["entries"]}
    assert {"afflictions.GALVANIZED", "keywords.SLY", "afflictions.HEXED", "keywords.ETHEREAL",
            "enchantments.ROYALLY_APPROVED", "keywords.INNATE", "keywords.RETAIN"} <= facts
    assert not any(fact.startswith("glossary.ROOM_") for fact in facts)


def test_real_queen_snapshot_keeps_restrictions_and_identifies_budget_omissions():
    """The failed run's exact observation now delivers both Chains and Bound within the original budget."""
    knowledge = KnowledgeBase(ROOT)
    fixture = json.loads((ROOT / "tests/fixtures/public/queen-bound-decision.json").read_text(encoding="utf-8"))
    context = knowledge.context(fixture["observation"])
    facts = {item["fact_id"] for item in context["entries"]}
    assert {"powers.CHAINS_OF_BINDING", "afflictions.BOUND"} <= facts
    assert context["entries"][0]["fact_id"] == "afflictions.BOUND"
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 18000
    assert context["omitted_related_entries"] > 0
    assert context["omitted_entries"]
    assert len(context["omitted_fact_ids"]) == context["omitted_related_entries"]
    assert context["omitted_identities_truncated"] == 0
    assert all(item["fact_id"] and item["reason"] in {"character_limit", "entry_limit"}
               and item["related_from"] for item in context["omitted_entries"])
    assert not any(fact.startswith("glossary.ROOM_") for fact in facts)


def test_missing_public_ids_are_reported_separately_from_budget_omissions():
    """An unknown observed ID is a discovery failure rather than a known rule dropped for size."""
    context = KnowledgeBase(ROOT).context({"combat": {"hand": [{"card_id": "NOT_IN_SNAPSHOT"}]}})
    assert context["unresolved_reference_count"] == 1
    assert context["unresolved_references"][0]["identifier"] == "NOT_IN_SNAPSHOT"
    assert context["unresolved_references"][0]["reason"] == "not_found"
    assert context["omitted_related_entries"] == 0


def test_model_context_keeps_all_queen_related_rules_before_prompt_projection():
    """Repeated provenance cannot consume a budget and drop owned equipment or known cards."""
    knowledge = KnowledgeBase(ROOT)
    fixture = json.loads((ROOT / "tests/fixtures/public/queen-bound-decision.json").read_text(encoding="utf-8"))
    limited = knowledge.context(fixture["observation"])
    complete = knowledge.context_for_model(fixture["observation"])
    facts = {entry["fact_id"]: entry for entry in complete["entries"]}
    assert len(facts) > 24
    assert complete["omitted_related_entries"] == 0
    assert complete["omitted_fact_ids"] == []
    assert {entry["fact_id"] for entry in limited["entries"]} <= facts.keys()
    assert set(limited["omitted_fact_ids"]) <= facts.keys()
    assert {"afflictions.BOUND", "powers.CHAINS_OF_BINDING", "relics.BELLOWS",
            "relics.BRILLIANT_SCARF", "cards.BASH"} <= facts.keys()
    for entry in limited["entries"]:
        assert all(facts[entry["fact_id"]][key] == value for key, value in entry.items())
    assert all(entry["selection"]["related_from"] for entry in complete["entries"])
    assert facts["afflictions.BOUND"]["selection"]["priority"] < facts["relics.BELLOWS"]["selection"]["priority"]
    assert "powers.CHAINS_OF_BINDING" in facts["afflictions.BOUND"]["selection"]["related_from"]


def test_model_context_remains_observation_related_and_preserves_live_amounts():
    """Unlimited related context does not inject the corpus or turn possible rules into state."""
    knowledge = KnowledgeBase(ROOT)
    assert knowledge.context_for_model({})["entries"] == []
    observation = {"combat": {"player": {"powers": ["SANDPIT_POWER 1"]}}}
    context = knowledge.context_for_model(observation)
    assert len(context["entries"]) < 10
    sandpit = next(entry for entry in context["entries"] if entry["entity_id"] == "SANDPIT")
    assert sandpit["observed_amounts"] == [1]
    assert "sample_description" not in sandpit
    assert sandpit["selection"]["dependency_depth"] == 0
    assert context == knowledge.context_for_model(observation)
    assert knowledge.context_for_model({"unrelated": "SANDPIT_POWER 1"})["entries"] == []


def test_combat_model_context_always_explains_hand_lifecycle_without_granting_keywords():
    """Ordinary, Retain, Ethereal and Exhaust cards share persistent exception definitions."""
    knowledge = KnowledgeBase(ROOT)
    hand = [{"card_id": "BASH", "keywords": [], "mods": []},
            {"card_id": "BASH", "keywords": ["Retain"], "mods": ["Retain"]},
            {"card_id": "ASCENDERS_BANE", "keywords": ["Ethereal", "Unplayable"]},
            {"card_id": "SLIMED", "keywords": ["Exhaust"]}]
    observation = {"state": {"combat": {"hand": hand}}}
    original = json.loads(json.dumps(observation))
    context = knowledge.context_for_model(observation)
    assert context["delivery_version"] == 2
    facts = {entry["fact_id"]: entry for entry in context["entries"]}
    for keyword in ("RETAIN", "ETHEREAL", "EXHAUST"):
        entry = facts["keywords." + keyword]
        assert "core_rules.hand_lifecycle" in entry["selection"]["related_from"]
        assert entry["admissibility"] == "allowed"
        assert entry["verification"] == "source_build_matched"
        assert entry["source"]["path"] == "data/eng/keywords.json"
    assert "not discarded" in facts["keywords.RETAIN"]["description"]
    assert "end of this turn" in facts["keywords.ETHEREAL"]["description"]
    assert "Removed until the end of combat" in facts["keywords.EXHAUST"]["description"]
    assert "does not grant those keywords" in context["usage"]
    assert observation == original
    assert not any(entry["kind"] == "mechanics" for entry in context["entries"])


def test_basic_hand_rules_apply_only_to_complete_combat_model_context():
    """Empty/menu observations and compatible bounded callers keep their existing selection."""
    knowledge = KnowledgeBase(ROOT)
    assert knowledge.context_for_model({})["entries"] == []
    assert knowledge.context_for_model({"state": {"screen": "MAIN_MENU"}})["entries"] == []
    assert knowledge.context({"state": {"combat": {"hand": []}}})["entries"] == []
    legacy_complete = knowledge.context({"state": {"combat": {"hand": []}}},
                                        max_entries=None, max_chars=None)
    assert legacy_complete["entries"] == []
    assert "delivery_version" not in legacy_complete
    assert "Baseline hand-lifecycle" not in legacy_complete["usage"]
    empty_combat = knowledge.context_for_model({"state": {"combat": {"hand": []}}})
    assert {entry["fact_id"] for entry in empty_combat["entries"]} == {
        "keywords.RETAIN", "keywords.ETHEREAL", "keywords.EXHAUST"}

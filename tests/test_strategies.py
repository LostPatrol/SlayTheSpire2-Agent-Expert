"""Verify decision-specific complete strategy sections and public reward-chain retrieval."""

from pathlib import Path

from sts2_agent.knowledge import KnowledgeBase
from sts2_agent.strategies import StrategyLibrary


# Tests read the same frozen local corpus as the production provider.
ROOT = Path(__file__).resolve().parents[1]


def test_strategy_context_keeps_whole_sections_and_current_character():
    """Reward advice includes skip/size context and the active class without forcing another class."""
    library = StrategyLibrary(ROOT)
    context = library.context_for_model({"state": {"screen": "REWARD",
        "run": {"character": "铁甲战士", "ascension": 0}}})
    sections = context["sections"]
    assert context["status"] == "ready"
    assert any(section["title"] == "When to Skip Cards" for section in sections)
    assert any(section["character"] == "ironclad" for section in sections)
    assert all(section["character"] in {None, "ironclad"} for section in sections)
    for section in sections:
        assert section["markdown"] == library.lookup(section["id"])["markdown"]
        assert section["author"] and section["date"] and section["source"]["revision"]
        assert section["verification"] == "unversioned_strategy_reference"


def test_strategy_context_routes_potions_and_paths_without_fixed_character_or_ascension():
    """Different scenes get relevant sections while future characters and A10 remain valid."""
    library = StrategyLibrary(ROOT)
    combat = library.context_for_model({"state": {"screen": "COMBAT", "run": {"character_id": "SILENT", "ascension": 10}}})
    assert combat["character"] == "silent" and combat["ascension"] == 10
    assert any(section["title"] == "Don't Be Afraid to Use Them" for section in combat["sections"])
    assert all(section["guide_id"] != "ironclad-tier-list" for section in combat["sections"])
    route = library.context_for_model({"state": {"screen": "MAP"}})
    assert any(section["title"] == "Why Pathing Matters" for section in route["sections"])
    assert not library.context_for_model({"state": {"screen": "MAIN_MENU"}})["sections"]
    assert library.search("药水")[0]["markdown"]
    assert library.lookup("ironclad-tier-list")["sections"]


def test_related_egg_event_reaches_hatched_card_with_source_evidence():
    """A detailed event query traverses the egg interaction and its associated reward card."""
    knowledge = KnowledgeBase(ROOT)
    result = knowledge.lookup_detailed("events", "BYRDONIS_NEST")
    related = {entry["fact_id"]: entry for entry in result["related"]}
    assert result["entry"]["fact_id"] == "events.BYRDONIS_NEST"
    assert {"cards.BYRDONIS_EGG", "rest_site_options.HATCH", "relics.BYRDPIP", "cards.BYRD_SWOOP"} <= related.keys()
    assert related["cards.BYRD_SWOOP"]["properties"]["cost"] == 0
    assert related["cards.BYRD_SWOOP"]["properties"]["vars"]["Damage"] == 14
    assert related["relics.BYRDPIP"]["relationship_evidence"][0]["evidence"]["path"]
    assert not knowledge.lookup_detailed("events", "NOT_A_REAL_EVENT")["related"]


def test_detailed_boss_lookup_does_not_make_future_moves_resident():
    """Optional detailed lookup expands monster membership while ordinary context stays scoped."""
    knowledge = KnowledgeBase(ROOT)
    observed = {"state": {"run": {"boss_id": "KNOWLEDGE_DEMON_BOSS"}}}
    resident = knowledge.context_for_model(observed)["entries"]
    assert "monsters.KNOWLEDGE_DEMON" not in {entry["fact_id"] for entry in resident}
    related = knowledge.lookup_detailed("encounters", "KNOWLEDGE_DEMON_BOSS")["related"]
    assert "monsters.KNOWLEDGE_DEMON" in {entry["fact_id"] for entry in related}

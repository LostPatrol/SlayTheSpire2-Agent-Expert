"""Check configured menu alignment without changing an ongoing run or replacing model choices."""

import json

import pytest

from sts2_agent.automatic import automatic_decision
from sts2_agent.public import ActionError, observation, validate_action
from test_provider import build, completion, public_payload


def menu(selected="IRONCLAD", ascension=1):
    """Build a public menu with stable IDs and deliberately non-sequential option handles."""
    return observation({"screen": "CHARACTER_SELECT", "available_actions": [
        "select_character", "increase_ascension", "decrease_ascension", "embark"],
        "character_select": {"selected": selected, "ascension": ascension, "embark": True,
            "characters": [{"i": index * 3 + 2, "character_id": name, "line": name,
                            "selected": name == selected, "locked": False}
                           for index, name in enumerate(("IRONCLAD", "SILENT", "REGENT", "NECROBINDER", "DEFECT"))]}},
        "run", "menu-episode")


def test_default_a0_menu_alignment_is_one_action_per_observation():
    """A persisted A1 menu first decreases, then a new matching observation permits embark."""
    obs = menu()
    assert automatic_decision(obs)["action"] == {"type": "decrease_ascension"}
    with pytest.raises(ActionError, match="Embark requires"):
        validate_action(obs, {"type": "embark"})
    assert automatic_decision(menu(ascension=0))["action"] == {"type": "embark"}


@pytest.mark.parametrize("character,index", [("IRONCLAD", 2), ("SILENT", 5), ("REGENT", 8),
                                             ("NECROBINDER", 11), ("DEFECT", 14)])
def test_character_selection_precedes_target_ascension(character, index):
    """All classes use their visible handles rather than an assumed menu position."""
    config = {"policy": {"character": character, "ascension": 3}}
    obs = menu(selected="UNKNOWN", ascension=0)
    assert automatic_decision(obs, config)["action"] == {"type": "select_character", "option_index": index}
    assert automatic_decision(menu(selected=character, ascension=2), config)["action"] == {"type": "increase_ascension"}
    assert automatic_decision(menu(selected=character, ascension=3), config)["action"] == {"type": "embark"}
    assert automatic_decision(menu(selected=character, ascension=4), config)["action"] == {"type": "decrease_ascension"}


def test_wrong_direction_wrong_character_and_non_menu_actions_are_rejected():
    """Legal native buttons cannot override the configured target or change a running game."""
    policy = {"character": "SILENT", "ascension": 3}
    for obs, action in [
        (menu(selected="SILENT", ascension=3), {"type": "increase_ascension"}),
        (menu(selected="SILENT", ascension=2), {"type": "decrease_ascension"}),
        (menu(selected="IRONCLAD", ascension=2), {"type": "increase_ascension"}),
        (menu(selected="IRONCLAD", ascension=3), {"type": "embark"}),
        (menu(selected="IRONCLAD", ascension=3), {"type": "select_character", "option_index": 2}),
    ]:
        with pytest.raises(ActionError):
            validate_action(obs, action, policy)
    obs = menu(selected="SILENT", ascension=3)
    obs["state"]["screen"] = "COMBAT"
    assert automatic_decision(obs, {"policy": policy}) is None
    with pytest.raises(ActionError, match="before embark"):
        validate_action(obs, {"type": "embark"}, policy)


def test_legacy_names_are_exact_and_locked_or_unknown_targets_do_not_embark():
    """Old localized bridge rows remain usable while missing or disabled targets stay unresolved."""
    obs = menu(selected="SILENT", ascension=0)
    target = obs["state"]["character_select"]["characters"][0]
    target.pop("character_id")
    target["line"] = "铁甲战士"
    assert automatic_decision(obs)["action"] == {"type": "select_character", "option_index": 2}
    target["locked"] = True
    assert automatic_decision(obs) is None
    target.update(locked=False, line="NOT_IRONCLAD")
    assert automatic_decision(obs) is None
    with pytest.raises(ActionError):
        validate_action(menu(selected="NOT_IRONCLAD", ascension=0), {"type": "embark"})


def test_ascension_range_is_integer_zero_through_ten():
    """The highest configured level is reachable, while malformed targets never authorize a step."""
    validate_action(menu(ascension=9), {"type": "increase_ascension"}, {"character": "IRONCLAD", "ascension": 10})
    for target in (-1, 11, True):
        with pytest.raises(ActionError, match="0 through 10"):
            validate_action(menu(), {"type": "decrease_ascension"}, {"character": "IRONCLAD", "ascension": target})


def test_provider_receives_target_even_when_automatic_is_disabled_and_context_resets(monkeypatch):
    """Both fresh and rebuilt requests retain explicit starting constraints for model decisions."""
    decision = {"action": {"type": "increase_ascension"}, "reason": "Match the configured start."}
    provider, requests = build(monkeypatch, [completion(json.dumps(decision)), completion(json.dumps(decision))])
    provider.config["policy"].update(character="DEFECT", ascension=3)
    provider.config["automatic"] = {"enabled": False}
    provider.config["provider"]["context_max_turns"] = 1
    for current in (1, 2):
        obs = menu(selected="DEFECT", ascension=current)
        assert automatic_decision(obs, provider.config) is None
        assert provider.decide(obs, {}) == decision
        payload = public_payload(requests[-1]["messages"])
        assert payload["configured_start"] == {"character": "DEFECT", "ascension": 3}
        assert "embark only when both match" in requests[-1]["messages"][0]["content"]

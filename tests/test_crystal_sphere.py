"""Validate reviewed sphere controls without choosing hidden rewards or simulating a real click."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from sts2_agent.provider import DECISION_SCHEMA
from sts2_agent.public import ActionError, observation, validate_action


def sphere():
    """Create a public covered board without hidden item layout."""
    return observation({"screen": "CRYSTAL_SPHERE", "available_actions": [
        "crystal_set_tool", "crystal_clear_cell", "proceed"], "crystal_sphere": {
        "divinations_left": 3, "tool": "small", "is_finished": False,
        "grid_width": 6, "grid_height": 5, "hidden_cells": [[0, 0], [2, 3]], "items": []}}, "run", "episode")


def test_native_sphere_parameters_reach_model_schema_and_validator():
    """Coordinate and tool fields survive the ordinary single-action contract."""
    obs = sphere()
    for action in [{"type": "crystal_set_tool", "tool": "big"},
                   {"type": "crystal_clear_cell", "x": 2, "y": 3},
                   {"type": "crystal_clear_cell", "x": 2, "y": 3, "tool": "small"},
                   {"type": "proceed"}]:
        validate_action(obs, action)
        assert Draft202012Validator(DECISION_SCHEMA).is_valid({"action": action, "reason": "Test"})
    assert obs["action_parameters"]["crystal_set_tool"] == {"tool": "big or small"}


@pytest.mark.parametrize("action", [
    {"type": "crystal_set_tool"}, {"type": "crystal_set_tool", "tool": "reveal"},
    {"type": "crystal_clear_cell", "x": 6, "y": 0},
    {"type": "crystal_clear_cell", "x": 0, "y": -1},
    {"type": "crystal_clear_cell", "x": True, "y": 0},
    {"type": "crystal_clear_cell", "x": 0},
    {"type": "crystal_clear_cell", "x": 0, "y": 0, "target_index": 0},
])
def test_sphere_rejects_invalid_parameters(action):
    """Never send guessed coordinates, unknown tools or unrelated indices to the game."""
    with pytest.raises(ActionError):
        validate_action(sphere(), action)


def test_finished_sphere_and_exhausted_divinations_do_not_accept_clears():
    """Stale board state cannot grant another divination."""
    obs = sphere()
    obs["state"]["crystal_sphere"]["divinations_left"] = 0
    with pytest.raises(ActionError):
        validate_action(obs, {"type": "crystal_clear_cell", "x": 0, "y": 0})
    obs["state"]["crystal_sphere"]["is_finished"] = True
    with pytest.raises(ActionError):
        validate_action(obs, {"type": "crystal_set_tool", "tool": "big"})
    plain = deepcopy(obs)
    plain["state"]["screen"] = "MAP"
    with pytest.raises(ActionError):
        validate_action(plain, {"type": "crystal_clear_cell", "x": 0, "y": 0})

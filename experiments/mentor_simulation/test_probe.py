"""Check the pure Mentor probe's rule execution and preserved choice continuations."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

# The probe is built outside source directories and never loads a real game process.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Allow a fresh build to be tested without replacing an active deployment.
HOST_EXE = Path(os.environ.get("MENTOR_PRODUCTION_EXE", PROJECT_ROOT / "local/simulation/mentor/mentor-simulation.exe"))


def run_probe(hand, actions, **scenario):
    """Execute a declared synthetic plan through the real compiled JSONL process."""
    request = {"scenario": {"hand": [{"id": name} for name in hand], **scenario}, "actions": actions}
    process = subprocess.run([str(HOST_EXE)], input=json.dumps(request) + "\n", capture_output=True,
                             text=True, encoding="utf-8", cwd=PROJECT_ROOT, timeout=20)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["ok"], result
    return result


def play(index, target=None):
    """Construct one currently indexed synthetic card action."""
    return {"type": "play_card", "card_index": index, "target_index": target}


def choose(index):
    """Answer the exact surfaced single-card choice without a default selection."""
    return {"type": "select_card", "choice_indices": [index]}


def test_base_card_rules():
    """The pinned default game build spends one energy and deals canonical Strike damage."""
    result = run_probe(["STRIKE_IRONCLAD"], [play(0, 0)])
    assert result["state"]["player"]["energy"] == 9
    assert result["state"]["enemies"][0]["hp"] == 94
    assert result["state"]["piles"]["discard"] == 1


def test_draw_is_real_rule_execution():
    """Shrug gains its block and draws a card while keeping sampled pile order private."""
    result = run_probe(["SHRUG_IT_OFF"], [play(0)], draw=[{"id": "STRIKE_IRONCLAD"}])
    assert result["state"]["player"]["block"] == 8
    assert result["state"]["hand"][0]["id"] == "STRIKE_IRONCLAD"
    assert result["state"]["piles"]["draw"] == 0


def test_armaments_preserves_play_tail_and_choice():
    """Armaments stays in Play until its chosen upgrade completes, then disposes once."""
    result = run_probe(["ARMAMENTS", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"],
                       [play(0), choose(0), play(0, 0)])
    first, second, _ = result["events"]
    assert first["result"] == "PendingChoice"
    assert first["state"]["pending_play"] is True
    assert first["state"]["piles"]["play"] == 1
    assert first["state"]["piles"]["discard"] == 0
    assert second["state"]["pending_play"] is False
    assert second["state"]["piles"]["play"] == 0
    assert result["state"]["enemies"][0]["hp"] == 91


def test_burning_pact_selection_can_cross_reshuffle():
    """Pact's deferred draw reshuffles existing discard without drawing the in-flight Pact."""
    result = run_probe(["BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
                       [play(0), choose(0)], discard=[{"id": "ANGER"}])
    first = result["events"][0]["state"]
    assert first["pending_play"] is True
    assert first["piles"] == {"draw": 0, "discard": 1, "exhaust": 0, "play": 1}
    assert result["state"]["pending_play"] is False
    assert [card["id"] for card in result["state"]["hand"]] == ["STRIKE_IRONCLAD", "ANGER"]
    assert result["state"]["piles"] == {"draw": 0, "discard": 1, "exhaust": 1, "play": 0}


def test_corruption_pact_continuation_exhausts_source_after_selected_draw():
    """Corruption routes the source to Exhaust only after Pact's awaited selection finishes."""
    result = run_probe(["CORRUPTION", "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
                       [play(0), play(0), choose(0)], discard=[{"id": "ANGER"}])
    pending = result["events"][1]["state"]
    assert pending["pending_play"] is True
    assert pending["piles"]["exhaust"] == 0
    assert result["state"]["pending_play"] is False
    assert result["state"]["piles"]["exhaust"] == 2
    assert result["state"]["piles"]["play"] == 0
    assert result["state"]["player"]["energy"] == 7


def test_dark_embrace_keeps_native_public_identity_in_hand_and_choice():
    """The verified registry alias must not become a different card in public snapshots."""
    result = run_probe(["CORRUPTION", "DARK_EMBRACE", "BURNING_PACT", "DEFEND_IRONCLAD"],
                       [play(0)])
    assert result["state"]["hand"][0]["id"] == "DARK_EMBRACE"
    pending = run_probe(["BURNING_PACT", "DARK_EMBRACE", "DEFEND_IRONCLAD"], [play(0)])
    assert pending["state"]["pending_choice"]["cards"][0]["id"] == "DARK_EMBRACE"


def test_omitted_choice_remains_pending():
    """A missing plan decision cannot be replaced by first-option resolution."""
    result = run_probe(["BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"], [play(0)])
    assert result["state"]["pending_choice"] is not None
    assert result["state"]["pending_play"] is True
    assert result["state"]["piles"]["exhaust"] == 0


def test_duplicator_pact_surfaces_a_second_consequential_choice():
    """A duplicated Pact requires both explicit choices before disposing its single source."""
    hand = ["BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"]
    prefix = [{"type": "use_potion", "potion_index": 0, "target_index": None}, play(0), choose(0)]
    scenario = {"potions": ["DUPLICATOR"], "draw": [{"id": "ANGER"}] * 6}
    waiting = run_probe(hand, prefix, **scenario)["state"]
    assert waiting["pending_choice"] is not None
    assert waiting["pending_play"] is True
    assert waiting["piles"] == {"draw": 4, "discard": 0, "exhaust": 1, "play": 1}
    assert waiting["player"]["card_plays_finished_this_turn"] == 1
    # Pick the first drawn Anger, rather than silently discarding the first Strike.
    result = run_probe(hand, prefix + [choose(2)], **scenario)["state"]
    assert result["pending_choice"] is None
    assert result["pending_play"] is False
    assert result["piles"] == {"draw": 2, "discard": 1, "exhaust": 2, "play": 0}
    assert result["player"]["energy"] == 9
    assert result["player"]["card_plays_finished_this_turn"] == 2
    assert sum(card["id"] == "STRIKE_IRONCLAD" for card in result["hand"]) == 2


def test_duplicator_pact_exhaust_hooks_fire_once_per_explicit_selection():
    """Resuming a replay cannot repeat the first play's exhaust or after-play effects."""
    result = run_probe(["FEEL_NO_PAIN", "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
                       [play(0), {"type": "use_potion", "potion_index": 0, "target_index": None},
                        play(0), choose(0), choose(0)], potions=["DUPLICATOR"],
                       draw=[{"id": "ANGER"}] * 6)
    assert result["events"][3]["state"]["player"]["block"] == 3
    assert result["state"]["player"]["block"] == 6
    assert result["state"]["player"]["card_plays_finished_this_turn"] == 3
    assert result["state"]["piles"]["exhaust"] == 2


def test_strength_potion_applies_once_and_consumes_slot():
    """The content potion registry participates in subsequent card damage and belt state."""
    result = run_probe(["STRIKE_IRONCLAD"], [{"type": "use_potion", "potion_index": 0,
                        "target_index": None}, play(0, 0)], potions=["STRENGTH_POTION"])
    assert result["state"]["potion_count"] == 0
    assert result["state"]["enemies"][0]["hp"] == 92


@pytest.mark.parametrize("potion,remaining,power", [
    ("FIRE_POTION", 74, None),
    ("VULNERABLE_POTION", 91, {"id": "VULNERABLE", "amount": 3}),
    ("WEAK_POTION", 94, {"id": "WEAK", "amount": 3}),
    ("POISON_POTION", 94, {"id": "POISON", "amount": 6}),
])
def test_targeted_potions_accept_explicit_valid_enemy(potion, remaining, power):
    """Slot-only upstream enumeration must not reject apply's supported explicit targets."""
    result = run_probe(["STRIKE_IRONCLAD"], [{"type": "use_potion", "potion_index": 0,
                        "target_index": 0}, play(0, 0)], potions=[potion])
    assert result["state"]["enemies"][0]["hp"] == remaining
    assert result["state"]["potion_count"] == 0
    if power:
        assert power in result["state"]["enemies"][0]["powers"]


def test_targeted_potion_invalid_enemy_is_rejected_before_consumption():
    """Expanding valid targets must not permit a missing enemy slot."""
    with pytest.raises(AssertionError, match="action_not_legal"):
        run_probe(["STRIKE_IRONCLAD"], [{"type": "use_potion", "potion_index": 0,
                  "target_index": 1}], potions=["FIRE_POTION"])


def test_unknown_id_is_rejected_without_placeholder():
    """Unresolved content is an explicit failure rather than a different card."""
    with pytest.raises(AssertionError, match="unknown_card_id"):
        run_probe(["MISSING_CARD"], [])


@pytest.mark.parametrize("name", ["ECHO_FORM", "ECHO_FORM_CARD"])
def test_known_unverified_echo_replay_path_is_explicitly_unsupported(name):
    """The verified manual replay repair cannot imply support for the separate Echo loop."""
    with pytest.raises(AssertionError, match="unsupported_echo_form_replay_choice_path"):
        run_probe([name, "BURNING_PACT", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD"], [])


def test_unknown_draw_multiset_is_invariant_to_input_order():
    """Canonical allocation before blindfold prevents input order from carrying hidden identity."""
    draw = [{"id": "STRIKE_IRONCLAD"}, {"id": "DEFEND_IRONCLAD"}, {"id": "ANGER"}]
    first = run_probe(["SHRUG_IT_OFF"], [play(0)], draw=draw)
    second = run_probe(["SHRUG_IT_OFF"], [play(0)], draw=list(reversed(deepcopy(draw))))
    assert first == second

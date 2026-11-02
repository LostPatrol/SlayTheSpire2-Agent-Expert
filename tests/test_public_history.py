"""Check that replay history distinguishes proven facts from unobserved actions."""

from copy import deepcopy

from sts2_agent.public_history import PublicCombatHistory, observation_coverage


def frame(revision=4):
    """Create one stable public combat boundary with a held Permafrost."""
    return {"screen": "COMBAT", "turn": 1, "action_revision": revision,
            "run": {"relic_ids": ["PERMAFROST"]},
            "combat": {"player": {"cards_played_this_turn": 0},
                       "hand": [{"i": 3, "card_id": "INFLAME", "card_type": "Power"}],
                       "action_readiness": {field: True for field in (
                           "can_use_combat_actions", "actions_settled", "snapshot_stable")}}}


def test_unknown_start_and_confirmed_power_survive_restart_without_double_counting():
    """Zero manual dispatches cannot prove an unused latch; one settled Power can prove use."""
    tracker = PublicCombatHistory()
    before = frame()
    assert tracker.observe(before, "combat-1", (10, "bridge"))["permafrost_first_power_used"] is None
    command = {"action_id": "a1", "action": {"type": "play_card", "card_index": 3},
               "observation": {"state": before}}
    receipt = {"action_id": "a1", "status": "completed"}
    successor = {"state": frame(5)}
    result = tracker.completed(command, receipt, successor)
    assert result["permafrost_first_power_used"] is True
    restored = PublicCombatHistory(tracker.data)
    assert restored.completed(command, receipt, successor)["observed_manual_power_plays"] == 1
    assert restored.observe(frame(5), "combat-1", (10, "bridge"))["permafrost_first_power_used"] is True
    assert restored.observe(frame(5), "combat-1", (11, "new-bridge"))["permafrost_first_power_used"] is None


def test_complete_native_history_counts_manual_series_and_includes_auto_powers():
    """Manual series, total completions and Scarf's non-auto completions have distinct semantics."""
    tracker = PublicCombatHistory()
    state = frame()
    history = {"available": True, "complete": True, "scope": "current_combat", "plays": []}
    state["combat"]["public_card_play_history"] = history
    assert tracker.observe(state, "combat-1", (10, "bridge"))["permafrost_first_power_used"] is False
    history["plays"] = [
        {"card_type": "Attack", "auto_played": False, "first_in_series": True, "this_turn": True},
        {"card_type": "Attack", "auto_played": False, "first_in_series": False, "this_turn": True},
        {"card_type": "Power", "auto_played": True, "first_in_series": True, "this_turn": True},
        {"card_type": "Skill", "auto_played": False, "first_in_series": True, "this_turn": False}]
    result = tracker.observe(state, "combat-1", (10, "bridge"))
    assert result["permafrost_first_power_used"] is True
    assert result["manual_card_plays_this_turn"] == result["manual_attack_plays_this_turn"] == 1
    assert result["manual_skill_plays_this_turn"] == 0
    assert result["total_card_plays_this_turn"] == 3
    assert result["total_attack_plays_this_turn"] == 2
    assert result["total_skill_plays_this_turn"] == 0
    assert result["non_auto_card_plays_this_turn"] == 2
    del history["plays"][0]["first_in_series"]
    assert tracker.observe(state, "combat-1", (10, "bridge"))["manual_card_plays_this_turn"] is None
    assert tracker.snapshot()["non_auto_card_plays_this_turn"] == 2
    del history["plays"][0]["auto_played"]
    assert tracker.observe(state, "combat-1", (10, "bridge"))["non_auto_card_plays_this_turn"] is None
    fresh = PublicCombatHistory()
    history.update(complete=False, plays=[])
    assert fresh.observe(state, "combat-1", (10, "bridge"))["permafrost_first_power_used"] is None


def test_unready_or_unrelated_successor_cannot_establish_a_public_fact():
    """Pending or mismatched native outcomes leave the history unknown."""
    tracker = PublicCombatHistory()
    before = frame()
    tracker.observe(before, "combat-1", (10, "bridge"))
    command = {"action_id": "a1", "action": {"type": "play_card", "card_index": 3},
               "observation": {"state": before}}
    for revision, action_id, ready in [(6, "a1", True), (5, "other", True), (5, "a1", False)]:
        after = frame(revision)
        after["combat"]["action_readiness"]["snapshot_stable"] = ready
        assert tracker.completed(command, {"action_id": action_id, "status": "completed"},
                                 {"state": after})["permafrost_first_power_used"] is None


def test_coverage_preserves_original_fields_and_reports_old_modifier_gaps():
    """Old card labels remain available while absent structured parameters are explicit."""
    state = frame()
    state["combat"]["hand"][0]["mods"] = ["Enchantment", "锋利"]
    original = deepcopy(state)
    coverage = observation_coverage(state)
    assert coverage["status"] == "partial"
    assert any(row["reason"] == "visible_enchantment_without_structured_record" for row in coverage["gaps"])
    assert any(row["path"] == "combat.public_card_play_history" for row in coverage["gaps"])
    assert state == original


def test_enemy_telegraph_history_preserves_instance_identity_across_slot_changes():
    """A surviving creature keeps its own visible history after another enemy is removed."""
    tracker = PublicCombatHistory()
    state = frame()
    state["combat"]["enemies"] = [
        {"i": 0, "instance_id": "enemy-1", "enemy_id": "NIBBIT", "intents": [{"intent_type": "Buff"}]},
        {"i": 1, "instance_id": "enemy-2", "enemy_id": "NIBBIT", "intents": [{"intent_type": "Attack", "damage": 12}]}]
    tracker.observe(state, "encounter", (10, "bridge"))
    state["combat"]["enemies"][1]["i"] = 0
    state["combat"]["enemies"] = state["combat"]["enemies"][1:]
    tracker.observe(state, "encounter", (10, "bridge"))
    state["turn"] = 2
    result = tracker.observe(state, "encounter", (10, "bridge"))
    assert len(result["enemy_turns"]) == 2
    assert result["enemy_turns"][0]["enemies"][0]["instance_id"] == "enemy-2"
    assert result["enemy_turns"][1]["enemies"][0]["instance_id"] == "enemy-2"
    assert result["enemy_history_complete_from_start"] is True
    restored = PublicCombatHistory(tracker.data)
    assert restored.snapshot()["enemy_turns"] == result["enemy_turns"]
    assert restored.observe(state, "different-encounter", (10, "bridge"))["enemy_history_complete_from_start"] is False


def choice_frame(revision):
    """Represent the real selection indices and explicit combat modal origin of a pending play."""
    state = frame(revision)
    state.update(screen="CARD_SELECTION", selection={"kind": "combat_hand_select", "underlying_screen": "COMBAT",
        "min": 1, "max": 1, "selected": 0, "confirm": False, "requires_confirmation": True,
        "cards": [{"i": 7, "card_id": "STRIKE_IRONCLAD", "selected": False}]})
    state["combat"]["action_readiness"]["can_use_combat_actions"] = False
    return state


def test_combat_selection_origin_keeps_routing_without_reconstructing_engine_history():
    """Public history stores room routing; pending effects belong to the original mirror."""
    tracker = PublicCombatHistory()
    before = frame()
    history = tracker.observe(before, "encounter-1", (10, "bridge"))
    before_original = deepcopy(before)
    opening = {"action_id": "open", "action": {"type": "play_card", "card_index": 3},
               "observation": {"state": before, "public_history": {**history, "combat_selection_origin": {"old": "must-not-nest"}}}}
    after = choice_frame(5)
    tracker.observe(after, "encounter-1", (10, "bridge"))
    opened = tracker.completed(opening, {"action_id": "open", "status": "completed"}, {"state": after})
    origin = opened["combat_selection_origin"]
    assert origin["encounter_id"] == "encounter-1"
    assert origin["source_action_id"] == "open"
    assert set(origin["before"]) == {"run"}
    assert "actions" not in origin
    assert before == before_original
    restored = PublicCombatHistory(tracker.data)
    assert restored.observe(after, "encounter-1", (10, "bridge"))["combat_selection_origin"] == origin
    selecting = {"action_id": "select", "action": {"type": "select_deck_card", "option_index": 7},
                 "observation": {"state": after, "public_history": opened}}
    confirm = choice_frame(6)
    confirm["selection"]["cards"][0]["selected"] = True
    confirm["selection"].update(selected=1, confirm=True)
    restored.observe(confirm, "encounter-1", (10, "bridge"))
    selected = restored.completed(selecting, {"action_id": "select", "status": "completed"}, {"state": confirm})
    assert selected["combat_selection_origin"] == origin
    assert restored.completed(selecting, {"action_id": "select", "status": "completed"},
                              {"state": confirm})["combat_selection_origin"] == origin
    assert "combat_selection_origin" not in restored.observe(frame(7), "encounter-1", (10, "bridge"))


def test_unwitnessed_or_unrelated_selection_cannot_create_a_combat_origin():
    """An existing choice, wrong receipt, skipped revision or run reward cannot invent an opening card."""
    tracker = PublicCombatHistory()
    before, after = frame(), choice_frame(5)
    tracker.observe(before, "encounter-1", (10, "bridge"))
    command = {"action_id": "open", "action": {"type": "play_card", "card_index": 3}, "observation": {"state": before}}
    assert "combat_selection_origin" not in tracker.observe(after, "encounter-1", (10, "bridge"))
    assert "combat_selection_origin" not in tracker.completed(command, {"action_id": "wrong", "status": "completed"}, {"state": after})
    skipped = choice_frame(7)
    assert "combat_selection_origin" not in tracker.completed(command, {"action_id": "open", "status": "completed"}, {"state": skipped})
    reward = deepcopy(after)
    reward["selection"]["underlying_screen"] = "REWARD"
    reward["modal"] = {"underlying_screen": "COMBAT"}
    assert "combat_selection_origin" not in tracker.completed(command, {"action_id": "open", "status": "completed"}, {"state": reward})
    generic_modal = deepcopy(after)
    generic_modal.update(screen="MODAL", selection={})
    assert "combat_selection_origin" not in tracker.completed(command, {"action_id": "open", "status": "completed"}, {"state": generic_modal})
    tracker.completed(command, {"action_id": "open", "status": "completed"}, {"state": after})
    assert "combat_selection_origin" not in tracker.observe(after, "different-encounter", (10, "bridge"))



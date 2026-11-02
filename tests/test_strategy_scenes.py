"""Verify scene-scoped tactics, automatic receipts, and durable tool observations."""

from sts2_agent.memory import PublicMemory
from test_session_controls import make_session


def frame(screen, floor=21, act="1"):
    """Make a public room identity without coupling tests to native game internals."""
    return {"state": {"screen": screen, "run": {"act_id": act, "floor": floor}}}


def test_room_tactics_expire_but_build_and_selection_continuation_survive():
    """A choice overlay stays in combat; the following shop cannot inherit its target."""
    memory = PublicMemory("test")
    assert memory.sync_scene(frame("COMBAT"))
    memory.set_plan("Need reliable block and draw")
    memory.set_scene_plan("Attack Glooper")
    assert not memory.sync_scene(frame("CARD_SELECTION"))
    assert memory.scene_plan == "Attack Glooper"
    assert memory.sync_scene(frame("SHOP", 22))
    assert memory.scene_plan == ""
    assert memory.context()["plan"] == "Need reliable block and draw"


def test_scene_scope_restores_without_reviving_an_old_room(tmp_path):
    """A restored tactic is kept only while its bound public room still matches."""
    session, _, _ = make_session(tmp_path)
    # The core lifecycle is exercised directly; no model or game action is needed here.
    session.memory.sync_scene(frame("COMBAT", 23))
    session.memory.set_scene_plan("Block current attack")
    session._persist()
    restored, _, _ = make_session(session.root)
    assert restored.memory.scene_plan == "Block current attack"
    restored.memory.sync_scene(frame("REWARD", 23))
    assert restored.memory.scene_plan == ""


def test_automatic_action_uses_receipt_chain_without_model(tmp_path):
    """A forced end turn is still dispatched once and counted only after settlement."""
    session, bridge, provider = make_session(tmp_path)
    session.config["automatic"] = {"enabled": True}
    bridge.state["run"] = {"potions": []}
    bridge.state["combat"]["player"]["cards_played_this_turn"] = 1
    bridge.state["combat"]["action_readiness"] = {
        "can_use_combat_actions": True, "snapshot_stable": True, "actions_settled": True}
    session.command("step-auto", "step")
    session._tick()
    assert not provider.calls
    assert len(bridge.executions) == 1
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    assert session.snapshot()["decision"]["decision_source"] == "automatic"


def test_explicit_empty_plans_clear_previous_values_after_acceptance(tmp_path):
    """Empty model fields clear plans; omitted fields retain their respective values."""
    session, _, provider = make_session(tmp_path)
    session.memory.set_plan("Old build")
    session.memory.set_scene_plan("Old tactic")
    original = provider.decide

    def decide(*args, **kwargs):
        """Retain the fake request accounting while returning explicit clears."""
        return {**original(*args, **kwargs), "build_plan": "", "scene_plan": ""}

    provider.decide = decide
    session.command("clear-plan", "step")
    session._tick()
    assert session.memory.context()["plan"] == ""
    assert session.memory.scene_plan == ""


def test_tool_result_is_persisted_and_traced_without_a_game_action(tmp_path):
    """Tool arguments/results stay observable after refresh without native side effects."""
    session, bridge, provider = make_session(tmp_path)
    provider.event_context = {}
    payload = {"tool_call_id": "t1", "name": "wiki_lookup", "arguments": {"entity_id": "BASH"},
               "status": "running"}
    session._provider_event("tool_call_started", payload)
    session._provider_event("tool_call_completed", {**payload, "status": "completed",
        "elapsed_ms": 1, "result": {"name": "Bash"}})
    restored, _, _ = make_session(tmp_path)
    assert restored.snapshot()["tool_calls"][0]["result"] == {"name": "Bash"}
    assert not bridge.executions


def combat_frame(encounter, floor=21, act='1', screen='COMBAT'):
    """Make a public combat identity without coupling tests to native game internals."""
    observation = frame(screen, floor, act)
    observation["public_history"] = {"encounter_id": encounter}
    return observation


def test_conversation_bounds_only_at_a_new_fight_or_act():
    """Map, shop and reward share one conversation; a new encounter or act starts a new one."""
    memory = PublicMemory("test")
    first = memory.conversation_scene(combat_frame("fight-a", 21))
    between = memory.conversation_scene(frame("MAP", 22))
    shop = memory.conversation_scene(frame("SHOP", 22))
    second = memory.conversation_scene(combat_frame("fight-b", 23))
    overlay = memory.conversation_scene(combat_frame("fight-b", 23, screen="CARD_SELECTION"))
    next_act = memory.conversation_scene(frame("MAP", 1, "2"))
    assert first == "1:combat:fight-a"
    assert between == shop == "1:between"
    assert second == "1:combat:fight-b"
    assert overlay == second
    assert next_act == "2:between"
    assert len({first, between, second, next_act}) == 4
    assert memory.conversation_scene({"state": {"screen": "COMBAT"}}) == ""


def test_only_a_new_fight_or_act_starts_a_new_conversation():
    """Leaving combat keeps the fight available to reward and map decisions."""
    assert PublicMemory.starts_new_conversation("0:combat:fight-b", "0:between")
    assert PublicMemory.starts_new_conversation("0:combat:fight-b", "0:combat:fight-a")
    assert PublicMemory.starts_new_conversation("1:between", "0:combat:fight-b")
    assert not PublicMemory.starts_new_conversation("0:between", "0:combat:fight-b")
    assert not PublicMemory.starts_new_conversation("0:between", "0:between")

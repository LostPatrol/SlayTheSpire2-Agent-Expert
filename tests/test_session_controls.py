"""Exercise real dashboard session races with offline public bridge/provider doubles."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from sts2_agent.session import DashboardSession, SessionError
from sts2_agent.provider import ProviderCancelled
from sts2_agent.telemetry import WriterLock, read_jsonl


class FakeBridge:
    """Expose controllable public frames and receipts without native game access."""

    def __init__(self):
        """Start at one legal public combat decision point."""
        self.identity = (123, "offline-instance")
        self.state = {
            "protocol_version": 1, "supported": True, "screen": "COMBAT", "turn": 1,
            "available_actions": ["end_turn"],
            "combat": {"player": {"energy": 3, "hp": "80/80"},
                       "enemies": [{"i": 0, "alive": True, "hp": "20/20"}], "hand": []},
        }
        self.token = "frame-one"
        self.execute_status = "completed"
        self.receipt_status = "unknown"
        self.executions = []
        self.receipt_reads = []
        self._advanced = set()
        self.block_execute = False
        self.executing = threading.Event()
        self.finish_execution = threading.Event()

    def health(self):
        """Return only the stable reviewed connection summary."""
        return {"process_id": 123, "game_version": "v0.107.1", "status": "healthy"}

    def snapshot(self):
        """Return a detached current public state and its atomic observation token."""
        return deepcopy(self.state), self.token

    def settled(self):
        """Make test successor frames immediately settled."""
        return self.snapshot()

    def _advance(self, action_id):
        """Advance each accepted native action once, even when its receipt is reread."""
        if action_id not in self._advanced:
            self._advanced.add(action_id)
            self.state["turn"] += 1
            self.token = f"frame-{self.state['turn']}"

    def execute(self, action, action_id, token):
        """Record one native POST and optionally expose its completed successor."""
        assert token == self.token
        self.executions.append({"action": deepcopy(action), "action_id": action_id, "token": token})
        if self.block_execute:
            self.executing.set()
            assert self.finish_execution.wait(3), "Test did not release the native action."
        if self.execute_status == "completed":
            self._advance(action_id)
        return {"status": self.execute_status, "action_id": action_id}

    def receipt(self, action_id):
        """Reconcile by GET without adding an execution POST."""
        self.receipt_reads.append(action_id)
        if self.receipt_status == "completed":
            self._advance(action_id)
        return {"status": self.receipt_status, "action_id": action_id}


class FakeProvider:
    """Emit actual flat request-event shapes and optionally block the first response."""

    def __init__(self, *, block=False, attempts=1):
        """Prepare bounded synchronization points and cumulative request counters."""
        self.requests_used = 0
        self.event_sink = None
        self.event_context = {}
        self.calls = []
        self.resets = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.attempts = attempts

    def reset_context(self, reason):
        """Track resets without changing cumulative spend."""
        self.resets.append(reason)

    def cancel(self, cancel_event):
        """Wake the blocked transport as production cancellation does."""
        cancel_event.set()
        self.release.set()

    def decide(self, obs, memory, *, instructions=None, cancel_event=None):
        """Return one legal action after zero or more in-decision request retries."""
        self.calls.append({"observation": deepcopy(obs), "instructions": deepcopy(instructions),
                           "context": deepcopy(self.event_context)})
        for attempt in range(self.attempts):
            self.requests_used += 1
            request_id = f"request-{self.requests_used}"
            payload = {**self.event_context, "model_request_id": request_id,
                       "attempt_index": attempt, "format_repair": False}
            self.event_sink("model_request_started", payload)
            if self.block and len(self.calls) == 1 and attempt == 0:
                self.started.set()
                assert self.release.wait(3), "Test did not release the blocked provider."
            if cancel_event is not None and cancel_event.is_set():
                self.event_sink("model_request_cancelled", {**payload, "usage": {}})
                raise ProviderCancelled("Test generation cancelled.")
            self.event_sink("model_reasoning_delta", {**payload, "text": "Public test reasoning"})
            self.event_sink("model_request_completed", {**payload, "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6}})
        return {"action": {"type": "end_turn"}, "reason": "Offline test decision", "plan": "Keep health"}


def make_session(tmp_path, *, block=False, attempts=1):
    """Build the production controller with only local doubles injected."""
    config = {
        "automatic": {"enabled": False},
        "limits": {"max_provider_requests_per_run": 30, "max_actions": 30, "max_run_wall_minutes": 60},
        "policy": {"mode": "NoSL"}, "provider": {"model": "deepseek-flash"},
    }
    bridge = FakeBridge()
    provider = FakeProvider(block=block, attempts=attempts)
    return DashboardSession(config, tmp_path, bridge=bridge, provider=provider), bridge, provider


@pytest.mark.parametrize("matching_token", [True, False])
def test_observation_binds_private_calibration_after_filtering_actions(tmp_path, matching_token):
    """Search keeps the same-frame private board after public control actions are filtered."""
    session, bridge, provider = make_session(tmp_path)
    bridge.state["available_actions"].append("save_and_quit")
    bridge.simulation_internal = {"mentor_state": {"type": "combat_action"}}
    bridge.simulation_internal_token = bridge.token if matching_token else "older-frame"
    observed, token = session._observe(settled=False)
    assert token == bridge.token
    assert observed["state"]["available_actions"] == ["end_turn"]
    assert "save_and_quit" in bridge.state["available_actions"]
    context = session.simulator._request_context(observed)
    assert context.get("internal_state") == (bridge.simulation_internal if matching_token else None)
    assert bridge.executions == [] and provider.calls == []


def test_simulation_settings_restore_without_refunding_run_budget(tmp_path):
    """Paused controls survive restart and invalidate proposals while consumed usage stays intact."""
    session, bridge, provider = make_session(tmp_path)
    session._state["metrics"]["requests_used"] = 7
    session._state["metrics"]["actions_completed"] = 5
    session._state["request_pending"] = True
    settings = {"read_access": "internal", "candidate_source": "model", "search_strength": 2.431234567}
    with pytest.raises(SessionError):
        session.configure_simulation(settings)
    assert not (tmp_path / "local/simulation-settings.json").exists()
    session._state["request_pending"] = False
    session._cached = {"decision_id": "previous-settings"}
    session._state["decision"] = {"decision_id": "previous-settings"}
    session.configure_simulation(settings)
    assert session._cached is None and session.snapshot()["decision"] is None
    assert {key: session.snapshot()["simulation_settings"][key] for key in settings} == settings
    assert session.snapshot()["simulation_settings"]["combat_decision_mode"] == "llm"
    assert session.snapshot()["simulation_settings"]["run_decision_mode"] == "llm"
    assert session.snapshot()["assistance_mode"] == "human_assisted"
    manifest = json.loads((session._journal.directory / "manifest.json").read_text())
    assert manifest["simulation"]["read_access"] == "internal"
    assert manifest["simulation_settings_history"][-1]["previous"]["read_access"] == "public"
    assert manifest["simulation_settings_history"][-1]["provider_requests"] == 7
    assert manifest["simulation_settings_history"][-1]["during_run"] is True
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.simulator.settings == json.loads((tmp_path / "local/simulation-settings.json").read_text())
    assert json.loads((tmp_path / "local/simulation-settings.json").read_text())["search_strength"] == 2.431234567
    assert restored.snapshot()["metrics"]["requests_used"] == 7
    assert restored.snapshot()["metrics"]["actions_completed"] == 5
    assert restored.snapshot()["simulation_settings_history"] == session.snapshot()["simulation_settings_history"]


def interrupted_completed_session(tmp_path):
    """Leave a durable completed receipt followed by an interrupted live observation."""
    session, bridge, provider = make_session(tmp_path)
    session._state["bridge_identity"] = list(bridge.identity)
    command = {"action_id": "completed-action", "action": {"type": "end_turn"},
        "expected_observation_token": "before", "decision_id": "decision", "accepted": False,
        "observation": {"state": deepcopy(bridge.state), "run_id": session._state["run_id"]}}
    session._record_submission(command)
    receipt = {"action_id": command["action_id"], "status": "completed", "stable": True,
        "public_state": deepcopy(bridge.state), "public_observation_token": "after"}
    session._observe = Mock(side_effect=RuntimeError("host exited before observation"))
    with pytest.raises(RuntimeError, match="host exited"):
        session._settle_receipt(command, receipt)
    return session, bridge, provider, receipt


def test_offline_completed_reconciliation_preserves_budget_and_never_replays(tmp_path):
    """Count historical completion once without fresh observation or repeated action memory."""
    session, bridge, provider, receipt = interrupted_completed_session(tmp_path)
    metrics = deepcopy(session._state["metrics"])
    memory = deepcopy(session.memory.events)
    bridge.health = Mock(side_effect=AssertionError("offline only"))
    bridge.receipt = Mock(side_effect=AssertionError("old process unavailable"))
    with WriterLock(tmp_path / "local/dashboard.lock"), WriterLock(tmp_path / "local/controller.lock"):
        result = session.reconcile_completed(receipt["action_id"])
        repeated = session.reconcile_completed(receipt["action_id"])
    assert repeated == result
    assert result["receipt"] == receipt and result["fresh_observation_required"] is True
    assert session._state["metrics"] == {**metrics, "actions_completed": metrics["actions_completed"] + 1}
    assert session.memory.events == memory
    assert session._state["inflight"] is None and session._state["observation"] is None
    assert session._state["desired_state"] == "paused" and session._cached is None
    assert not (tmp_path / "local/pending-action.json").exists()
    assert not bridge.executions and not provider.calls
    assert provider.resets[-1] == "historical_completion_reconciled"
    events = list(read_jsonl(session._journal.directory / "transitions.jsonl"))
    assert events[-1]["event_type"] == "historical_action_reconciled"
    assert not any(event["event_type"] == "action_completed" for event in events)
    restored, _, _ = make_session(tmp_path)
    assert restored.reconcile_completed(receipt["action_id"]) == result
    assert restored._state["metrics"]["actions_completed"] == metrics["actions_completed"] + 1


@pytest.mark.parametrize("invalid", ["missing", "identity", "run", "action", "pending", "unstable", "unsupported"])
def test_offline_completed_reconciliation_rejects_unproven_outcomes(tmp_path, invalid):
    """Keep pending submissions and spending unchanged when historical proof is invalid."""
    session, bridge, provider, receipt = interrupted_completed_session(tmp_path)
    path = session._journal.directory / "transitions.jsonl"
    records = list(read_jsonl(path))
    proof = records[-1]
    if invalid == "missing":
        records.pop()
    elif invalid == "identity":
        proof["bridge_identity"]["instance_id"] = "different-process"
    elif invalid == "run":
        proof["run_id"] = "different-run"
    elif invalid == "action":
        proof["data"]["receipt"]["action_id"] = "different-action"
    elif invalid == "pending":
        proof["data"]["receipt"]["status"] = "pending"
    elif invalid == "unstable":
        proof["data"]["receipt"]["stable"] = False
    else:
        proof["data"]["receipt"]["public_state"]["supported"] = False
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    before = deepcopy(session._state)
    with pytest.raises(SessionError):
        session.reconcile_completed(receipt["action_id"])
    assert session._state == before
    assert (tmp_path / "local/pending-action.json").exists()
    assert not bridge.executions and not provider.calls


@pytest.mark.parametrize("lock_name", ["dashboard.lock", "controller.lock"])
def test_offline_completed_cli_excludes_existing_writers(tmp_path, lock_name):
    """Acquire both service locks before constructing any recovery session."""
    from sts2_agent.session import reconcile_completed_action
    session, _, _, receipt = interrupted_completed_session(tmp_path)
    with WriterLock(tmp_path / "local" / lock_name), pytest.raises(RuntimeError, match="writer lock"):
        reconcile_completed_action(session.config, tmp_path, receipt["action_id"])


def test_offline_completed_recovery_survives_pending_cleanup_interruption(tmp_path, monkeypatch):
    """A committed completion survives failed pending cleanup without counting it again."""
    session, _, _, receipt = interrupted_completed_session(tmp_path)
    pending_path = tmp_path / "local/pending-action.json"
    original_unlink = Path.unlink

    def fail_pending_cleanup(path, *args, **kwargs):
        """Interrupt only the final cleanup after the counted ledger has reached disk."""
        if path == pending_path:
            raise PermissionError("cleanup interrupted")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_pending_cleanup)
        with pytest.raises(PermissionError, match="cleanup interrupted"):
            session.reconcile_completed(receipt["action_id"])
    restored, bridge, provider = make_session(tmp_path)
    assert pending_path.exists() and restored._state["inflight"] is None
    restored.reconcile_completed(receipt["action_id"])
    assert not pending_path.exists()
    assert restored._state["metrics"]["actions_completed"] == 1
    assert not bridge.executions and not provider.calls


def test_old_stream_timing_recovers_without_rebilling_or_replaying(tmp_path):
    """Recover missing text timing from existing events while preserving token accounting."""
    session, _, _ = make_session(tmp_path)
    session.command("one", "step")
    session._tick()
    request = session._state["model_requests"][-1]
    request["first_delta_utc"] = "2026-10-27T09:00:01+00:00"
    session._journal.event("dashboard-events", {
        "seq": session._state["seq"] + 1, "type": "model_content_delta",
        "timestamp_utc": "2026-10-27T09:00:03+00:00",
        "payload": {"model_request_id": request["model_request_id"], "text": "last fragment"}})
    metrics = deepcopy(session._state["metrics"])
    session._persist()
    restored, bridge, provider = make_session(tmp_path)
    recovered = restored.snapshot()["model_requests"][-1]
    assert recovered["first_delta_utc"] == "2026-10-27T09:00:01+00:00"
    assert recovered["last_delta_utc"] == "2026-10-27T09:00:03+00:00"
    assert restored._state["metrics"] == metrics
    assert not bridge.executions and not provider.calls


def test_worker_disk_failure_pauses_and_retains_inflight(tmp_path, monkeypatch, caplog):
    """Keep an unreceipted action and visible failure when saving the error also fails."""
    session, bridge, provider = make_session(tmp_path)
    session._state.update(desired_state="running", inflight={"action_id": "unresolved"})

    def fail_tick():
        """Stop the loop after one injected disk failure without dispatching an action."""
        session._shutdown.set()
        raise PermissionError("ledger replacement denied")

    def fail_persist():
        """Keep the disk unavailable while the worker records and finalizes its error."""
        raise PermissionError("disk remains unavailable")

    monkeypatch.setattr(session, "_tick", fail_tick)
    monkeypatch.setattr(session, "_persist", fail_persist)
    session._work()
    view = session.snapshot()
    assert view["desired_state"] == "paused"
    assert view["phase"] == "error"
    assert view["error"] == "ledger replacement denied"
    assert view["inflight"] == {"action_id": "unresolved"}
    assert not bridge.executions and not provider.calls
    assert "interruption could not be persisted" in caplog.text


def start_tick(session):
    """Run exactly one controller cycle and capture its exceptions for deterministic assertions."""
    failures = []

    def run():
        """Keep background failures visible to the calling test."""
        try:
            session._tick()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, failures


def finish_tick(thread, failures):
    """Require each controlled cycle to finish within a short bounded wait."""
    thread.join(3)
    assert not thread.is_alive(), "Controller cycle did not finish."
    assert not failures, failures


@pytest.mark.parametrize("command", ["pause", "stop"])
def test_cancel_during_generation_blocks_post_and_resume_generates_fresh(tmp_path, command):
    """Both controls stop output without retry, action, budget refund, or cached partial answer."""
    session, bridge, provider = make_session(tmp_path, block=True, attempts=2)
    session.command("resume", "resume")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        first = session.snapshot()
        session.command("cancel", command)
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert bridge.executions == []
    cancelled = session.snapshot()
    assert cancelled["status"] == ("stopped" if command == "stop" else "paused")
    assert not cancelled["request_pending"] and not cancelled["decision_pending"]
    assert cancelled["decision"] is None and session._cached is None
    assert cancelled["model_requests"][-1]["status"] == "cancelled"
    assert cancelled["model_requests"][-1]["reasoning"] == ""
    assert cancelled["metrics"]["unknown_usage_requests"] == 1
    assert cancelled["metrics"]["retries"] == 0 and cancelled["error"] is None
    result = json.loads((session._journal.directory / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "user_paused"
    assert result["provider_requests"] == session.snapshot()["metrics"]["requests_used"] == 1
    assert result["actions_completed"] == 0
    session.command("again", "resume")
    session._tick()
    assert len(provider.calls) == 2
    assert provider.calls[0]["context"]["decision_id"] != provider.calls[1]["context"]["decision_id"]
    assert provider.calls[1]["context"]["decision_episode_id"] == first["decision_episode_id"]
    assert session.snapshot()["next_combat_decision_counter"] == first["next_combat_decision_counter"]
    assert len(bridge.executions) == 1
    assert session.snapshot()["metrics"]["actions_completed"] == 1


@pytest.mark.parametrize("mode", ["resume", "step"])
def test_quick_resume_cannot_revive_cancelled_generation(tmp_path, mode):
    """A newer permit survives old cancellation cleanup, and queued deltas remain suppressed."""
    session, bridge, provider = make_session(tmp_path, block=True)
    session.instruction("guide", "Keep potions", "next_decision", "next_decision")
    session.command("begin", "resume")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        # Hold the transport until the new command has been accepted.
        provider.cancel = lambda event: event.set()
        session.command("pause", "pause")
        pending = session.snapshot()
        assert pending["generation_cancelling"]
        assert pending["model_requests"][-1]["status"] == "cancelling"
        session.command("continue", mode)
        request = pending["model_requests"][-1]
        session._provider_event("model_reasoning_delta", {**request, "text": "late output"})
        assert session.snapshot()["model_requests"][-1]["reasoning"] == ""
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert not bridge.executions and session._cached is None
    assert session.snapshot()["instructions"][0]["status"] != "consumed"
    session._tick()
    assert len(provider.calls) == 2 and len(bridge.executions) == 1
    assert session.snapshot()["instructions"][0]["status"] == "consumed"
    assert session._step_permit == 0


@pytest.mark.parametrize("stage", ["advice", "prediction"])
def test_pause_around_simulation_discards_unfinished_proposal(tmp_path, stage):
    """Cancellation before transport avoids a request; after completion it preserves its usage."""
    session, bridge, provider = make_session(tmp_path)
    started, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        """Expose a deterministic pause boundary around the model request."""
        started.set()
        assert release.wait(3)
        return {"status": "skipped"}

    if stage == "advice":
        session.simulator.advise = blocked
    else:
        session.simulator.predict = blocked
    session.command("begin", "resume")
    thread, failures = start_tick(session)
    try:
        assert started.wait(3)
        session.command("pause", "pause")
    finally:
        release.set()
    finish_tick(thread, failures)
    view = session.snapshot()
    assert not bridge.executions and session._cached is None and not view["decision_pending"]
    assert view["error"] is None and view["status"] == "paused"
    assert len(provider.calls) == (0 if stage == "advice" else 1)
    assert view["metrics"]["total_tokens"] == (0 if stage == "advice" else 15)


@pytest.mark.parametrize("command", ["pause", "stop"])
def test_cancel_preserves_already_completed_proposal(tmp_path, command):
    """An idle control keeps a complete proposal reusable without spending another request."""
    session, bridge, provider = make_session(tmp_path)
    session.command("prepare", "redecide")
    session._tick()
    session.command("cancel", command)
    session.command("continue", "step")
    session._tick()
    assert len(provider.calls) == len(bridge.executions) == 1


@pytest.mark.parametrize("mode", ["resume", "step"])
def test_state_change_during_generation_discards_old_proposal_and_preserves_step(tmp_path, mode):
    """Refresh before admission; a stale unsubmitted proposal spends no native step permit."""
    session, bridge, provider = make_session(tmp_path, block=True)
    session.instruction("guide", "Keep potions", "next_decision", "next_decision")
    session.command("begin", mode)
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        first_episode = session.snapshot()["decision_episode_id"]
        bridge.state["turn"] = 2
        bridge.token = "new-public-frame"
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert bridge.executions == []
    assert session._cached is None and session.snapshot()["decision"] is None
    assert session.snapshot()["decision_episode_id"] != first_episode
    assert session.snapshot()["metrics"]["actions_completed"] == 0
    assert session.snapshot()["desired_state"] == ("running" if mode == "resume" else "paused")
    assert session._step_permit == (1 if mode == "step" else 0)
    assert session.instructions.records[0]["status"] == "queued"
    assert provider.resets == ["public_state_changed"]
    assert len(provider.calls) == provider.requests_used == 1
    session._tick()
    assert len(provider.calls) == provider.requests_used == 2
    assert provider.calls[-1]["observation"]["state"]["turn"] == 2
    assert provider.calls[-1]["context"]["decision_episode_id"] != first_episode
    assert provider.calls[-1]["instructions"][0]["text"] == "Keep potions"
    assert len(bridge.executions) == 1
    assert bridge.executions[0]["token"] == "new-public-frame"
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    assert session._step_permit == 0


def test_queued_guidance_never_changes_current_complete_decision(tmp_path):
    """Guidance arriving during a request waits until the following complete decision."""
    session, bridge, provider = make_session(tmp_path, block=True)
    session.command("resume", "resume")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        session.instruction("guide", "Keep potions", "next_decision", "next_decision")
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert provider.calls[0]["instructions"] == []
    assert session.snapshot()["instructions"][0]["status"] == "queued"
    session._tick()
    assert provider.calls[1]["instructions"][0]["text"] == "Keep potions"
    assert session.snapshot()["instructions"][0]["status"] == "consumed"
    assert len(bridge.executions) == 2


def test_redecision_discards_late_result_retains_episode_and_prepares_paused(tmp_path):
    """Epoch retirement never refunds a request or changes an unchanged public episode."""
    session, bridge, provider = make_session(tmp_path, block=True)
    session.command("resume", "resume")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        original = session.snapshot()
        session.command("rethink", "redecide")
        manifest = json.loads((session._journal.directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["assistance_mode"] == "human_assisted"
        assert manifest["manual_gameplay_intervention"] is True
        assert manifest["current_limits"] == session.config["limits"]
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert bridge.executions == []
    assert any(item["type"] == "decision_discarded" for item in session.events_after(0))
    session._tick()
    prepared = session.snapshot()
    assert prepared["decision_epoch"] == original["decision_epoch"] + 1
    assert prepared["decision_episode_id"] == original["decision_episode_id"]
    assert prepared["status"] == "paused" and prepared["decision"] is not None
    assert prepared["metrics"]["requests_used"] == 2
    assert bridge.executions == []
    session.command("execute-one", "step")
    session._tick()
    assert len(provider.calls) == 2 and len(bridge.executions) == 1
    assert session.snapshot()["assistance_mode"] == "human_assisted"


def test_step_releases_exactly_one_native_action_then_stays_paused(tmp_path):
    """Repeated observer cycles cannot spend another native action after one step."""
    session, bridge, provider = make_session(tmp_path)
    session.command("one", "step")
    session._tick()
    session._tick()
    session._tick()
    assert len(bridge.executions) == 1
    assert len(provider.calls) == 1
    assert session.snapshot()["status"] == "paused"
    result = json.loads((session._journal.directory / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "user_paused"
    assert result["actions_completed"] == session.snapshot()["metrics"]["actions_completed"] == 1
    assert result["provider_requests"] == session.snapshot()["metrics"]["requests_used"] == 1


def test_unknown_step_receipt_is_not_refunded_or_resubmitted(tmp_path):
    """An uncertain native outcome permits receipt reads but blocks another step."""
    session, bridge, provider = make_session(tmp_path)
    bridge.execute_status = "unknown"
    session.command("one", "step")
    session._tick()
    assert session.snapshot()["inflight"] is not None
    assert session.snapshot()["metrics"]["actions_completed"] == 0
    with pytest.raises(SessionError):
        session.command("two", "step")
    session._tick()
    session._tick()
    assert len(bridge.executions) == 1 and len(bridge.receipt_reads) == 2
    bridge.receipt_status = "completed"
    session._tick()
    assert session.snapshot()["inflight"] is None
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    session._tick()
    assert len(bridge.executions) == 1 and len(provider.calls) == 1


def test_command_receipts_are_idempotent_and_conflicts_fail_without_effect(tmp_path):
    """Browser retries share persisted receipts instead of adding control revisions."""
    session, bridge, provider = make_session(tmp_path)
    receipt = session.command("same", "step")
    assert session.command("same", "step") == receipt
    with pytest.raises(SessionError):
        session.command("same", "pause")
    assert session.snapshot()["control_revision"] == receipt["control_revision"]
    session._tick()
    assert session.command("same", "step") == receipt
    session._tick()
    assert len(bridge.executions) == len(provider.calls) == 1
    spent = deepcopy(session.snapshot()["metrics"])
    session.command("stop", "stop")
    assert session.snapshot()["status"] == "stopped"
    session.command("restart-view", "start")
    assert session.snapshot()["status"] == "paused"
    session._state.update(phase="error", error="Recoverable offline bridge interruption")
    session.command("recover-view", "start")
    recovered = session.snapshot()
    assert recovered["status"] == "paused" and recovered["error"] is None
    session._tick()
    assert len(bridge.executions) == len(provider.calls) == 1
    assert recovered["metrics"]["requests_used"] == spent["requests_used"] == 1
    assert recovered["metrics"]["actions_completed"] == spent["actions_completed"] == 1


def test_snapshot_and_event_readers_do_not_start_another_controller(tmp_path):
    """Independent views receive detached monotonically ordered event data."""
    session, bridge, provider = make_session(tmp_path)
    session._tick()
    session.command("pause", "pause")
    first = session.snapshot()
    result = json.loads((session._journal.directory / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "user_paused"
    assert result["actions_completed"] == result["provider_requests"] == 0
    events = session.events_after(0)
    assert events and [event["seq"] for event in events] == sorted({event["seq"] for event in events})
    assert session.events_after(events[-1]["seq"]) == []
    assert session.wait_for_events(events[-1]["seq"], timeout=0) == []
    first["observation"]["state"]["turn"] = 99
    events[0]["type"] = "changed"
    assert session.snapshot()["observation"]["state"]["turn"] == 1
    assert session.events_after(0)[0]["type"] != "changed"
    assert session._thread is None
    assert bridge.executions == [] and provider.calls == []


def test_one_shot_model_retries_consume_only_after_native_acceptance(tmp_path):
    """Multiple provider requests in one decision share one instruction reservation."""
    session, bridge, provider = make_session(tmp_path, block=True, attempts=2)
    session.instruction("guide", "Save health", "next_decision", "next_decision")
    session.command("prepare", "redecide")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    record = session.snapshot()["instructions"][0]
    assert record["status"] == "injected" and len(record["injected_decisions"]) == 1
    assert session.snapshot()["metrics"]["requests_used"] == 2
    assert session.snapshot()["metrics"]["retries"] == 1
    session.command("step", "step")
    session._tick()
    assert session.snapshot()["instructions"][0]["status"] == "consumed"
    assert len(provider.calls) == len(bridge.executions) == 1


def test_revoke_preserves_captured_context_and_irreversible_assistance(tmp_path):
    """Default revoke removes future guidance while an existing proposal stays valid."""
    session, bridge, provider = make_session(tmp_path, block=True)
    receipt = session.instruction("guide", "Keep gold", "run", "next_decision")
    session.command("prepare", "redecide")
    thread, failures = start_tick(session)
    try:
        assert provider.started.wait(3)
        manifest = json.loads((session._journal.directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["assistance_mode"] == "human_assisted"
        assert manifest["manual_gameplay_intervention"] is True
        assert manifest["current_limits"] == session.config["limits"]
        session.revoke("revoke", receipt["instruction_id"])
    finally:
        provider.release.set()
    finish_tick(thread, failures)
    assert provider.calls[0]["instructions"][0]["text"] == "Keep gold"
    session.command("resume-again", "resume")
    session._tick()
    assert len(bridge.executions) == len(provider.calls) == 1
    record = session.snapshot()["instructions"][0]
    assert record["status"] == "revoked" and record["injected_decisions"]
    assert session.snapshot()["assistance_mode"] == "human_assisted"
    session._tick()
    assert provider.calls[1]["instructions"] == []


def test_restart_keeps_cached_decision_budget_guidance_and_assistance(tmp_path):
    """A restart resumes paused and reuses persisted context without refunding requests."""
    session, bridge, provider = make_session(tmp_path)
    session.instruction("guide", "Keep health", "next_decision", "redecide")
    session._tick()
    before = session.snapshot()
    assert before["status"] == "paused" and before["decision"] is not None
    replacement = FakeProvider()
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=replacement)
    after = restored.snapshot()
    assert after["run_id"] == before["run_id"]
    assert after["decision"] == before["decision"]
    assert after["metrics"]["requests_used"] == replacement.requests_used == 1
    assert after["assistance_mode"] == "human_assisted"
    assert after["instructions"][0]["injected_decisions"] == before["instructions"][0]["injected_decisions"]
    restored.command("execute", "step")
    restored._tick()
    assert replacement.calls == [] and len(bridge.executions) == 1
    assert restored.snapshot()["instructions"][0]["status"] == "consumed"
    assert restored.snapshot()["metrics"]["requests_used"] == 1


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_provider_flat_usage_is_counted_once_and_stays_known(tmp_path, terminal):
    """Terminal request events retain known usage and cannot bill the same request twice."""
    session, _, _ = make_session(tmp_path)
    session.command("prepare", "redecide")
    session._tick()
    metrics = session.snapshot()["metrics"]
    assert metrics["prompt_tokens"] == 10 and metrics["completion_tokens"] == 5
    assert metrics["total_tokens"] == 15 and metrics["unknown_usage_requests"] == 0
    assert metrics["cache_hit_rate"] == 0.4
    session._provider_event("model_request_" + terminal, {"model_request_id": "request-1", "usage": {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    assert session.snapshot()["metrics"]["total_tokens"] == 15


def test_pause_after_submission_waits_and_redecision_retains_experienced_action(tmp_path):
    """An admitted native action settles even when its original proposal is superseded."""
    session, bridge, provider = make_session(tmp_path)
    bridge.block_execute = True
    session.command("run", "resume")
    thread, failures = start_tick(session)
    try:
        assert bridge.executing.wait(3)
        session.command("pause", "pause")
        assert session.snapshot()["status"] == "pause_pending"
        session.command("rethink", "redecide")
    finally:
        bridge.finish_execution.set()
    finish_tick(thread, failures)
    assert session.snapshot()["inflight"] is None
    actions = [event for event in session.memory.events if event["source"] == "experienced_action"]
    assert len(actions) == 1
    assert actions[0]["action"] == {"type": "end_turn"}
    assert actions[0]["facts"]["turn"] == 1
    session._tick()
    assert len(provider.calls) == 2 and len(bridge.executions) == 1
    assert session.snapshot()["status"] == "paused"


def test_restart_with_unknown_native_outcome_only_reconciles_receipt(tmp_path):
    """Process recreation retains the spent step and never repeats its pending POST."""
    session, bridge, _ = make_session(tmp_path)
    bridge.execute_status = "unknown"
    session.command("one", "step")
    session._tick()
    action_id = bridge.executions[0]["action_id"]
    replacement = FakeProvider()
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=replacement)
    with pytest.raises(SessionError):
        restored.command("two", "step")
    restored._tick()
    assert bridge.receipt_reads == [action_id]
    assert len(bridge.executions) == 1 and replacement.calls == []
    bridge.receipt_status = "completed"
    restored._tick()
    assert restored.snapshot()["metrics"]["actions_completed"] == 1
    assert len(bridge.executions) == 1


def test_last_allowed_request_can_still_execute_its_cached_decision(tmp_path):
    """Reaching the request ceiling does not strand an already generated legal action."""
    session, bridge, provider = make_session(tmp_path)
    session._state["metrics"]["max_requests"] = 1
    session.command("prepare", "redecide")
    session._tick()
    assert session.snapshot()["decision"] is not None
    cached = deepcopy(session._cached)
    epoch = session.snapshot()["decision_epoch"]
    with pytest.raises(SessionError):
        session.command("too-expensive", "redecide")
    assert session._cached == cached
    assert session.snapshot()["decision_epoch"] == epoch
    session.command("pause", "pause")
    session.command("execute", "resume")
    session._tick()
    assert len(provider.calls) == len(bridge.executions) == 1
    session._tick()
    assert session.snapshot()["status"] == "budget_stop"
    with pytest.raises(SessionError):
        session.command("another", "step")


def test_native_action_ceiling_rejects_resume_and_stops_even_with_a_cache(tmp_path):
    """A paid cached proposal cannot bypass the native action budget."""
    session, bridge, provider = make_session(tmp_path)
    session.command("prepare", "redecide")
    session._tick()
    session.command("run", "resume")
    session._state["metrics"]["actions_completed"] = session._state["metrics"]["max_actions"]
    with pytest.raises(SessionError):
        session.command("past-limit", "resume")
    session._tick()
    assert session.snapshot()["status"] == "budget_stop"
    assert len(provider.calls) == 1 and bridge.executions == []


@pytest.mark.parametrize("mode", ["resume", "step"])
def test_terminal_successor_beats_last_budget_unit_and_stays_recorded(tmp_path, mode):
    """A final native action records public victory even on a step or the final allowed unit."""
    session, bridge, provider = make_session(tmp_path)
    session._state["metrics"].update(max_actions=1, max_requests=1)
    evidence = {"victory": True, "floor": 50, "character": "IRONCLAD", "phase": "victory"}

    def finish_run(action_id):
        """Expose a terminal public successor without contacting a native process."""
        bridge.state = {"protocol_version": 1, "supported": True, "screen": "GAME_OVER",
                        "available_actions": [], "game_over": deepcopy(evidence)}
        bridge.token = "terminal-frame"

    bridge._advance = finish_run
    session.command("final-action", mode)
    session._tick()
    assert session.snapshot()["status"] == "game_over"
    assert session.snapshot()["victory"] is True
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    assert provider.requests_used == len(bridge.executions) == 1
    session.command("pause-after-terminal", "pause")
    session._tick()
    result = json.loads((session._journal.directory / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "native_game_over"
    assert result["terminal_evidence"] == {
        "source": "bridge.public_state.game_over", "screen": "GAME_OVER", "game_over": evidence}


@pytest.mark.parametrize("active", [False, True])
def test_existing_terminal_public_state_beats_pause_and_exhausted_budget(tmp_path, active):
    """Discover terminal evidence without a model request while paused or already at the ceiling."""
    session, bridge, provider = make_session(tmp_path)
    bridge.state = {"protocol_version": 1, "supported": True, "screen": "GAME_OVER",
                    "available_actions": [], "game_over": {"victory": False, "floor": 20}}
    session._state["desired_state"] = "running" if active else "paused"
    session._state["metrics"]["actions_completed"] = session._state["metrics"]["max_actions"]
    session._tick()
    assert session.snapshot()["status"] == "game_over"
    assert session.snapshot()["victory"] is False
    assert provider.calls == [] and bridge.executions == []


def test_terminal_connection_refresh_preserves_finished_run(tmp_path):
    """Detect closure and recovery without observing, deciding, or rewriting terminal evidence."""
    from sts2_agent.bridge import BridgeError

    session, bridge, provider = make_session(tmp_path)
    bridge.state.update(screen="GAME_OVER", game_over={"victory": False, "floor": 46})
    session._tick()
    before = session.snapshot()
    identity = deepcopy(session._state["bridge_identity"])
    result_path = session._journal.directory / "result.json"
    result = result_path.read_bytes()
    bridge.snapshot = Mock(side_effect=AssertionError("Terminal public state must stay frozen"))
    bridge.health = Mock(side_effect=BridgeError("bridge_unreachable"))
    session._tick()
    disconnected = session.snapshot()
    assert disconnected["connection"] == {"connected": False, "error": "bridge_unreachable"}
    seq = disconnected["seq"]
    session._tick()
    assert session.snapshot()["seq"] == seq
    bridge.health.side_effect = None
    bridge.health.return_value = {"process_id": 123, "game_version": "v0.107.1", "status": "healthy"}
    session._tick()
    assert session.snapshot()["connection"]["connected"] is True
    bridge.health.side_effect = BridgeError("bridge_process_changed_receipts_lost")
    session._tick()
    after = session.snapshot()
    assert after["connection"]["connected"] is False
    assert after["status"] == "game_over" and after["error"] is None
    for key in ("observation", "terminal_evidence", "metrics"):
        assert after[key] == before[key]
    assert session._state["bridge_identity"] == identity
    assert result_path.read_bytes() == result
    assert provider.calls == [] and bridge.executions == []


@pytest.mark.parametrize("terminal", [False, True])
def test_elapsed_freezes_only_at_terminal_and_survives_restart(tmp_path, monkeypatch, terminal):
    """Terminal elapsed stays fixed while ordinary pause and downtime keep consuming wall time."""
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 27, tzinfo=timezone.utc)
    monkeypatch.setattr("sts2_agent.session.datetime", clock)
    session, bridge, _ = make_session(tmp_path)
    session._state["budget_started_utc"] = clock.now.return_value.isoformat()
    session._state["metrics"].update(requests_used=3, actions_completed=2)
    clock.now.return_value += timedelta(seconds=120)
    if terminal:
        session._record_game_over({"state": {"screen": "GAME_OVER", "game_over": {"victory": False}}})
    else:
        session.command("ordinary-pause", "pause")
    assert session.snapshot()["metrics"]["elapsed_seconds"] == 120
    clock.now.return_value += timedelta(hours=1)
    expected = 120 if terminal else 3720
    assert session.snapshot()["metrics"]["elapsed_seconds"] == expected
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.snapshot()["metrics"]["elapsed_seconds"] == expected
    assert restored.snapshot()["metrics"]["requests_used"] == 3
    assert restored.snapshot()["metrics"]["actions_completed"] == 2


def test_legacy_terminal_elapsed_is_recovered_from_matching_run_result(tmp_path, monkeypatch):
    """Use the historical terminal result without changing its final wall time or old events."""
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 27, tzinfo=timezone.utc)
    monkeypatch.setattr("sts2_agent.session.datetime", clock)
    session, bridge, _ = make_session(tmp_path)
    session._state["budget_started_utc"] = (clock.now.return_value - timedelta(seconds=9581.26)).isoformat()
    session._record_game_over({"state": {"screen": "GAME_OVER", "game_over": {"victory": False}}})
    state = json.loads(session._state_file.read_text(encoding="utf-8"))
    state.pop("terminal_elapsed_seconds")
    session._state_file.write_text(json.dumps(state), encoding="utf-8")
    result_path = session._journal.directory / "result.json"
    events_path = session._journal.directory / "dashboard-events.jsonl"
    original_result, original_events = result_path.read_bytes(), events_path.read_bytes()
    clock.now.return_value += timedelta(hours=2)
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.snapshot()["metrics"]["elapsed_seconds"] == 9581.26
    clock.now.return_value += timedelta(hours=1)
    assert restored.snapshot()["metrics"]["elapsed_seconds"] == 9581.26
    assert result_path.read_bytes() == original_result
    assert events_path.read_bytes() == original_events


@pytest.mark.parametrize("failure", ["validation", "evaluation"])
def test_rejected_decision_releases_guidance_and_discards_provider_history(tmp_path, monkeypatch, failure):
    """Rejected proposals remain explicit failures and never strand one-shot guidance or retry locally."""
    from sts2_agent.public import ActionError

    session, bridge, provider = make_session(tmp_path)
    session.instruction("guide", "Keep potions", "next_decision", "next_decision")
    original_decide = provider.decide

    def invalid_decision(obs, memory, *, instructions=None, cancel_event=None):
        """Return valid JSON structure containing an unavailable action for semantic rejection."""
        decision = original_decide(obs, memory, instructions=instructions, cancel_event=cancel_event)
        decision["action"] = {"type": "unavailable_action"}
        return decision

    def broken_evaluation(obs, action):
        """Expose a calculation failure after an otherwise valid model-selected action."""
        raise ValueError("Offline evaluator failure")

    if failure == "validation":
        monkeypatch.setattr(provider, "decide", invalid_decision)
    else:
        monkeypatch.setattr("sts2_agent.session.verify_selected_plan", broken_evaluation)
    session.command("run", "resume")
    with pytest.raises((ActionError, ValueError)):
        session._tick()
    record = session.instructions.records[0]
    assert record["status"] == "queued" and record["decision_id"] is None
    assert record["injected_decisions"]
    assert provider.resets == ["decision_rejected"]
    assert provider.requests_used == len(provider.calls) == 1
    assert session._cached is None and bridge.executions == []
    assert any(event["type"] == "decision_rejected" for event in session.snapshot()["events"])


def test_pause_during_native_post_is_prompt_and_prevents_a_following_action(tmp_path):
    """The command lock stays available while one native HTTP operation is blocked."""
    session, bridge, provider = make_session(tmp_path)
    bridge.block_execute = True
    session.command("run", "resume")
    thread, failures = start_tick(session)
    try:
        assert bridge.executing.wait(3)
        session.command("pause", "pause")
        assert session.snapshot()["status"] == "pause_pending"
        assert session.snapshot()["inflight"] is not None
    finally:
        bridge.finish_execution.set()
    finish_tick(thread, failures)
    session._tick()
    session._tick()
    assert session.snapshot()["status"] == "paused"
    assert session.snapshot()["metrics"]["actions_completed"] == 1
    assert len(provider.calls) == len(bridge.executions) == 1
    result = json.loads((session._journal.directory / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "user_paused"
    assert result["actions_completed"] == result["provider_requests"] == 1


def test_public_history_is_logged_after_settlement_and_restored_without_reexecution(tmp_path):
    """A completed public Power fact survives restart without touching budgets or native actions."""
    session, bridge, _ = make_session(tmp_path)
    bridge.state.update(action_revision=4, run={"relic_ids": ["PERMAFROST"]})
    bridge.state["combat"].update(
        hand=[{"i": 0, "card_id": "INFLAME", "card_type": "Power"}],
        action_readiness={field: True for field in (
            "can_use_combat_actions", "actions_settled", "snapshot_stable")})
    before, token = session._observe(settled=True)
    assert before["public_history"]["permafrost_first_power_used"] is None
    command = {"action_id": "history-action", "decision_id": "history-decision",
               "decision_episode_id": before["decision_episode_id"], "decision_epoch": 0,
               "expected_observation_token": token, "action": {"type": "play_card", "card_index": 0},
               "observation": before, "accepted": False}
    session._record_submission(command)
    bridge.state["action_revision"] = 5
    bridge.state["combat"]["hand"] = []
    session._settle_receipt(command, {"status": "completed", "action_id": "history-action",
                                    "public_state": deepcopy(bridge.state)})
    rows = [json.loads(line) for line in (session._journal.directory / "transitions.jsonl").read_text(
        encoding="utf-8").splitlines()]
    completed = next(row for row in rows if row["event_type"] == "action_completed")
    assert completed["schema_version"] == 2
    assert completed["data"]["observation"]["public_history"]["permafrost_first_power_used"] is True
    assert completed["data"]["observation_coverage"]["status"] == "partial"
    before_metrics = deepcopy(session._state["metrics"])
    restored = DashboardSession(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    after, _ = restored._observe(settled=True)
    assert after["public_history"]["permafrost_first_power_used"] is True
    assert after["public_history"]["observed_manual_power_plays"] == 1
    assert restored._state["metrics"] == before_metrics
    assert "public_history_tracker" not in restored.snapshot()
    assert not bridge.executions


def test_card_reward_claim_is_recognized_for_the_once_per_screen_rule():
    """Only a claim that resolves to a CARD option marks this reward screen as opened."""
    import json as _json
    from copy import deepcopy as _deepcopy
    fixture = _json.loads((Path(__file__).parent / "fixtures" / "public" / "reward.json").read_text(encoding="utf-8"))
    card = {"action": {"type": "claim_reward", "option_index": 1}, "observation": _deepcopy(fixture)}
    gold = {"action": {"type": "claim_reward", "option_index": 0}, "observation": _deepcopy(fixture)}
    proceed = {"action": {"type": "proceed"}, "observation": _deepcopy(fixture)}
    assert DashboardSession._opens_card_reward(card) is True
    assert DashboardSession._opens_card_reward(gold) is False
    assert DashboardSession._opens_card_reward(proceed) is False
    assert DashboardSession._opens_card_reward({}) is False

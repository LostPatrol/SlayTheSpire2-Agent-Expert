"""Verify explicit native launch preparation using real ledgers and offline bridge doubles."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from types import SimpleNamespace
import threading
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from sts2_agent import game_launcher
from sts2_agent.bridge import Bridge, BridgeError
from sts2_agent.dashboard import create_app
from sts2_agent.session import DashboardSession, SessionError, archive_terminal_session
from test_session_controls import FakeBridge, FakeProvider


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native launch handle inheritance")
def test_native_opener_returns_while_descendant_keeps_output_handles(tmp_path, monkeypatch):
    """A short-lived launcher must return while its game still holds inherited standard handles."""
    started = tmp_path / "child-started"
    release = tmp_path / "release-child"
    child_code = (
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).touch(); "
        f"deadline=time.monotonic()+15\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(.05)\n"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "deadline=time.monotonic()+5\n"
        f"while not Path({str(started)!r}).exists() and time.monotonic()<deadline: time.sleep(.05)\n"
    )
    native_run = subprocess.run

    def launch_descendant(_command, **kwargs):
        """Replace only the installed launcher, retaining production subprocess handle options."""
        return native_run([sys.executable, "-c", parent_code], **kwargs)

    bridge = Mock()
    bridge.health.side_effect = [BridgeError("bridge_unreachable"), {}]
    bridge.settled.return_value = ({"screen": "MAIN_MENU"}, "menu-token")
    monkeypatch.setattr(game_launcher, "Bridge", Mock(return_value=bridge))
    monkeypatch.setattr(game_launcher.shutil, "which", lambda _name: sys.executable)
    monkeypatch.setattr(game_launcher.subprocess, "run", launch_descendant)
    outcome = []

    def open_native():
        """Keep a failed pipe regression bounded so its descendant can always be released."""
        try:
            outcome.append(game_launcher._open_game({}, tmp_path))
        except Exception as error:
            outcome.append(error)

    worker = threading.Thread(target=open_native, daemon=True)
    worker.start()
    try:
        worker.join(5)
        assert started.exists(), "The simulated game did not start."
        assert not worker.is_alive(), "Launcher waited for the running game's output pipe EOF."
        assert outcome == [{"screen": "MAIN_MENU"}]
        bridge.client.close.assert_called_once()
    finally:
        release.touch()
        worker.join(5)


class MenuBridge(FakeBridge):
    """Reflect native menu transitions while keeping all observations public and local."""

    def __init__(self):
        """Start at a main menu with an unfinished save available to continue."""
        super().__init__()
        self.native_identity = self.identity
        self.config = {"expected_game_version": "v0.107.1"}
        self.loaded_screen = "EVENT"
        self.set_screen("MAIN_MENU")

    def _request(self, method, path):
        """Provide a public health envelope to the production process identity checks."""
        assert (method, path) == ("GET", "/health")
        return {"process_id": self.native_identity[0], "game_version": "v0.107.1",
                "external_protocol": {"version": 1, "action_receipts": True,
                                      "instance_id": self.native_identity[1]},
                "play_running": False, "instance_role": "human", "status": "healthy"}

    def health(self):
        """Use the real bridge identity guard instead of accepting every fake process."""
        return Bridge.health(self)

    def set_screen(self, screen):
        """Expose only the operator button permitted by this native screen."""
        actions = {"MAIN_MENU": ["continue_run"], "EVENT": ["choose_event_option"],
                   "GAME_OVER": ["return_to_main_menu"]}
        self.state = {"protocol_version": 1, "supported": True, "screen": screen,
                      "available_actions": actions[screen]}
        if screen == "GAME_OVER":
            self.state["game_over"] = {"victory": False}
        elif screen == "EVENT":
            self.state["event"] = {"options": [{"i": 0, "enabled": True}]}
        self.token = f"public-{screen}-{len(self._advanced)}"

    def _advance(self, action_id):
        """Apply each accepted operator transition once, including later receipt reconciliation."""
        if action_id in self._advanced:
            return
        self._advanced.add(action_id)
        action = next(item["action"]["type"] for item in self.executions if item["action_id"] == action_id)
        self.set_screen("MAIN_MENU" if action == "return_to_main_menu" else self.loaded_screen)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Inject production sessions with local providers and explicitly dormant background workers."""
    config = {"limits": {"max_provider_requests_per_run": 30, "max_actions": 30,
                         "max_run_wall_minutes": 60},
              "policy": {"mode": "NoSL"}, "provider": {"model": "deepseek-flash"}}
    bridge = MenuBridge()
    sessions = []
    providers = []

    def session_factory(_config, root):
        """Restore the real durable state while replacing only external dependencies and worker scheduling."""
        provider = FakeProvider()
        session = DashboardSession(_config, root, bridge=bridge, provider=provider)

        def start_paused_worker():
            """Persist readiness without scheduling a policy cycle or acquiring a second writer lock."""
            assert session.snapshot()["desired_state"] == "paused"
            session._persist()

        session.start_background = Mock(side_effect=start_paused_worker)
        session._record_artifacts = Mock()
        sessions.append(session)
        providers.append(provider)
        return session

    initial = session_factory(config, tmp_path)
    initial._persist()
    monkeypatch.setattr(game_launcher, "DashboardSession", session_factory)
    opener = Mock(side_effect=lambda _config, _root: bridge.snapshot()[0])
    monkeypatch.setattr(game_launcher, "_open_game", opener)
    launcher = game_launcher.GameLauncher(config, tmp_path, initial)
    yield SimpleNamespace(root=tmp_path, config=config, bridge=bridge, sessions=sessions,
                          providers=providers, session=initial, launcher=launcher, opener=opener)
    launcher.shutdown()


def finish_launch(launcher):
    """Wait only for the bounded fake native preparation and surface an unfinished worker."""
    launcher.thread.join(3)
    assert not launcher.thread.is_alive(), "Offline launch preparation did not finish."
    return launcher.snapshot()


def mark_terminal(harness):
    """Record an actual public terminal frame through the production result writer."""
    harness.bridge.set_screen("GAME_OVER")
    obs, _ = harness.session._observe(settled=True)
    assert harness.session._record_game_over(obs)
    harness.session._persist()
    result = harness.session._journal.directory / "result.json"
    return harness.session.snapshot()["run_id"], result, result.read_bytes()


def assert_no_provider_calls(harness):
    """Require every created policy dependency to remain idle during operator preparation."""
    assert all(not provider.calls and provider.requests_used == 0 for provider in harness.providers)


def test_launch_settings_persist_before_run_without_refunding_or_assistance(harness):
    """A menu preference survives restoration and never alters spending or assistance flags."""
    before = harness.session.snapshot()
    assert harness.launcher.launch_settings()["default"] == {"character": "IRONCLAD", "ascension": 0}
    result = harness.launcher.set_launch_settings({"character": "SILENT", "ascension": 4})
    assert (result["character"], result["ascension"], result["editable"]) == ("SILENT", 4, True)
    after = harness.session.snapshot()
    assert after["metrics"]["requests_used"] == before["metrics"]["requests_used"]
    assert after["assistance_mode"] == before["assistance_mode"]
    restored = DashboardSession(harness.config, harness.root, bridge=harness.bridge, provider=FakeProvider())
    assert restored.config["policy"]["character"] == "SILENT"
    assert restored.config["policy"]["ascension"] == 4
    assert restored.snapshot()["run_id"] == before["run_id"]
    harness.bridge.set_screen("EVENT")
    harness.session._observe(settled=True)
    assert harness.launcher.launch_settings()["editable"] is False
    with pytest.raises(SessionError):
        harness.launcher.set_launch_settings({"character": "IRONCLAD", "ascension": 0})
    assert harness.session.config["policy"]["character"] == "SILENT"
    assert_no_provider_calls(harness)


def test_launch_settings_api_rejects_invalid_and_busy_changes(harness):
    """HTTP input and live readiness enforce the same pre-run boundary as the UI."""
    app = create_app(harness.config, harness.root, session=harness.session)
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        for body in ({"character": "RANDOM", "ascension": 0}, {"character": "IRONCLAD", "ascension": 11},
                     {"character": "IRONCLAD", "ascension": True}):
            assert client.post("/api/game/settings", json=body).status_code == 422
        valid = {"character": "DEFECT", "ascension": 2}
        assert client.post("/api/game/settings", json=valid).status_code == 200
        assert client.get("/api/snapshot").json()["launch_settings"]["character"] == "DEFECT"
        harness.session._state["request_pending"] = True
        assert client.post("/api/game/settings", json=valid).status_code == 409
        harness.session._state["request_pending"] = False


def test_selected_launch_settings_survive_terminal_archive(harness):
    """Selecting the next run's setup must outlive rotation of the old terminal ledger."""
    old_run, result, original_result = mark_terminal(harness)
    harness.launcher.set_launch_settings({"character": "SILENT", "ascension": 3})
    harness.launcher.start("prepare-selected-next-run")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert view["run_id"] != old_run
    assert harness.launcher.session.config["policy"]["character"] == "SILENT"
    assert harness.launcher.session.config["policy"]["ascension"] == 3
    assert result.read_bytes() == original_result
    assert_no_provider_calls(harness)


def test_launch_restores_save_and_waits_for_explicit_autoplay(harness):
    """Opening the game continues its save without a policy request or automatic resume."""
    run_id = harness.session.snapshot()["run_id"]
    receipt = harness.launcher.start("open-native-game")
    assert receipt["status"] == "accepted"
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert view["desired_state"] == "paused"
    assert view["run_id"] == run_id
    assert view["observation"]["state"]["screen"] == "EVENT"
    assert view["metrics"]["actions_completed"] == 0
    assert view["metrics"]["requests_used"] == 0
    assert [item["action"] for item in harness.bridge.executions] == [{"type": "continue_run"}]
    assert harness.launcher.session.start_background.call_count == 1
    assert_no_provider_calls(harness)


@pytest.mark.parametrize("native_screen", ["MAIN_MENU", "GAME_OVER"])
@pytest.mark.parametrize("replacement_process", [False, True])
def test_terminal_archive_preserves_original_result_bytes(harness, native_screen, replacement_process):
    """Leaving an old terminal and rotating its ledger never rewrites the original result."""
    old_run, result, original_result = mark_terminal(harness)
    harness.bridge.set_screen(native_screen)
    if replacement_process:
        harness.bridge.native_identity = (456, "replacement-offline-instance")
    harness.launcher.start("prepare-next-run")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert view["run_id"] != old_run
    archives = list((harness.root / "local/session-archives").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["run_id"] == old_run
    assert result.read_bytes() == original_result
    active_ledger = json.loads((harness.root / "local/dashboard-session.json").read_text(encoding="utf-8"))
    assert active_ledger["bridge_identity"] == list(harness.bridge.native_identity)
    actions = [item["action"]["type"] for item in harness.bridge.executions]
    assert actions == (["return_to_main_menu", "continue_run"] if native_screen == "GAME_OVER" else ["continue_run"])
    assert_no_provider_calls(harness)


def test_terminal_worker_shutdown_preserves_final_result(harness):
    """The real observing worker's shutdown retains the already finalized native result bytes."""
    _old_run, result, original_result = mark_terminal(harness)
    DashboardSession.start_background(harness.session)
    harness.bridge.set_screen("MAIN_MENU")
    harness.launcher.start("replace-live-terminal-worker")
    assert finish_launch(harness.launcher)["game_launch"]["status"] == "ready"
    assert not harness.session._thread.is_alive()
    assert result.read_bytes() == original_result
    assert_no_provider_calls(harness)


def test_unfinished_reopen_retains_run_and_cumulative_budget(harness):
    """Repeated preparation preserves the unfinished run's paid work and elapsed-time origin."""
    session = harness.session
    session._state["budget_started_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    session._state["metrics"].update(requests_used=7, actions_completed=4, prompt_tokens=70)
    session._persist()
    original = session.snapshot()
    harness.launcher.start("prepare-first-time")
    first = finish_launch(harness.launcher)
    harness.bridge.set_screen("MAIN_MENU")
    harness.launcher.start("prepare-again")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert first["run_id"] == view["run_id"] == original["run_id"]
    assert view["budget_started_utc"] == original["budget_started_utc"]
    assert view["metrics"]["requests_used"] == 7
    assert view["metrics"]["actions_completed"] == 4
    assert view["metrics"]["prompt_tokens"] == 70
    assert view["metrics"]["elapsed_seconds"] >= original["metrics"]["elapsed_seconds"]
    assert not (harness.root / "local/session-archives").exists()
    assert len(harness.bridge.executions) == 2
    assert all(not provider.calls for provider in harness.providers)


def test_unfinished_native_restart_preserves_run_and_budget_without_pending_work(harness):
    """An explicit launch may bind a replacement game process only after pending work is excluded."""
    session = harness.session
    session._observe(settled=True)
    session._state["metrics"].update(requests_used=7, actions_completed=4)
    session._persist()
    original = session.snapshot()
    harness.bridge.native_identity = (456, "replacement-offline-instance")
    harness.launcher.start("restart-native-process")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert view["run_id"] == original["run_id"]
    assert view["metrics"]["requests_used"] == 7 and view["metrics"]["actions_completed"] == 4
    retained = json.loads((harness.root / "local/dashboard-session.json").read_text(encoding="utf-8"))
    assert retained["bridge_identity"] == [456, "replacement-offline-instance"]
    assert not view["inflight"] and not (harness.root / "local/pending-action.json").exists()
    assert all(not provider.calls for provider in harness.providers)


@pytest.mark.parametrize("pending_kind", ["action", "model", "pending_file"])
def test_pending_work_rejects_launch_without_ledger_changes(harness, pending_kind):
    """In-flight work is rejected before the game opens, shutdown occurs, or durable bytes change."""
    if pending_kind == "action":
        harness.session._state["inflight"] = {"action_id": "unresolved-native-action"}
    elif pending_kind == "model":
        harness.session._state["request_pending"] = True
    else:
        (harness.root / "local/pending-action.json").write_text('{"action_id":"unknown"}', encoding="utf-8")
    harness.session._persist()
    ledger = harness.root / "local/dashboard-session.json"
    original_ledger = ledger.read_bytes()
    with pytest.raises(SessionError):
        harness.launcher.start("unsafe-open")
    assert ledger.read_bytes() == original_ledger
    assert not harness.opener.called
    assert len(harness.sessions) == 1
    assert not harness.bridge.executions
    assert_no_provider_calls(harness)


def test_duplicate_command_id_opens_game_only_once(harness):
    """A confirmed retry returns the same receipt before and after preparation completes."""
    first = harness.launcher.start("same-launch-request")
    finish_launch(harness.launcher)
    assert harness.launcher.start("same-launch-request") == first
    assert harness.opener.call_count == 1
    assert len(harness.bridge.executions) == 1
    assert_no_provider_calls(harness)


def test_http_controls_block_during_start_while_snapshot_remains_readable(harness, monkeypatch):
    """Brief native preparation rejects mutations while HTTP reads and retry receipts remain available."""
    opened = threading.Event()
    release = threading.Event()

    def blocking_open(_config, _root):
        """Hold native loading at a deterministic point without blocking snapshot readers."""
        opened.set()
        assert release.wait(3), "Test did not release native loading."
        return harness.bridge.snapshot()[0]

    monkeypatch.setattr(game_launcher, "_open_game", blocking_open)
    app = create_app(harness.config, harness.root, session=harness.session)
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        try:
            body = {"command_id": "http-launch"}
            first = client.post("/api/game/start", json=body)
            assert first.status_code == 200 and opened.wait(3)
            ledger = harness.root / "local/dashboard-session.json"
            preparing_ledger = ledger.read_bytes()
            assert client.get("/api/snapshot").json()["game_launch"]["status"] == "starting"
            assert client.post("/api/game/start", json=body).json() == first.json()
            assert client.post("/api/game/start", json={"command_id": "different-launch"}).status_code == 409
            assert client.post("/api/commands", json={"command_id": "resume", "command": "resume"}).status_code == 409
            assert client.post("/api/instructions", json={"command_id": "guidance", "text": "Test guidance"}).status_code == 409
            assert client.post("/api/instructions/unused/revoke", json={"command_id": "revoke"}).status_code == 409
            assert ledger.read_bytes() == preparing_ledger
        finally:
            release.set()
        assert finish_launch(app.state.launcher)["game_launch"]["status"] == "ready"
    assert_no_provider_calls(harness)


def test_archive_failure_keeps_terminal_ledger_and_original_result(harness, monkeypatch):
    """A failed atomic archive leaves the finished run available with its result unchanged."""
    old_run, result, original_result = mark_terminal(harness)
    harness.bridge.set_screen("MAIN_MENU")
    ledger = harness.root / "local/dashboard-session.json"
    original_ledger = ledger.read_bytes()
    monkeypatch.setattr(game_launcher, "_archive_terminal_ledger", Mock(side_effect=PermissionError("archive is occupied")))
    harness.launcher.start("failed-archive")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "error"
    assert view["run_id"] == old_run
    retained = json.loads(ledger.read_text(encoding="utf-8"))
    original = json.loads(original_ledger)
    for field in ("run_id", "metrics", "budget_started_utc", "terminal_evidence", "terminal_elapsed_seconds"):
        assert retained[field] == original[field]
    assert result.read_bytes() == original_result
    assert not (harness.root / "local/session-archives").exists()
    assert not harness.bridge.executions
    assert_no_provider_calls(harness)


def test_unknown_navigation_receipt_retains_pending_and_blocks_archive(harness):
    """An uncertain native menu action remains durable and is never replayed or archived away."""
    old_run, result, original_result = mark_terminal(harness)
    harness.bridge.execute_status = "unknown"
    harness.launcher.start("uncertain-menu-return")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "error"
    assert view["run_id"] == old_run and view["inflight"]
    pending_path = harness.root / "local/pending-action.json"
    original_pending = pending_path.read_bytes()
    pending = json.loads(original_pending)
    assert pending["run_id"] == old_run
    assert pending["action"] == {"type": "return_to_main_menu"}
    assert pending["operator_navigation"] is True
    with pytest.raises(SessionError):
        harness.launcher.start("retry-uncertain-launch")
    with pytest.raises(SessionError, match="Reconcile"):
        archive_terminal_session(harness.root)
    assert pending_path.read_bytes() == original_pending
    assert result.read_bytes() == original_result
    assert len(harness.bridge.executions) == 1
    assert_no_provider_calls(harness)
    harness.bridge.receipt_status = "completed"
    harness.launcher.session._tick()
    assert not pending_path.exists()
    assert harness.launcher.session.snapshot()["inflight"] is None
    assert harness.bridge.receipt_reads == [pending["action_id"]]
    assert len(harness.bridge.executions) == 1
    assert result.read_bytes() == original_result
    # Later native actions complete normally after the original uncertain outcome is reconciled.
    harness.bridge.execute_status = "completed"
    harness.launcher.start("prepare-after-receipt-recovery")
    ready = finish_launch(harness.launcher)
    assert ready["game_launch"]["status"] == "ready"
    assert ready["run_id"] != old_run
    archives = list((harness.root / "local/session-archives").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["run_id"] == old_run
    assert [item["action"]["type"] for item in harness.bridge.executions] == ["return_to_main_menu", "continue_run"]
    assert result.read_bytes() == original_result
    assert_no_provider_calls(harness)

"""Exercise dashboard HTTP boundaries and event framing without a game or model."""

import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from sts2_agent.dashboard import _encode_sse, _event_cursor, _event_stream, create_app, serve
from types import SimpleNamespace


def test_service_stop_requires_a_paused_settled_session(tmp_path):
    """Deployment shutdown cannot interrupt active requests or unresolved native actions."""
    session = FakeSession()
    view = {"desired_state": "running", "inflight": None, "request_pending": False}
    session.snapshot = lambda: view
    app = create_app({}, tmp_path, session=session)
    app.state.server = SimpleNamespace(should_exit=False)
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        assert client.post("/api/service/stop").status_code == 409
        view["desired_state"] = "paused"
        view["request_pending"] = True
        assert client.post("/api/service/stop").status_code == 409
        view["request_pending"] = False
        assert client.post("/api/service/stop").status_code == 200
        assert app.state.server.should_exit is True


def test_simulation_settings_route_keeps_modes_independent_and_validates_effort(tmp_path):
    """HTTP controls preserve fractional effort while rejecting nonnumeric or nonfinite input."""
    session = FakeSession()
    calls = []
    def configure(settings):
        """Capture the reviewed session boundary without starting native work."""
        calls.append(settings)
        return settings
    session.configure_simulation = configure
    app = create_app({}, tmp_path, session=session)
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        settings = {"read_access": "internal", "candidate_source": "model", "search_strength": 3.125789}
        response = client.post("/api/simulation/settings", json=settings)
        assert response.status_code == 200
        assert calls == [settings]
        assert response.json()["simulation_settings"] == {**settings, "editable": True}
        for value in (True, False, float("nan"), float("inf"), float("-inf"), "3.5", 0.99, 5.01):
            response = client.post("/api/simulation/settings", content=json.dumps({**settings, "search_strength": value}),
                                   headers={"Content-Type": "application/json"})
            assert response.status_code == 422, (value, response.text)
        assert client.post("/api/simulation/settings", json={**settings, "search_strength": 3}).status_code == 200
        assert client.post("/api/simulation/settings", json={**settings, "candidate_source": "beam"}).status_code == 422


def test_network_controls_api_preserves_explicit_owners_and_component_choices(tmp_path):
    """The dashboard can independently set decision owners and each real search component."""
    session = FakeSession()
    captured = []
    def configure(settings):
        """Capture fields at the session boundary without network or game activity."""
        captured.append(settings)
        return settings
    session.configure_simulation = configure
    app = create_app({}, tmp_path, session=session)
    settings = {"read_access": "public", "candidate_source": "model", "search_strength": 2.431234567,
        "combat_decision_mode": "upstream", "run_decision_mode": "assist", "rollout_policy": "heuristic",
        "action_prior": "off", "value_model": "learned", "search_profile": "base", "reward_pool": "full_unlock"}
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        assert client.post("/api/simulation/settings", json=settings).status_code == 200
        assert captured == [settings]
        assert client.post("/api/simulation/settings", json={**settings, "value_model": "policy"}).status_code == 422
        assert client.post("/api/simulation/settings", json={**settings, "combat_decision_mode": "off"}).status_code == 422
        assert client.post("/api/simulation/settings", json={**settings, "reward_pool": "guess"}).status_code == 422


class FakeSession:
    """Record session calls while preserving the real idempotency contract."""

    def __init__(self):
        """Create one paused, in-memory session with no external dependencies."""
        self.started = 0
        self.stopped = 0
        self.commands = {}
        self.instructions = []
        self.revocations = []
        self.waited_after = []
        self.pending = []

    def start_background(self):
        """Count server startup, never HTTP reads."""
        self.started += 1

    def shutdown(self):
        """Count deterministic lifespan cleanup."""
        self.stopped += 1

    def snapshot(self):
        """Return only public state suitable for the dashboard."""
        return {"seq": 8, "status": "paused", "instructions": self.instructions, "metrics": {}}

    def command(self, command_id, command):
        """Apply each command identifier once, as the real session does."""
        if command_id not in self.commands:
            self.commands[command_id] = {"command_id": command_id, "command": command}
        return self.commands[command_id]

    def instruction(self, command_id, text, scope, timing):
        """Capture the exact scope and timing forwarded by the endpoint."""
        receipt = {"id": command_id, "text": text, "scope": scope, "timing": timing}
        self.instructions.append(receipt)
        return receipt

    def revoke(self, command_id, instruction_id):
        """Capture an explicit revocation command."""
        receipt = {"command_id": command_id, "instruction_id": instruction_id}
        self.revocations.append(receipt)
        return receipt

    def wait_for_events(self, after, timeout):
        """Return preloaded public events without sleeping or model activity."""
        self.waited_after.append(after)
        return self.pending.pop(0)


def test_snapshot_refresh_and_duplicate_command_share_one_session(tmp_path):
    """Refreshes read one server-owned session; repeated POSTs share its ledger."""
    session = FakeSession()
    app = create_app({}, tmp_path, session=session)
    with TestClient(app, base_url="http://localhost:18766") as client:
        for _index in range(3):
            assert client.get("/api/snapshot").json()["status"] == "paused"
        body = {"command_id": "pause-1", "command": "pause"}
        first = client.post("/api/commands", json=body)
        second = client.post("/api/commands", json=body)
        assert first.status_code == 200
        assert second.json() == first.json()
        assert len(session.commands) == 1
        assert session.started == 1
    assert session.stopped == 1


def test_instruction_parameters_revoke_and_invalid_requests(tmp_path):
    """Keep the selected instruction lifetime and reject malformed controls."""
    session = FakeSession()
    with TestClient(create_app({}, tmp_path, session=session), base_url="http://127.0.0.1:18766") as client:
        result = client.post("/api/instructions", json={
            "command_id": "guide-1", "text": "保留药水", "scope": "combat", "timing": "redecide",
        })
        assert result.status_code == 200
        assert result.json()["receipt"]["scope"] == "combat"
        assert result.json()["receipt"]["timing"] == "redecide"
        assert client.post("/api/instructions/guide-1/revoke", json={"command_id": "revoke-1"}).status_code == 200
        assert session.revocations == [{"command_id": "revoke-1", "instruction_id": "guide-1"}]
        invalid = [
            ("/api/commands", {"command_id": "x", "command": "toggle"}),
            ("/api/commands", {"command_id": "  ", "command": "step"}),
            ("/api/instructions", {"command_id": "x", "text": "x" * 3001}),
            ("/api/instructions", {"command_id": "x", "text": "x", "scope": "forever"}),
        ]
        for path, body in invalid:
            assert client.post(path, json=body).status_code == 422
        assert client.get("/api/events?after=-1").status_code == 422
        assert client.get("/api/events", headers={"Last-Event-ID": "not-a-number"}).status_code == 422


def test_origins_and_hosts_are_loopback_same_origin_only(tmp_path):
    """A browser on another site or port cannot send a control command."""
    session = FakeSession()
    body = {"command_id": "pause-1", "command": "pause"}
    with TestClient(create_app({}, tmp_path, session=session), base_url="http://localhost:18766") as client:
        for origin in ["https://example.com", "http://localhost:80", "null", "http://127.0.0.1:18766"]:
            assert client.post("/api/commands", json=body, headers={"Origin": origin}).status_code == 403
        assert session.commands == {}
        assert client.get("/api/snapshot", headers={"Host": "example.com"}).status_code == 403
        assert client.post("/api/commands", json=body, headers={"Origin": "http://localhost:18766"}).status_code == 200


def test_lifespan_lock_excludes_a_second_server_and_releases(tmp_path):
    """A second server cannot start its worker, and shutdown releases the lock."""
    first = FakeSession()
    second = FakeSession()
    with TestClient(create_app({}, tmp_path, session=first), base_url="http://localhost"):
        with pytest.raises(RuntimeError, match="writer lock"):
            with TestClient(create_app({}, tmp_path, session=second), base_url="http://localhost"):
                pass
        assert second.started == 0
    with TestClient(create_app({}, tmp_path, session=second), base_url="http://localhost"):
        assert second.started == 1
    assert first.stopped == second.stopped == 1


@pytest.mark.parametrize("error,status", [(ValueError("private backend detail"), 422), (RuntimeError("private backend detail"), 409)])
def test_session_errors_never_expose_backend_details(tmp_path, error, status):
    """Return a safe error boundary even if a backend exception contains secrets."""
    class BrokenSession(FakeSession):
        """Fail one command at the API boundary."""

        def command(self, command_id, command):
            """Simulate an invalid operation or session conflict."""
            raise error

    with TestClient(create_app({}, tmp_path, session=BrokenSession()), base_url="http://localhost") as client:
        result = client.post("/api/commands", json={"command_id": "x", "command": "step"})
        assert result.status_code == status
        assert "private backend detail" not in result.text


def test_event_resume_and_reset_framing():
    """Resume IDs survive reconnects and expired history requests a new snapshot."""
    event = {"seq": 7, "type": "model_delta", "timestamp_utc": "now", "payload": {"text": "line1\n行2"}}
    encoded = _encode_sse(event)
    assert encoded.startswith("id: 7\nevent: event\ndata: ")
    assert json.loads(encoded.split("data: ", 1)[1]) == event
    assert _event_cursor(3, "6") == 6
    assert _event_cursor(None, None) == 0

    class ConnectedRequest:
        """Allow the test to pull exact frames before closing the generator."""

        async def is_disconnected(self):
            """Keep the stream open until the test explicitly closes it."""
            return False

    async def read_frames():
        """Read history and reset frames without a buffering HTTP test client."""
        session = FakeSession()
        session.pending = [[event], None]
        stream = _event_stream(ConnectedRequest(), session, 6)
        first = await anext(stream)
        reset = await anext(stream)
        await stream.aclose()
        assert first == encoded
        assert reset.startswith("id: 8\nevent: reset\ndata: ")
        assert json.loads(reset.split("data: ", 1)[1])["payload"]["snapshot_required"] is True
        assert session.waited_after == [6, 7]

    asyncio.run(read_frames())


def test_serve_rejects_remote_bind_before_creating_a_session(tmp_path):
    """The public launch function cannot accidentally expose local control."""
    with pytest.raises(ValueError, match="Dashboard host"):
        serve({}, tmp_path, host="0.0.0.0", open_browser=False)

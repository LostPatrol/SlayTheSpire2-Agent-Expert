"""Exercise generation cancellation against real loopback sockets and offline envelopes."""

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Event, Thread
import time

import httpx
import pytest

from sts2_agent.config import load_config
from sts2_agent.provider import Provider, ProviderCancelled
from test_provider import DECISION, ROOT, TEST_KEY, build, completion
from test_provider_stream_cache import frame, stream


# Local waits detect stalled cancellation well before the configured HTTP read timeout.
WAIT_SECONDS = 4


@contextmanager
def stalled_server(mode):
    """Expose a loopback completion endpoint that observes the client's TCP disconnect."""
    ready, disconnected = Event(), Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        """Serve one optional reusable response before deliberately withholding data."""

        # A content length on the warmup response permits HTTPX connection reuse.
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            """Keep test request headers and paths out of console output."""

        def do_POST(self):
            """Read the request and wait for peer EOF without sending another token."""
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.client_address)
            if mode == "reused_headers" and len(requests) == 1:
                payload = json.dumps({"model": "deepseek-flash", "choices": [{
                    "finish_reason": "stop", "message": {"content": json.dumps(DECISION)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                return
            if mode == "sse":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                payload = ("data: " + json.dumps(frame(
                    {"reasoning_content": "partial reasoning"}, usage={"prompt_tokens": 17}))
                    + "\n\n").encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
            ready.set()
            self.connection.settimeout(WAIT_SECONDS)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except (ConnectionResetError, ConnectionAbortedError):
                disconnected.set()
            except socket.timeout:
                pass
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", ready, disconnected, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(WAIT_SECONDS)


def run_decision(provider, cancel_event):
    """Run a synchronous decision worker while retaining either its result or exception."""
    outcomes = []

    def decide():
        """Capture worker errors so tests assert them on the main thread."""
        try:
            outcomes.append(provider.decide({}, {}, cancel_event=cancel_event))
        except Exception as error:
            outcomes.append(error)

    worker = Thread(target=decide, daemon=True)
    worker.start()
    return worker, outcomes


@pytest.mark.parametrize("mode", ["sse", "headers", "reused_headers"])
def test_cancel_disconnects_stalled_peer_without_retry(monkeypatch, mode):
    """Interrupt blocked reads before and after headers, including an existing connection."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    with stalled_server(mode) as (base, ready, disconnected, requests):
        config = deepcopy(load_config("configs/smoke.json", ROOT))
        config["provider"].update(api_base=base, timeout_seconds=30, transient_retries=2,
                                  use_env_proxy=False)
        provider = Provider(config, ROOT)
        seen_delta = Event()
        events = []

        def sink(kind, payload):
            """Retain public request evidence and signal when the first token was consumed."""
            events.append((kind, payload))
            if kind == "model_reasoning_delta":
                seen_delta.set()

        provider.event_sink = sink
        provider.event_context = {"decision_id": "cancelled-decision", "decision_epoch": 9}
        cancel_event = Event()
        if mode == "reused_headers":
            assert provider.decide({}, {}, cancel_event=Event()) == DECISION
            provider.reset_context()
            events.clear()
        worker, outcomes = run_decision(provider, cancel_event)
        try:
            assert ready.wait(WAIT_SECONDS)
            if mode == "sse":
                assert seen_delta.wait(WAIT_SECONDS)
            started = time.monotonic()
            provider.cancel(cancel_event)
            assert time.monotonic() - started < 0.5
            worker.join(WAIT_SECONDS)
            assert not worker.is_alive(), "Cancellation waited for the configured 30-second read timeout"
            assert len(outcomes) == 1 and isinstance(outcomes[0], ProviderCancelled)
            assert disconnected.wait(WAIT_SECONDS), "The loopback peer did not receive TCP EOF/reset"
            assert len(requests) == provider.requests_used == (2 if mode == "reused_headers" else 1)
            if mode == "reused_headers":
                assert requests[0] == requests[1], "The header test must exercise the same pooled connection"
            assert provider._history == [] and provider._history_turns == 0
            terminal = [(kind, payload) for kind, payload in events
                        if kind in {"model_request_completed", "model_request_failed", "model_request_cancelled"}]
            assert len(terminal) == 1 and terminal[0][0] == "model_request_cancelled"
            record = terminal[0][1]
            assert record["decision_id"] == "cancelled-decision" and record["decision_epoch"] == 9
            assert record["error"] == "cancelled" and record["duration_ms"] >= 0
            assert record["usage"]["prompt_tokens"] == (17 if mode == "sse" else "unknown")
            assert record["usage"]["completion_tokens"] == "unknown"
            assert record["usage"] == provider.metadata[-1]["usage"]
            assert "completed_utc" in record and "model_request_id" in record
            if mode == "sse":
                assert record["actual_model"] == "deepseek-flash" and "last_delta_elapsed_ms" in record
            assert TEST_KEY not in json.dumps(events)
        finally:
            provider.cancel(cancel_event)
            worker.join(WAIT_SECONDS)
            if not worker.is_alive():
                provider.close()


def test_pre_cancelled_generation_never_opens_a_request(monkeypatch):
    """A paused simulation cannot later start its obsolete provider call."""
    provider, requests = build(monkeypatch, [])
    cancel_event = Event()
    cancel_event.set()
    with pytest.raises(ProviderCancelled):
        provider.decide({}, {}, cancel_event=cancel_event)
    assert provider.requests_used == 0 and not requests and not provider._history


def test_cancel_stops_buffered_deltas_and_resume_uses_fresh_event(monkeypatch):
    """Discard buffered content and keep the next generation independent of old cancellation."""
    provider, requests = build(monkeypatch, [
        stream([frame({"reasoning_content": "first"}, usage={"prompt_tokens": 11}),
                frame({"content": json.dumps(DECISION)}, finish="stop")]),
        completion(json.dumps(DECISION)),
    ])
    old_event = Event()
    events = []

    def cancel_on_delta(kind, payload):
        """Cancel immediately after one delivered reasoning chunk."""
        events.append((kind, payload))
        if kind == "model_reasoning_delta":
            provider.cancel(old_event)

    provider.event_sink = cancel_on_delta
    with pytest.raises(ProviderCancelled):
        provider.decide({}, {}, cancel_event=old_event)
    assert not any(kind == "model_content_delta" for kind, _ in events)
    assert provider._history == [] and provider._response_message is None
    assert events[-1][0] == "model_request_cancelled"
    assert events[-1][1]["usage"]["prompt_tokens"] == 11

    def cancel_old_generation(kind, _payload):
        """A delayed stop for the old event must not close the new request."""
        if kind == "model_reasoning_delta":
            provider.cancel(old_event)

    provider.event_sink = cancel_old_generation
    assert provider.decide({}, {}, cancel_event=Event()) == DECISION
    assert len(requests) == provider.requests_used == 2 and provider._history_turns == 1
    assert old_event.is_set()


@pytest.mark.parametrize("kind", ["repair", "tool", "final"])
def test_cancel_after_response_does_not_repair_or_dispatch_tools(monkeypatch, kind):
    """Stop continuation at a completed response boundary without accepting history."""
    answer = completion("invalid JSON")
    if kind == "final":
        answer = completion(json.dumps(DECISION))
    if kind == "tool":
        answer = httpx.Response(200, json={"model": "deepseek-flash", "choices": [{
            "finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [{
                "id": "lookup", "type": "function", "function": {
                    "name": "wiki_search", "arguments": '{"query":"Bash"}'}}]}}]})
    provider, requests = build(monkeypatch, [answer])
    calls = []
    if kind == "tool":
        provider.configure_tools()
        monkeypatch.setattr(provider._tools, "call", lambda *args: calls.append(args))
    cancel_event = Event()

    def cancel_on_completion(event_type, _payload):
        """Model completion is still followed by a cancellable decision-admission boundary."""
        if event_type == "model_request_completed":
            provider.cancel(cancel_event)

    provider.event_sink = cancel_on_completion
    with pytest.raises(ProviderCancelled):
        provider.decide({}, {}, cancel_event=cancel_event)
    assert len(requests) == provider.requests_used == 1 and not calls and not provider._history
    assert provider._response_message is None


def test_cancel_during_tool_stops_remaining_tools_and_continuation(monkeypatch):
    """Finish an already-running local tool without starting its sibling or another HTTP turn."""
    tool_calls = [{"id": identity, "type": "function", "function": {
        "name": "wiki_search", "arguments": '{"query":"Bash"}'}} for identity in ("first", "second")]
    answer = httpx.Response(200, json={"model": "deepseek-flash", "choices": [{
        "finish_reason": "tool_calls", "message": {"content": "", "tool_calls": tool_calls}}]})
    provider, requests = build(monkeypatch, [answer])
    provider.configure_tools()
    cancel_event = Event()
    calls = []

    def cancel_during_call(call, *_args):
        """Simulate a pause arriving while the first read-only tool is running."""
        calls.append(call["id"])
        provider.cancel(cancel_event)
        return {}

    monkeypatch.setattr(provider._tools, "call", cancel_during_call)
    with pytest.raises(ProviderCancelled):
        provider.decide({}, {}, cancel_event=cancel_event)
    assert calls == ["first"] and len(requests) == provider.requests_used == 1
    assert not provider._history and provider._response_message is None


def test_cancel_interrupts_retry_backoff(monkeypatch):
    """A cancelled transient failure never spends another request or waits out backoff."""
    provider, requests = build(monkeypatch, [httpx.Response(503)])
    failed = Event()
    provider.event_sink = lambda kind, _payload: failed.set() if kind == "model_request_failed" else None
    cancel_event = Event()
    worker, outcomes = run_decision(provider, cancel_event)
    assert failed.wait(WAIT_SECONDS)
    provider.cancel(cancel_event)
    worker.join(0.5)
    assert not worker.is_alive() and isinstance(outcomes[0], ProviderCancelled)
    assert len(requests) == provider.requests_used == 1 and not provider._history


def test_cancel_does_not_wait_for_response_cleanup(monkeypatch):
    """Keep pause controls responsive even when a custom response close blocks."""
    provider, _ = build(monkeypatch, [])
    cancel_event, entered, release = Event(), Event(), Event()

    class BlockingResponse:
        """Represent a slow third-party stream close independently of socket shutdown."""

        def close(self):
            """Signal cleanup entry and block only the daemon cleanup worker."""
            entered.set()
            release.wait(WAIT_SECONDS)

    provider._active_request = {"event": cancel_event, "response": BlockingResponse(), "stream": None}
    try:
        started = time.monotonic()
        provider.cancel(cancel_event)
        assert time.monotonic() - started < 0.5
        assert entered.wait(WAIT_SECONDS) and cancel_event.is_set()
    finally:
        release.set()

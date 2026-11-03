"""Serve one local dashboard over HTTP and resumable server-sent events."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
import webbrowser

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .telemetry import WriterLock, utc_now
from .knowledge import KnowledgeBase
from .game_launcher import GameLauncher
from .save_guard import SaveGuardError, ensure_full_unlock
from .session import SessionError
from .history import HistoryStore


# Only these local names are accepted in HTTP Host and Origin headers.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Keep idle connections alive without growing a per-client event queue.
HEARTBEAT_SECONDS = 10.0
# Static assets are installed beside this module as package data.
WEB_DIRECTORY = Path(__file__).with_name("web")


class CommandRequest(BaseModel):
    """Validate an explicit, idempotent control operation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    command_id: str = Field(min_length=1, max_length=128)
    command: Literal["start", "pause", "resume", "step", "redecide", "stop"]


class InstructionRequest(BaseModel):
    """Validate a bounded strategy instruction and its activation scope."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    command_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=3000)
    scope: Literal["next_decision", "combat", "run"] = "next_decision"
    timing: Literal["next_decision", "redecide"] = "next_decision"


class RevokeRequest(BaseModel):
    """Identify a retry-safe instruction revocation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    command_id: str = Field(min_length=1, max_length=128)


class LaunchSettingsRequest(BaseModel):
    """Allow only native starting characters and difficulty before a new run."""

    model_config = ConfigDict(extra="forbid", strict=True)
    character: Literal["IRONCLAD", "SILENT", "REGENT", "NECROBINDER", "DEFECT"] = "IRONCLAD"
    ascension: int = Field(default=0, ge=0, le=10)


class SimulationSettingsRequest(BaseModel):
    """Keep read access independent of candidate generation and search effort."""

    model_config = ConfigDict(extra="forbid", strict=True)
    read_access: Literal["public", "internal"] = "public"
    candidate_source: Literal["model", "mcts"] = "mcts"
    search_strength: float = Field(default=3, ge=1, le=5, allow_inf_nan=False)
    combat_decision_mode: Literal["llm", "assist", "upstream"] = "assist"
    run_decision_mode: Literal["llm", "assist", "upstream"] = "llm"
    rollout_policy: Literal["upstream", "heuristic"] = "upstream"
    action_prior: Literal["upstream", "off"] = "upstream"
    value_model: Literal["texel", "learned"] = "texel"
    search_profile: Literal["upstream", "base"] = "upstream"
    reward_pool: Literal["known", "full_unlock"] = "known"


class PresentationSettingsRequest(BaseModel):
    """Select spectator language before a run without changing native game settings."""

    model_config = ConfigDict(extra="forbid", strict=True)
    language: Literal["en", "zh-CN"]


def _local_request(request: Request) -> bool:
    """Require a loopback Host and an exactly matching browser origin."""
    try:
        target = urlsplit(str(request.url))
        if target.hostname not in LOCAL_HOSTS:
            return False
        origin = request.headers.get("origin")
        if origin is None:
            return True
        source = urlsplit(origin)
        if source.username is not None or source.password is not None:
            return False
        if source.path not in {"", "/"} or source.query or source.fragment:
            return False
        source_port = source.port or (443 if source.scheme == "https" else 80)
        target_port = target.port or (443 if target.scheme == "https" else 80)
        return (
            source.scheme == target.scheme
            and source.hostname == target.hostname
            and source.hostname in LOCAL_HOSTS
            and source_port == target_port
        )
    except ValueError:
        return False


def _event_cursor(after: int | None, last_event_id: str | None) -> int:
    """Prefer EventSource's reconnect cursor over its original query string."""
    if last_event_id is None:
        return after or 0
    try:
        cursor = int(last_event_id)
    except ValueError:
        raise HTTPException(422, "Last-Event-ID must be a nonnegative integer.") from None
    if cursor < 0:
        raise HTTPException(422, "Last-Event-ID must be a nonnegative integer.")
    return cursor


def _encode_sse(event: dict, event_name: str = "event") -> str:
    """Encode public event data without allowing payload newlines into framing."""
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {int(event['seq'])}\nevent: {event_name}\ndata: {data}\n\n"


async def _event_stream(request: Request, session, after: int):
    """Copy bounded history for each client while all control work stays separate."""
    heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
    while not await request.is_disconnected():
        current = session() if callable(session) else session
        events = await asyncio.to_thread(current.wait_for_events, after, 1.0)
        if events is None:
            snapshot = await asyncio.to_thread(current.snapshot)
            after = int(snapshot.get("seq", 0))
            reset = {
                "seq": after,
                "type": "reset",
                "timestamp_utc": utc_now(),
                "payload": {"snapshot_required": True},
            }
            yield _encode_sse(reset, "reset")
            heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
        else:
            for event in events:
                if int(event["seq"]) > after:
                    after = int(event["seq"])
                    yield _encode_sse(event)
                    heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
            if not events:
                # A stopped session wakes immediately while its replacement is loading the game.
                await asyncio.sleep(0.1)
        if time.monotonic() >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS


def create_app(config: dict, root: Path, *, session=None) -> FastAPI:
    """Bind one session to the server lifetime, independently of page requests."""
    launcher = GameLauncher(config, root, session)
    history_store = HistoryStore(root)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Own one dashboard process lock and stop its worker on every exit."""
        with WriterLock(Path(root) / "local" / "dashboard.lock"):
            if launcher.session is None:
                from .session import DashboardSession
                launcher.session = DashboardSession(config, root)
            _app.state.launcher = launcher
            try:
                await asyncio.to_thread(launcher.session.start_background)
                yield
            finally:
                await asyncio.to_thread(launcher.shutdown)

    app = FastAPI(title="STS2 Agent Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.session = session
    app.state.launcher = launcher

    def knowledge_base():
        """Browse the session's frozen corpus without starting a model request."""
        return getattr(getattr(launcher.session, "provider", None), "knowledge", None) or KnowledgeBase(root)

    @app.get("/knowledge")
    def knowledge_index():
        """Serve the local wiki reader independently of online source availability."""
        return FileResponse(WEB_DIRECTORY / "knowledge.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/knowledge")
    def knowledge_search(q: str = "", kind: str = "", limit: int = 20):
        """Return a bounded human-facing search over approved static references."""
        corpus = knowledge_base()
        return {"metadata": corpus.metadata(),
                "results": corpus.search(q[:500], kind=kind or None, limit=max(1, min(limit, 100)))}

    @app.get("/api/knowledge/{kind}/{entity_id}")
    def knowledge_entity(kind: str, entity_id: str):
        """Read an entity without exposing source paths as filesystem operations."""
        entry = knowledge_base().lookup(kind, entity_id)
        if entry is None:
            raise HTTPException(404, "Knowledge entity was not found in this snapshot.")
        return entry

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        """Reject external hosts and browser requests from unrelated origins."""
        if not _local_request(request):
            return JSONResponse({"detail": "Only same-origin loopback requests are allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def invalid_operation(_request: Request, _error: ValueError):
        """Return a fixed validation message without exception details or config."""
        return JSONResponse({"detail": "Invalid dashboard operation."}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def conflicting_operation(_request: Request, _error: RuntimeError):
        """Expose session conflicts without leaking underlying exception context."""
        return JSONResponse({"detail": "The operation conflicts with the current session state."}, status_code=409)

    @app.exception_handler(SessionError)
    async def session_conflict(_request: Request, error: SessionError):
        """Show curated operator guidance while keeping arbitrary backend exceptions private."""
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/")
    def index():
        """Return the installed dashboard shell without starting another agent."""
        return FileResponse(WEB_DIRECTORY / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/app.js")
    def javascript():
        """Return only the fixed application script."""
        return FileResponse(WEB_DIRECTORY / "app.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/styles.css")
    def stylesheet():
        """Return only the fixed application stylesheet."""
        return FileResponse(WEB_DIRECTORY / "styles.css", media_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/observatory.js")
    def observatory_javascript():
        """Serve the fixed visual shell without exposing arbitrary filesystem paths."""
        return FileResponse(WEB_DIRECTORY / "observatory.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/i18n.js")
    def presentation_javascript():
        """Serve the fixed spectator translation catalog without arbitrary file access."""
        return FileResponse(WEB_DIRECTORY / "i18n.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/timeline.js")
    def timeline_javascript():
        """Serve the read-only correlated history client."""
        return FileResponse(WEB_DIRECTORY / "timeline.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    def run_history(run_id: str):
        """Localize missing archive errors to the optional history reader."""
        try:
            return history_store.get(run_id)
        except (KeyError, OSError, json.JSONDecodeError):
            raise HTTPException(404, "该局历史日志不可用。") from None

    @app.get("/api/history")
    def history_page(run_id: str, before: int | None = Query(None, ge=0),
                     limit: int = Query(30, ge=1, le=100)):
        """Page complete public decision history independently of the live snapshot window."""
        return run_history(run_id).page(before, limit)

    @app.get("/api/history-events")
    def history_events(run_id: str, before: int | None = Query(None, ge=0),
                       limit: int = Query(50, ge=1, le=100)):
        """Page unassociated session events without inventing decision linkage."""
        return run_history(run_id).events(before, limit)

    @app.get("/api/history/{decision_id}")
    def history_detail(decision_id: str, run_id: str):
        """Load original request text and public evidence only when a group is expanded."""
        try:
            return run_history(run_id).detail(decision_id)
        except KeyError:
            raise HTTPException(404, "该决策的历史记录不存在。") from None

    @app.get("/api/snapshot")
    def snapshot():
        """Read the existing public session snapshot."""
        return launcher.snapshot()

    @app.get("/api/health")
    def health():
        """Identify this project so a double-click can reuse its existing local server."""
        return {"app": "sts2-agent-dashboard", "project_root": str(Path(root).resolve())}

    @app.post("/api/game/start")
    def start_game(body: RevokeRequest):
        """Prepare the game and archive only completed sessions, without starting the policy."""
        return {"ok": True, "receipt": launcher.start(body.command_id)}

    @app.post("/api/game/settings")
    def launch_settings(body: LaunchSettingsRequest):
        """Save an explicit starting policy; the native menu is aligned at normal admission."""
        return {"ok": True, "launch_settings": launcher.set_launch_settings(body.model_dump())}

    @app.post("/api/service/stop")
    def stop_service():
        """Exit only a paused, fully settled local service so deployment needs no process kill."""
        with launcher.lock:
            view = launcher.session.snapshot()
            if (view.get("desired_state") != "paused" or view.get("inflight") or view.get("request_pending")
                    or view.get("decision_pending")):
                raise HTTPException(409, "Pause and wait for all requests and receipts before stopping the service.")
            server = getattr(app.state, "server", None)
            if server is None:
                raise HTTPException(503, "This host does not expose service shutdown.")
            server.should_exit = True
            return {"ok": True, "status": "stopping"}

    @app.post("/api/presentation/settings")
    def presentation_settings(body: PresentationSettingsRequest):
        """Apply only an explicit, paused pre-run spectator language preference."""
        return {"ok": True, "presentation_settings": launcher.set_presentation_settings(body.language)}

    @app.post("/api/simulation/settings")
    def simulation_settings(body: SimulationSettingsRequest):
        """Update the settled paused session and retain its consumed budgets."""
        with launcher.lock:
            settings = launcher.session.configure_simulation(body.model_dump(exclude_unset=True))
            launcher.config.setdefault("simulation", {}).update(settings)
            return {"ok": True, "simulation_settings": {**settings, "editable": True}}

    @app.get("/api/events")
    async def events(request: Request, after: int | None = None):
        """Resume public events using EventSource's last confirmed sequence."""
        if after is not None and after < 0:
            raise HTTPException(422, "after must be a nonnegative integer.")
        cursor = _event_cursor(after, request.headers.get("last-event-id"))
        return StreamingResponse(
            _event_stream(request, lambda: launcher.session, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/commands")
    def command(body: CommandRequest):
        """Forward an explicit command to the single session command ledger."""
        with launcher.lock:
            return {"ok": True, "receipt": launcher.require_ready().command(body.command_id, body.command)}

    @app.post("/api/instructions")
    def instruction(body: InstructionRequest):
        """Queue one instruction without accessing model credentials."""
        with launcher.lock:
            return {"ok": True, "receipt": launcher.require_ready().instruction(body.command_id, body.text, body.scope, body.timing)}

    @app.post("/api/instructions/{instruction_id}/revoke")
    def revoke(instruction_id: str, body: RevokeRequest):
        """Remove an instruction from future decision snapshots."""
        with launcher.lock:
            return {"ok": True, "receipt": launcher.require_ready().revoke(body.command_id, instruction_id)}

    return app


def serve(config: dict, root: Path, host: str = "127.0.0.1", port: int = 18766, open_browser: bool = True) -> None:
    """Run one local server and optionally open its browser after it is listening."""
    import uvicorn

    if host not in LOCAL_HOSTS:
        raise ValueError("Dashboard host must be localhost, 127.0.0.1, or ::1.")
    if not 1 <= port <= 65535:
        raise ValueError("Dashboard port must be between 1 and 65535.")
    # The dashboard must never supervise a run on a profile that lost the full-unlock state.
    try:
        guard = ensure_full_unlock(Path(root))
    except SaveGuardError as error:
        raise RuntimeError(f"未使用全解锁存档：{error}") from None
    if guard["status"] == "restored":
        print(f"已恢复全解锁主档：{guard['progress']}（原档备份：{guard['backup']}）", flush=True)
    app = create_app(config, root)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, workers=1, reload=False,
                                         log_level="info", timeout_graceful_shutdown=5))
    app.state.server = server
    stopped = threading.Event()

    def open_when_started():
        """Wait for successful startup before opening a single browser tab."""
        while not stopped.wait(0.1):
            if server.started:
                url_host = f"[{host}]" if ":" in host else host
                webbrowser.open(f"http://{url_host}:{port}/")
                return

    if open_browser:
        threading.Thread(target=open_when_started, name="dashboard-browser", daemon=True).start()
    try:
        server.run()
    finally:
        stopped.set()

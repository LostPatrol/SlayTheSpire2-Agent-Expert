"""One persistent controller for dashboard commands, policy decisions, and native actions."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
import uuid

from .bridge import Bridge, BridgeError
from .automatic import automatic_decision
from .instructions import InstructionBook
from .memory import PublicMemory, SCENE_OVERLAYS
from .model_tools import SIMULATION_TOOL_SUMMARY_PROFILE, bounded_tool_result
from .provider import DECISION_SCHEMA, Provider, ProviderCancelled, ProviderError, decision_prompt
from .presentation import (DEFAULT_LANGUAGE, PRESENTATION_LANGUAGES, display_text,
                           language_from_config, presentation_run_active, saved_language)
from .public import ActionError, configured_start, observation, validate_action
from .public_history import PublicCombatHistory, observation_coverage
from .rules import verify_selected_plan
from .simulation import CombatSimulator, SIMULATION_SETTINGS
from .strategies import StrategyLibrary
from .telemetry import Journal, TRANSITION_SCHEMA_VERSION, WriterLock, read_jsonl, utc_now, write_json


# Reconnection retains a bounded event window; the full append-only journal remains on disk.
EVENT_WINDOW = 2000
# Long model texts stay available for the latest requests; complete deltas remain in the journal.
DISPLAY_REQUESTS = 8
# Completed explanations survive high-volume token events and browser refreshes.
DISPLAY_DECISIONS = 40
# Cached proposals must be reconsidered when admission semantics or rule artifacts change.
CACHED_PROPOSAL_RULES_VERSION = "public-admission-tools-scenes-counter-v3"
# Compact tool history is displayed; complete arguments/results remain in the journal.
DISPLAY_TOOLS = 40
# Observing and controls are independent of the model's blocking network operation.
OBSERVE_INTERVAL = 0.35
# These derived UI updates already have replay evidence and need no separate ledger replacement.
DISPLAY_ONLY_EVENTS = frozenset({"simulation", "calculation", "tool_call_started"})
# Progress events repeat constantly and carry no durable fact; everything else writes immediately.
COALESCED_EVENTS = frozenset({"observation", "model_request_started", "strategy_scene_changed"})
# These facts must reach the ledger before the controller acts on them.
PERSIST_INTERVAL_SECONDS = 0.3
# Only these native transitions end a public combat encounter.
ROOM_SCREENS = frozenset({"MAP", "REWARD", "EVENT", "SHOP", "REST", "CHEST", "GAME_OVER"})
# Token fields copied from provider usage; absent values remain unknown in request records.
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


class SessionError(RuntimeError):
    """A short operator-facing error without credentials or raw game payloads."""


def _fingerprint(state: dict) -> str:
    """Identify meaningful public facts without timestamps, gate sampling, or read identifiers."""
    public = deepcopy(state)
    for key in ("action_revision", "protocol_version", "supported", "available_actions"):
        public.pop(key, None)
    if isinstance(public.get("combat"), dict):
        public["combat"].pop("action_readiness", None)
    return hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _action_label(action: dict, obs: dict, language: str = DEFAULT_LANGUAGE) -> str:
    """Capture names from the decision's own public frame for later readable history."""
    state = obs.get("state", {})
    kind = action.get("type", "")
    if kind == "skip_selection":
        return display_text(language, "skip_selection")
    label = kind
    if kind == "play_card":
        combat = state.get("combat", {})
        card = next((item for item in combat.get("hand", [])
                     if item.get("i") == action.get("card_index")), {})
        label = display_text(language, "play_card", name=str(
            card.get("name") or card.get("line") or card.get("card_id") or kind))
        if "target_index" in action:
            enemy = next((item for item in combat.get("enemies", [])
                          if item.get("i") == action["target_index"]), {})
            label += " → " + str(enemy.get("name") or enemy.get("enemy_id") or action["target_index"])
    return label


def archive_terminal_session(root: Path) -> dict:
    """Preserve a finished ledger before an explicit new experiment, with no budget reset in place."""
    root = Path(root).resolve()
    with WriterLock(root / "local/dashboard.lock"), WriterLock(root / "local/controller.lock"):
        return _archive_terminal_ledger(root)


def reconcile_completed_action(config: dict, root: Path, action_id: str) -> dict:
    """Reconcile historical completion offline while excluding both service writers."""
    root = Path(root).resolve()
    with WriterLock(root / "local/dashboard.lock"), WriterLock(root / "local/controller.lock"):
        if not (root / "local/dashboard-session.json").exists():
            raise SessionError("No persisted session to reconcile.")
        session = DashboardSession(config, root)
        try:
            return session.reconcile_completed(action_id)
        finally:
            session.bridge.client.close()
            session.simulator.close()
            session.provider.close()


def _archive_terminal_ledger(root: Path) -> dict:
    """Archive only under the caller's dashboard and controller locks, with its worker stopped."""
    ledger = root / "local/dashboard-session.json"
    if not ledger.exists():
        return {"status": "ready", "archived_run_id": None}
    state = json.loads(ledger.read_text(encoding="utf-8"))
    if (root / "local/pending-action.json").exists() or state.get("inflight") or state.get("request_pending"):
        raise SessionError("Reconcile pending work before creating a new experiment.")
    if not state.get("terminal_evidence"):
        raise SessionError("Only a recorded terminal run can be archived by new-session.")
    archive = root / "local/session-archives" / (
        str(state["run_id"]) + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + ".json")
    archive.parent.mkdir(parents=True, exist_ok=True)
    ledger.replace(archive)
    return {"status": "ready", "archived_run_id": state["run_id"], "archive": str(archive)}


class DashboardSession:
    """Retain memory, budgets, commands, and a single serialized game-action admission gate."""

    def __init__(self, config: dict, root: Path, *, bridge=None, provider=None):
        """Restore one local session paused; constructing a view never starts a policy request."""
        self.root = Path(root).resolve()
        self.config = deepcopy(config)
        settings_path = self.root / "local/simulation-settings.json"
        if settings_path.exists():
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            base = CombatSimulator.settings_from_config(self.config)
            configured = self.config.get("simulation", {})
            for key in ("combat_decision_mode", "run_decision_mode"):
                if key in configured and key not in saved:
                    # An old candidate alias cannot replace an explicitly configured phase owner.
                    saved[key] = configured[key]
            legacy_enabled = configured.get("enabled", False) is True and self.config.get("policy", {}).get("deliberation") != "calculation_only"
            if not legacy_enabled and not any(key in saved for key in ("combat_decision_mode", "run_decision_mode")):
                saved = {**saved, "combat_decision_mode": "llm", "run_decision_mode": "llm"}
            saved = CombatSimulator.normalize_settings(saved, base)
            self.config.setdefault("simulation", {}).update(saved)
        self._condition = threading.Condition(threading.RLock())
        self._shutdown = threading.Event()
        self._thread = None
        self._writer = None
        self._reset_provider = False
        self._prepare = False
        self._step_permit = 0
        self._cached = None
        # Cancellation belongs to one generation and is never cleared by resume.
        self._generation_cancel = None
        self._generation_id = None
        self._prediction_executor = None
        self._prediction_actions = set()
        self._state_file = self.root / "local" / "dashboard-session.json"
        # Progress events coalesce; durable facts write immediately and clear the pending flag.
        self._last_persist = 0.0
        self._persist_pending = False
        if self._state_file.exists():
            self._state = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._state["desired_state"] = "paused"
            self._state["phase"] = "paused"
            # A stopped process cannot promise completion of an HTTP request it no longer owns.
            if self._state.get("request_pending"):
                self._state["request_pending"] = False
                self._state["metrics"]["unknown_usage_requests"] += 1
                for request in self._state["model_requests"]:
                    if request.get("status") in {"generating", "cancelling"}:
                        request["status"] = "interrupted"
            self._cached = self._state.pop("cached_decision", None)
        else:
            self.config.setdefault("presentation", {})["language"] = saved_language(
                self.root, language_from_config(self.config))
            self._state = self._new_state()
        # The current ledger wins over a newer next-run preference, including older Chinese runs.
        language = self._state.get("presentation_language", DEFAULT_LANGUAGE)
        self._state["presentation_language"] = (language if isinstance(language, str)
            and language in PRESENTATION_LANGUAGES else DEFAULT_LANGUAGE)
        self.config.setdefault("presentation", {})["language"] = self._state["presentation_language"]
        self._state["decision_pending"] = False
        if self._state.get("launch_policy"):
            # A resumed run keeps the starting policy recorded with its ledger.
            self.config.setdefault("policy", {}).update(configured_start(self._state["launch_policy"]))
        self._public_history = PublicCombatHistory(self._state.get("public_history_tracker"))
        self._state["metrics"].update(max_requests=config["limits"]["max_provider_requests_per_run"],
            max_actions=config["limits"]["max_actions"], max_wall_seconds=config["limits"]["max_run_wall_minutes"] * 60)
        self._journal = Journal(self.root / "runs" / self._state["run_id"], resume=True)
        if self._state.get("terminal_evidence") and self._state.get("terminal_elapsed_seconds") is None:
            # Older terminal ledgers already recorded their final elapsed time in this run's result.
            try:
                result = json.loads((self._journal.directory / "result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            wall = result.get("wall_seconds")
            if (result.get("run_id") == self._state["run_id"] and result.get("status") == "native_game_over"
                    and type(wall) in (int, float) and wall >= 0):
                self._state["terminal_elapsed_seconds"] = wall
        self._events = deque(maxlen=EVENT_WINDOW)
        event_path = self._journal.directory / "dashboard-events.jsonl"
        if event_path.exists():
            recovery_issues = []
            displayed_requests = {item["model_request_id"]: item for item in self._state.get("model_requests", [])}
            for event in read_jsonl(event_path, recovery_issues):
                self._events.append(event)
                if event.get("type") in {"model_reasoning_delta", "model_content_delta"}:
                    payload = event.get("payload", {})
                    request = displayed_requests.get(payload.get("model_request_id"))
                    if request is not None and payload.get("text"):
                        # Older ledgers lack the final text time; recover it from their exact events.
                        request["last_delta_utc"] = payload.get("last_delta_utc") or event["timestamp_utc"]
                        if payload.get("first_delta_utc"):
                            request.setdefault("first_delta_utc", payload["first_delta_utc"])
            if recovery_issues:
                self._state["journal_issues"] = recovery_issues
                self._journal.event("journal-recovery", {"issues": recovery_issues})
            if self._events:
                self._state["seq"] = max(self._state["seq"], self._events[-1]["seq"])
        if "decision_history" not in self._state:
            # Older ledgers have a compact decision journal independent of streaming text.
            history_path = self._journal.directory / "decisions.jsonl"
            self._state["decision_history"] = []
            if history_path.exists():
                for item in deque(read_jsonl(history_path), maxlen=DISPLAY_DECISIONS):
                    if isinstance(item, dict) and isinstance(item.get("action"), dict):
                        item["status"] = "historical_proposal"
                        self._state["decision_history"].append(item)
        self.instructions = InstructionBook(self._state.get("instructions", []))
        self.memory = PublicMemory(self._state["run_id"])
        self.memory.events = deepcopy(self._state.get("memory_events", []))
        self.memory.set_plan(self._state.get("plan", ""))
        if self._state.get("strategy_memory_version") == 2:
            self.memory.scene_key = self._state.get("scene_scope")
            self.memory.set_scene_plan(self._state.get("scene_plan", ""))
        else:
            # Legacy plans mixed expired room tactics with build ideas; do not revive them.
            self.memory.set_plan("")
            self._state["plan"] = ""
        self._state["strategy_memory_version"] = 2
        self._restore_public_map()
        self.bridge = bridge or Bridge(self.config)
        identity = self._state.get("bridge_identity")
        if identity:
            self.bridge.identity = tuple(identity)
        self.provider = provider or Provider(self.config, self.root,
            knowledge_snapshot_id=self._state.get("knowledge_snapshot_id"))
        knowledge = getattr(self.provider, "knowledge", None)
        if knowledge is not None:
            self._state.setdefault("knowledge_snapshot_id", knowledge.snapshot_id)
            self._state["knowledge"] = knowledge.metadata()
        self.simulator = CombatSimulator(self.config, self.root)
        self._simulation_rules = self.simulator.rule_identity()
        self._track_simulation_settings(self.simulator.settings,
            self._state.get("simulation_settings", self.simulator.settings), "session_restore")
        self.provider.event_sink = self._provider_event
        if hasattr(self.provider, "configure_tools"):
            self.provider.configure_tools(simulator=self.simulator,
                history=lambda: deepcopy(self.memory.events), strategies=StrategyLibrary(self.root),
                before_request=self._check_model_budget)
        self.provider.requests_used = self._state["metrics"]["requests_used"]
        if hasattr(self.provider, "_started"):
            self.provider._started = time.monotonic() - self._elapsed()
        if not (self._journal.directory / "manifest.json").exists():
            self._journal.document("manifest.json", {"run_id": self._state["run_id"],
                "started_utc": self._state["started_utc"], "phase": "dashboard_smoke",
                "config": self.config, "policy": self.config["policy"], "model_mode": config["provider"]["model"],
                "reload_mode": config["policy"]["mode"], "assistance_mode": "autonomous",
                "manual_gameplay_intervention": False,
                "scope": "Live controller session; completion and assistance are recorded independently."})
        self._sync_manifest()
        self._discard_rule_stale_proposal()
        if self._cached is None and self._state.get("inflight") is None:
            for record in self.instructions.records:
                if record.get("status") == "injected" and record.get("scope") == "next_decision":
                    self.instructions.release(record["decision_id"])

    def _new_state(self) -> dict:
        """Create a ledger once, independently of browser tabs and HTTP requests."""
        return {"seq": 0, "run_id": str(uuid.uuid4()), "started_utc": utc_now(), "budget_started_utc": None,
            "desired_state": "paused", "phase": "paused", "request_pending": False, "inflight": None,
            "connection": {"connected": False}, "observation": None, "decision": None,
            "model_requests": [], "instructions": [], "control_revision": 0, "decision_epoch": 0,
            "decision_episode_id": None, "episode_count": 0, "public_fingerprint": None,
            "next_combat_decision_counter": 0, "combat_counter_episode": None,
            "combat_decision_counter_source": "session_public_decision_episodes",
            "encounter_id": None, "assistance_mode": "autonomous", "commands": {},
            "finished_requests": [], "bridge_identity": None, "error": None, "plan": "",
            "presentation_language": language_from_config(self.config),
            "strategy_memory_version": 2, "scene_scope": None, "scene_plan": "", "tool_calls": [],
            "metrics": {"requests_used": 0, "actions_completed": 0, "retries": 0,
                "format_repairs": 0, "unknown_usage_requests": 0, **{key: 0 for key in TOKEN_FIELDS}}}

    def _proposal_rule_identity(self) -> dict:
        """Bind reusable proposals to semantic policy, knowledge and actual simulation rules."""
        schema = json.dumps(DECISION_SCHEMA, sort_keys=True, separators=(",", ":"))
        simulation = self.simulator.rule_identity()
        if not isinstance(simulation, dict):
            # Test doubles may replace the optional client without implementing its provenance API.
            simulation = self._simulation_rules
        return {"admission_rules_version": CACHED_PROPOSAL_RULES_VERSION,
                "prompt_sha256": hashlib.sha256(decision_prompt(self.config).encode("utf-8")).hexdigest(),
                "action_schema_sha256": hashlib.sha256(schema.encode("utf-8")).hexdigest(),
                "simulation_tool_encoding": (SIMULATION_TOOL_SUMMARY_PROFILE
                    if self.config.get("provider", {}).get(
                    "compact_simulation_tool_results", False) else "plain-json"),
                "model_view": self.config.get("provider", {}).get("model_view", "off"),
                "simulation": deepcopy(simulation),
                "knowledge_snapshot_id": self._state.get("knowledge_snapshot_id"),
                "policy": deepcopy(self.config.get("policy", {}))}

    def _discard_rule_stale_proposal(self) -> bool:
        """Retire only an unsubmitted stale proposal, preserving episodes, spending and native work."""
        if (not self._cached or self._state.get("terminal_evidence") or self._state.get("inflight")
                or (self.root / "local/pending-action.json").exists()
                or self._cached.get("rules_identity") == self._proposal_rule_identity()):
            return False
        decision_id = self._cached["decision_id"]
        self.instructions.release(decision_id)
        self._cached = None
        self._state["decision"] = None
        self.provider.reset_context("decision_rules_changed")
        self._emit("decision_discarded", {"decision_id": decision_id, "reason": "decision_rules_changed"})
        return True

    def _restore_public_map(self) -> None:
        """Recover only this run's observed graph, preserving all event and budget ledgers."""
        self.memory.restore_public_map(self._state.get("public_map"))
        if self.memory.public_map is None:
            # Older ledgers did not persist maps; this journal contains reviewed observations only.
            for record in read_jsonl(self._journal.directory / "public-events.jsonl"):
                observed = record.get("observation")
                if isinstance(observed, dict) and observed.get("run_id") == self._state["run_id"]:
                    self.memory.remember_public_map(observed)
        current = self._state.get("observation")
        if isinstance(current, dict) and current.get("run_id") == self._state["run_id"]:
            self.memory.remember_public_map(current)

    def _elapsed(self) -> float:
        """Keep paused/restarted wall time cumulative and stop counting after a recorded terminal."""
        if self._state.get("terminal_elapsed_seconds") is not None:
            return self._state["terminal_elapsed_seconds"]
        if not self._state.get("budget_started_utc"):
            return 0
        started = datetime.fromisoformat(self._state["budget_started_utc"])
        return max(0, (datetime.now(timezone.utc) - started).total_seconds())

    def _check_model_budget(self) -> None:
        """Apply cumulative session limits before each tool continuation API request."""
        with self._condition:
            if self._budget_exhausted():
                raise ProviderError("Model request budget exhausted during tool deliberation.")

    def _persist(self) -> None:
        """Save command and budget state before acknowledging effects to an operator."""
        started = time.perf_counter()
        self._state["instructions"] = self.instructions.snapshot() if hasattr(self, "instructions") else []
        self._state["memory_events"] = deepcopy(self.memory.events) if hasattr(self, "memory") else []
        self._state["public_map"] = self.memory.public_map if hasattr(self, "memory") else None
        if hasattr(self, "memory"):
            self._state["scene_scope"] = self.memory.scene_key
            self._state["scene_plan"] = self.memory.scene_plan
        self._state["cached_decision"] = deepcopy(self._cached)
        write_json(self._state_file, self._state)
        self._last_persist = time.monotonic()
        self._persist_pending = False
        self._record_timing("ledger_persist", started)

    def _record_timing(self, stage: str, started: float, context: dict | None = None,
                       details: dict | None = None) -> None:
        """Record inclusive diagnostic spans without an extra durable ledger replacement."""
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        with self._condition:
            summary = self._state.setdefault("performance", {}).setdefault(stage,
                {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
            summary["count"] += 1
            summary["total_ms"] = round(summary["total_ms"] + elapsed, 3)
            summary["max_ms"] = max(summary["max_ms"], elapsed)
            summary["last_ms"] = elapsed
            try:
                self._journal.event("stage-timings", {"run_id": self._state["run_id"],
                    "stage": stage, "elapsed_ms": elapsed, "inclusive": True,
                    **(context or {}), "details": details or {}})
            except OSError:
                # Optional timing loss must not mask an execution or persistence outcome.
                logging.warning("Could not record controller stage timing", exc_info=True)

    @contextmanager
    def _timed(self, stage: str, context: dict | None = None):
        """Measure a controller span, including its failures, with optional diagnostic counters."""
        started, details = time.perf_counter(), {}
        try:
            yield details
        finally:
            self._record_timing(stage, started, context, details)

    def _emit(self, event_type: str, payload: dict, *, persist: bool = True) -> dict:
        """Append sequence-numbered facts; slow SSE readers never own this controller lock."""
        with self._condition:
            self._state["seq"] += 1
            event = {"seq": self._state["seq"], "timestamp_utc": utc_now(),
                     "type": event_type, "payload": {"run_id": self._state["run_id"], **deepcopy(payload)}}
            history = self._state.setdefault("decision_history", [])
            if event_type == "decision":
                history.append({**deepcopy(payload), "timestamp_utc": event["timestamp_utc"],
                                "status": "proposed"})
                del history[:-DISPLAY_DECISIONS]
            elif event_type in {"decision_discarded", "decision_rejected", "action_submitted",
                                "action_completed", "action_rejected"}:
                # Only native acceptance and settlement mark a proposal as executed.
                decision_id = payload.get("decision_id")
                if decision_id:
                    entry = next((item for item in reversed(history)
                                  if item.get("decision_id") == decision_id), None)
                    if entry is not None:
                        entry["status"] = {"decision_discarded": "discarded",
                            "decision_rejected": "rejected", "action_submitted": "submitted",
                            "action_completed": "executed", "action_rejected": "rejected"}[event_type]
            self._events.append(event)
            self._journal.event("dashboard-events", {key: value for key, value in event.items() if key != "timestamp_utc"})
            if event_type == "decision":
                self._trace("decision", {"decision": payload,
                    "observation": self._cached["observation"] if self._cached else None}, payload)
            elif event_type in {"action_submitted", "action_completed", "action_rejected", "error",
                                "decision_discarded", "decision_rejected"}:
                self._trace(event_type, payload, payload)
            if persist and event_type not in DISPLAY_ONLY_EVENTS:
                if (event_type in COALESCED_EVENTS
                        and time.monotonic() - self._last_persist < PERSIST_INTERVAL_SECONDS):
                    # A repeated progress frame rides along with the next durable write.
                    self._persist_pending = True
                else:
                    self._persist()
            self._condition.notify_all()
            return event

    def _trace(self, event_type: str, data: dict, context: dict | None = None) -> None:
        """Persist a public replay envelope independently of the dashboard's token event stream."""
        context = context or {}
        identity = self._state.get("bridge_identity") or [None, None]
        keys = ("decision_id", "decision_episode_id", "decision_epoch", "action_id", "model_request_id")
        observed = data.get("observation", {}).get("state")
        if isinstance(observed, dict):
            data = {**data, "observation_coverage": observation_coverage(observed)}
        self._journal.event("transitions", {"schema_version": TRANSITION_SCHEMA_VERSION, "event_id": str(uuid.uuid4()),
            "event_type": event_type, "run_id": self._state["run_id"],
            "bridge_identity": {"pid": identity[0], "instance_id": identity[1]},
            **{key: context[key] for key in keys if key in context}, "data": deepcopy(data)})

    def snapshot(self) -> dict:
        """Return public display state without provider credentials or internal transport objects."""
        with self._condition:
            view = {key: deepcopy(value) for key, value in self._state.items() if key not in {
                "commands", "memory_events", "cached_decision", "finished_requests", "bridge_identity", "public_fingerprint",
                "public_history_tracker"}}
            view["instructions"] = self.instructions.snapshot()
            view["generation_cancelling"] = bool(self._generation_cancel and self._generation_cancel.is_set())
            view["metrics"]["elapsed_seconds"] = round(self._elapsed(), 2)
            metrics = view["metrics"]
            denominator = metrics["prompt_cache_hit_tokens"] + metrics["prompt_cache_miss_tokens"]
            metrics["cache_hit_rate"] = metrics["prompt_cache_hit_tokens"] / denominator if denominator else None
            view["status"] = ("pause_pending" if view["inflight"] else "paused") if view["desired_state"] == "paused" else view["phase"]
            if view.get("error"):
                view["status"] = "error"
            if view["phase"] in {"budget_stop", "game_over", "stopped"}:
                view["status"] = view["phase"]
            view["events"] = deepcopy(list(self._events)[-100:])
            view["simulation_settings"] = {**deepcopy(self._state["simulation_settings"]),
                "editable": self._state.get("desired_state") == "paused"
                and not self._state.get("request_pending") and not self._state.get("inflight")
                and not self._state.get("decision_pending")
                and not (self.root / "local/pending-action.json").exists()}
            network_identity = self.simulator.network_identity()
            if isinstance(network_identity, dict):
                view["network_identity"] = network_identity
            return view

    def events_after(self, after: int) -> list[dict] | None:
        """Replay by sequence or ask a reader outside the retained window to obtain a snapshot."""
        with self._condition:
            if after > self._state["seq"]:
                return None
            if self._events and after < self._events[0]["seq"] - 1:
                return None
            return deepcopy([event for event in self._events if event["seq"] > after])

    def wait_for_events(self, after: int, timeout: float = 1) -> list[dict] | None:
        """Wait on the event condition without holding the model or native action worker."""
        with self._condition:
            self._condition.wait_for(lambda: self._state["seq"] > after or self._shutdown.is_set(), timeout)
            return self.events_after(after)

    def _existing_command(self, command_id: str, signature: str) -> dict | None:
        """Reject changing the meaning of a command ID while returning exact duplicate receipts."""
        if not command_id or len(command_id) > 150:
            raise ValueError("A short command_id is required.")
        existing = self._state["commands"].get(command_id)
        if existing and existing["signature"] != signature:
            raise SessionError("This command_id already names another command.")
        return deepcopy(existing["receipt"]) if existing else None

    def _receipt(self, command_id: str, signature: str, **fields) -> dict:
        """Persist a command receipt and its ordering revision before returning it."""
        self._state["control_revision"] += 1
        receipt = {"command_id": command_id, "status": "accepted",
                   "control_revision": self._state["control_revision"], **fields}
        self._state["commands"][command_id] = {"signature": signature, "receipt": receipt}
        self._emit("control_command", receipt)
        return deepcopy(receipt)

    def command(self, command_id: str, command: str) -> dict:
        """Cancel unfinished generation promptly while retaining already completed proposals."""
        with self._condition:
            signature = "command:" + command
            existing = self._existing_command(command_id, signature)
            if existing:
                return existing
            if command not in {"start", "pause", "resume", "step", "redecide", "stop"}:
                raise ValueError("Unsupported controller command.")
            if command in {"resume", "step", "redecide"} and (
                self._native_budget_exhausted()
                or (self._budget_exhausted() and (self._cached is None or command == "redecide"))
            ):
                raise SessionError("The persisted session budget is exhausted.")
            if command in {"resume", "step", "redecide"} and not self._state.get("budget_started_utc"):
                self._state["budget_started_utc"] = utc_now()
                if hasattr(self.provider, "_started"):
                    self.provider._started = time.monotonic()
            if command == "start":
                self._state["desired_state"] = "paused"
                self._state["phase"] = "paused"
                self._state["error"] = None
            elif command in {"pause", "stop"}:
                self._state["desired_state"] = "paused"
                self._step_permit = 0
                self._prepare = False
                self._cancel_generation()
                if command == "stop":
                    self._state["phase"] = "stopped"
            elif command == "resume":
                self._state["desired_state"] = "running"
                self._state["error"] = None
            elif command == "step":
                if self._state["inflight"] or self._step_permit:
                    raise SessionError("A native step is already admitted; wait for its receipt.")
                self._state["desired_state"] = "paused"
                self._state["error"] = None
                self._step_permit = 1
                self._prepare = True
            elif command == "redecide":
                self._invalidate_decision()
            receipt = self._receipt(command_id, signature, command=command,
                                    desired_state=self._state["desired_state"])
            if command in {"pause", "stop"} and not self._state["request_pending"] and not self._state["inflight"]:
                self._write_result("user_paused")
            return receipt

    def _cancel_generation(self) -> None:
        """Interrupt model transport under the control gate without waiting for network cleanup."""
        if self._generation_cancel is None:
            return
        self._generation_cancel.set()
        for request in self._state["model_requests"]:
            if request.get("decision_id") == self._generation_id and request.get("status") == "generating":
                request["status"] = "cancelling"
        self.provider.cancel(self._generation_cancel)

    def _discard_cancelled_generation(self, decision_id: str) -> None:
        """Release incomplete work without consuming guidance or a later resume/step command."""
        self.instructions.release(decision_id)
        self._state["request_pending"] = False
        self._state["decision_pending"] = False
        self._generation_cancel = None
        self._generation_id = None
        # Cancellation can race the provider's final history commit before controller admission.
        self.provider.reset_context("generation_cancelled")
        if self._state["phase"] != "stopped":
            self._state["phase"] = "paused" if self._state["desired_state"] == "paused" else "observing"
        self._emit("decision_discarded", {"decision_id": decision_id, "reason": "generation_cancelled"})
        if self._state["desired_state"] == "paused" and not self._step_permit and not self._prepare:
            self._write_result("user_paused")

    def _invalidate_decision(self) -> None:
        """Retire an unsubmitted proposal while keeping the real decision episode and budgets."""
        if self._cached and not self._state["inflight"]:
            self.instructions.release(self._cached["decision_id"])
        self._state["decision_epoch"] += 1
        self._state["desired_state"] = "paused"
        self._state["assistance_mode"] = "human_assisted"
        self._sync_manifest()
        self._state["error"] = None
        self._cached = None
        self._state["decision"] = None
        self._step_permit = 0
        self._prepare = True
        self._reset_provider = True

    def instruction(self, command_id: str, text: str, scope: str, timing: str) -> dict:
        """Queue guidance for the next complete decision or explicitly supersede the current one."""
        with self._condition:
            signature = json.dumps(["instruction", text, scope, timing], ensure_ascii=False)
            existing = self._existing_command(command_id, signature)
            if existing:
                return existing
            if timing not in {"next_decision", "redecide"}:
                raise ValueError("Unsupported instruction timing.")
            if timing == "redecide" and self._budget_exhausted():
                raise SessionError("The persisted session budget is exhausted.")
            record = self.instructions.add(text, scope, self._state.get("encounter_id"))
            if timing == "redecide":
                if not self._state.get("budget_started_utc"):
                    self._state["budget_started_utc"] = utc_now()
                    if hasattr(self.provider, "_started"):
                        self.provider._started = time.monotonic()
                self._invalidate_decision()
            self._emit("instruction_received", record)
            return self._receipt(command_id, signature, instruction_id=record["id"], timing=timing)

    def revoke(self, command_id: str, instruction_id: str) -> dict:
        """Remove future guidance without rewriting an already captured decision snapshot."""
        with self._condition:
            signature = "revoke:" + instruction_id
            existing = self._existing_command(command_id, signature)
            if existing:
                return existing
            record = self.instructions.revoke(instruction_id)
            self._emit("instruction_revoked", record)
            return self._receipt(command_id, signature, instruction_id=instruction_id)

    def _budget_exhausted(self) -> bool:
        """Check cumulative session limits; a refresh, pause, or epoch never refunds them."""
        metrics = self._state["metrics"]
        obs = self._state.get("observation") or {}
        direct = self._decision_owner(obs) == "upstream"
        if not obs.get("state", {}).get("screen"):
            settings = self._state.get("simulation_settings", SIMULATION_SETTINGS)
            direct = "upstream" in (settings.get("combat_decision_mode"), settings.get("run_decision_mode"))
        return (not direct and metrics["requests_used"] >= metrics["max_requests"]) or self._native_budget_exhausted()

    def _decision_phase(self, obs: dict, *, scene: dict | None = None) -> str:
        """Keep combat overlays attached to public room evidence while rewards remain noncombat."""
        state = obs.get("state", {})
        screen = state.get("screen")
        if screen == "COMBAT":
            return "combat"
        if screen not in SCENE_OVERLAYS:
            return "run"
        underlying = [state.get("selection", {}).get("underlying_screen"), state.get("modal", {}).get("underlying_screen")]
        if any(screen and screen != "COMBAT" for screen in underlying):
            return "run"
        if "COMBAT" in underlying:
            return "combat"
        if isinstance(obs.get("public_history", {}).get("combat_selection_origin"), dict):
            return "combat"
        selection = state.get("selection", {})
        kind = str(selection.get("kind", "")).lower()
        if state.get("reward", {}).get("pending_card_choice") or kind.startswith(("deck_", "reward_", "card_reward")):
            return "run"
        if kind.startswith(("combat", "hand_")) or selection.get("operation") == "combat":
            return "combat"
        run = state.get("run", {})
        scope = [obs.get("run_id"), run.get("act_id"), run.get("floor")]
        scene = self._state.get("decision_scene", {}) if scene is None else scene
        return "combat" if scene.get("scope") == scope and scene.get("screen") == "COMBAT" else "run"

    def _decision_owner(self, obs: dict) -> str:
        """Resolve the final chooser independently for combat and noncombat decisions."""
        settings = self._state.get("simulation_settings", SIMULATION_SETTINGS)
        return settings.get(self._decision_phase(obs) + "_decision_mode", "llm")

    def _run_policy_scope(self, obs: dict) -> list:
        """Bind a pending upstream selection intent to its controller run and public floor."""
        run = obs.get("state", {}).get("run", {})
        return [obs.get("run_id"), run.get("act_id"), run.get("floor")]

    def _allocate_combat_counter(self, obs: dict) -> dict:
        """Reserve one durable wrapper decision slot per public episode, including forced combat roots."""
        if "next_combat_decision_counter" not in self._state:
            self._recover_combat_counter()
        episode = obs.get("decision_episode_id")
        if not isinstance(episode, str) or not episode:
            raise SessionError("Combat decisions require a public episode identity.")
        existing = self._state.get("combat_counter_episode") or {}
        if existing.get("decision_episode_id") == episode:
            return deepcopy(existing)
        counter = self._state["next_combat_decision_counter"]
        binding = {"decision_episode_id": episode, "decision_counter": counter,
                   "decision_counter_source": self._state["combat_decision_counter_source"]}
        self._state["next_combat_decision_counter"] = counter + 1
        self._state["combat_counter_episode"] = binding
        self._persist()
        self._trace("combat_decision_counter", binding, obs)
        return deepcopy(binding)

    def _recover_combat_counter(self) -> None:
        """Lazily recover only this old run's observed decision episodes; unobserved native calls stay unknown."""
        episodes, seen, scene, issues = [], set(), {}, []
        for record in read_jsonl(self._journal.directory / "transitions.jsonl", issues):
            if record.get("run_id") != self._state["run_id"]:
                continue
            data = record.get("data") or {}
            obs = data.get("observation")
            if not isinstance(obs, dict) or not isinstance(obs.get("state"), dict):
                continue
            state = obs["state"]
            if state.get("screen") and state["screen"] not in SCENE_OVERLAYS:
                scene = {"scope": self._run_policy_scope(obs), "screen": state["screen"]}
            episode = obs.get("decision_episode_id") or record.get("decision_episode_id")
            decision = data.get("decision") or {}
            if (decision.get("decision_source") == "automatic"
                    and decision.get("action", {}).get("type") not in {"end_turn", "confirm_selection"}):
                continue
            if (record.get("event_type") in {"decision", "decision_input"} and isinstance(episode, str)
                    and episode not in seen and self._decision_phase(obs, scene=scene) == "combat"):
                seen.add(episode)
                episodes.append(episode)
        source = "historical_public_episode_recovery"
        self._state["next_combat_decision_counter"] = len(episodes)
        self._state["combat_decision_counter_source"] = source
        self._state["combat_counter_episode"] = ({"decision_episode_id": episodes[-1],
            "decision_counter": len(episodes) - 1, "decision_counter_source": source} if episodes else None)
        self._state["combat_counter_recovery"] = {"observed_episodes": len(episodes),
            "unobserved_native_or_manual_calls_known": False, "journal_issues": issues}

    def _native_budget_exhausted(self) -> bool:
        """Do not submit a paid cached proposal beyond the action or wall-time ceiling."""
        metrics = self._state["metrics"]
        return (metrics["actions_completed"] >= metrics["max_actions"]
                or self._elapsed() >= metrics["max_wall_seconds"])

    def start_background(self) -> None:
        """Start one observing worker under the same writer lock used by headless runs."""
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._writer = WriterLock(self.root / "local" / "controller.lock")
            self._writer.__enter__()
            self._record_artifacts()
            self._sync_manifest()
            self._persist()
            self._thread = threading.Thread(target=self._work, name="sts2-controller", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Close admission immediately and retain any in-flight request/action for reconciliation."""
        with self._condition:
            if self._shutdown.is_set() and (self._thread is None or not self._thread.is_alive()):
                return
            self._state["desired_state"] = "paused"
            self._step_permit = 0
            self._shutdown.set()
            self._cancel_generation()
            self._persist()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=5)
        else:
            self._close_prediction_worker()
            if hasattr(self.bridge, "client"):
                self.bridge.client.close()
            self.simulator.close()
            if hasattr(self.provider, "close"):
                self.provider.close()

    def navigate_menu(self, action: str) -> bool:
        """Perform explicit launch navigation under the writer lock before starting the worker."""
        if self._thread and self._thread.is_alive():
            raise SessionError("Stop the observing worker before preparing the game.")
        if self._state["inflight"] or self._state["request_pending"] or (self.root / "local/pending-action.json").exists():
            raise SessionError("Reconcile pending work before preparing the game.")
        # These operator-only transitions never enter the model's NoSL action list.
        screen = {"continue_run": "MAIN_MENU", "return_to_main_menu": "GAME_OVER"}.get(action)
        if screen is None:
            raise ValueError("Unsupported launch navigation.")
        self.bridge.health()
        public, token = self.bridge.settled()
        if public.get("screen") != screen or action not in public.get("available_actions", []):
            return False
        if action == "continue_run" and self._state.get("terminal_evidence"):
            raise SessionError("Archive the terminal ledger before continuing another saved run.")
        obs, _ = self._observe(settled=True)
        # No cached strategy may survive loading a save or leaving the terminal summary.
        self._cached = None
        self._state["decision"] = None
        self.provider.reset_context("operator_launch_navigation")
        admitted = {"action_id": str(uuid.uuid4()), "action": {"type": action},
            "expected_observation_token": token, "decision_id": str(uuid.uuid4()),
            "decision_episode_id": self._state["decision_episode_id"],
            "decision_epoch": self._state["decision_epoch"], "accepted": False,
            "observation": obs, "operator_navigation": True}
        self._record_submission(admitted)
        self._dispatch(admitted)
        if self._state["inflight"] or self._state.get("error"):
            raise SessionError("Launch navigation requires receipt reconciliation.")
        if not any(event["type"] == "action_completed"
                   and event["payload"].get("action_id") == admitted["action_id"] for event in self._events):
            raise SessionError("游戏菜单状态已变化，请重试启动游戏。")
        return True

    def configure_simulation(self, settings: dict) -> dict:
        """Persist paused simulation controls without refunding run or episode budgets."""
        with self._condition:
            settings = CombatSimulator.normalize_settings(settings, self.simulator.settings)
            if (self._state.get("desired_state") != "paused" or self._state.get("request_pending")
                    or self._state.get("decision_pending")
                    or self._state.get("inflight") or (self.root / "local/pending-action.json").exists()):
                raise SessionError("请先暂停，并等待模型请求和在途动作完成。")
            write_json(self.root / "local/simulation-settings.json", settings)
            previous = deepcopy(self.simulator.settings)
            self.simulator.configure(settings)
            self.config.setdefault("simulation", {}).update(settings)
            self._track_simulation_settings(settings, previous, "dashboard")
            self._sync_manifest()
            if previous != settings:
                self._trace("simulation_settings_changed", {
                    "change": deepcopy(self._state["simulation_settings_history"][-1]),
                    "assistance_mode": self._state["assistance_mode"]})
            if hasattr(self.bridge, "read_access"):
                self.bridge.read_access = settings["read_access"]
            if self._cached:
                self.instructions.release(self._cached["decision_id"])
            self._cached = None
            self._state["decision"] = None
            self._state["decision_epoch"] += 1
            self._state.pop("simulation", None)
            self._state.pop("upstream_selection_context", None)
            self._reset_provider = True
            self._emit("simulation_settings", {"simulation_settings": deepcopy(settings)})
            return deepcopy(settings)

    def _track_simulation_settings(self, settings: dict, previous: dict, source: str) -> None:
        """Keep mode changes attributable without turning a modified run into a frozen experiment."""
        self._state.setdefault("initial_simulation_settings", deepcopy(previous))
        self._state.setdefault("simulation_settings_baseline_scope", "first_recorded_after_existing_run_usage"
            if self._state["metrics"]["requests_used"] or self._state["metrics"]["actions_completed"] else "run_start")
        history = self._state.setdefault("simulation_settings_history", [])
        self._state["simulation_settings"] = deepcopy(settings)
        if previous == settings:
            return
        metrics = self._state["metrics"]
        during_run = bool(self._state.get("budget_started_utc") or metrics["requests_used"]
                          or metrics["actions_completed"]) and not self._state.get("terminal_evidence")
        history.append({"timestamp_utc": utc_now(), "source": source, "previous": deepcopy(previous),
            "settings": deepcopy(settings), "during_run": during_run,
            "provider_requests": metrics["requests_used"], "actions_completed": metrics["actions_completed"],
            "decision_episode_id": self._state.get("decision_episode_id")})
        if during_run:
            self._state["assistance_mode"] = "human_assisted"

    def configure_launch(self, settings: dict) -> None:
        """Change only a paused pre-run policy, retaining all spending and native evidence."""
        target = configured_start(settings)
        with self._condition:
            state = (self._state.get("observation") or {}).get("state", {})
            if (self._state.get("desired_state") != "paused" or self._state.get("request_pending")
                    or self._state.get("inflight") or (self.root / "local/pending-action.json").exists()
                    or (state.get("screen") not in {None, "MAIN_MENU", "CHARACTER_SELECT"}
                        and not self._state.get("terminal_evidence"))):
                raise SessionError("只能在新局开始前暂停设置角色与难度；进行中的对局不可更改。")
            self.config.setdefault("policy", {}).update(target)
            self._state["launch_policy"] = target
            self._discard_rule_stale_proposal()
            self._persist()

    def configure_presentation(self, language: str) -> None:
        """Set a fresh run's spectator language; a terminal ledger keeps its original text identity."""
        if language not in PRESENTATION_LANGUAGES:
            raise ValueError("Unsupported presentation language.")
        with self._condition:
            if (self._state.get("desired_state") != "paused" or self._state.get("request_pending")
                    or self._state.get("decision_pending") or self._state.get("inflight")
                    or (self.root / "local/pending-action.json").exists()
                    or presentation_run_active(self._state)):
                raise SessionError("Choose the presentation language before a new run while paused.")
            if self._state.get("terminal_evidence"):
                # The launcher stores the next-run preference without rewriting the finished ledger.
                return
            if self._state["presentation_language"] == language:
                return
            self.config.setdefault("presentation", {})["language"] = language
            self._state["presentation_language"] = language
            self._discard_rule_stale_proposal()
            self._reset_provider = True
            self._persist()

    def prepare_launch_connection(self) -> None:
        """Explicitly reconnect an unfinished run only after all previous native work is resolved."""
        if (self._state["inflight"] or self._state["request_pending"]
                or (self.root / "local/pending-action.json").exists()):
            raise SessionError("Reconcile pending work before reconnecting the game.")
        if self._thread and self._thread.is_alive():
            raise SessionError("Stop the observing worker before reconnecting the game.")
        previous = self.bridge.identity
        self.bridge.identity = None
        try:
            self.bridge.health()
        except Exception:
            self.bridge.identity = previous
            raise
        if previous is not None and previous != self.bridge.identity:
            if self._cached:
                self.instructions.release(self._cached["decision_id"])
                self._emit("decision_discarded", {"decision_id": self._cached["decision_id"],
                    "reason": "operator_game_restart"})
            self._cached = None
            self._state["decision"] = None
            self._state["decision_epoch"] += 1
            self._state["plan"] = ""
            self.memory.set_plan("")
            self.provider.reset_context("operator_game_restart")
            self._state["bridge_identity"] = list(self.bridge.identity)
            self._emit("game_process_reconnected", {"previous_identity": list(previous),
                "identity": list(self.bridge.identity)})

    def _provider_event(self, event_type: str, payload: dict) -> None:
        """Publish streaming text while persisting request reservations before network I/O."""
        with self._condition:
            if (event_type.endswith("_delta") and self._generation_cancel is not None
                    and self._generation_cancel.is_set() and payload.get("decision_id") == self._generation_id):
                # A delta already queued on this lock must not appear after pause acknowledgement.
                return
            if event_type.startswith("tool_call_"):
                calls = self._state.setdefault("tool_calls", [])
                call = next((item for item in calls
                    if item.get("tool_call_id") == payload.get("tool_call_id")), None)
                if call is None:
                    call = {}
                    calls.append(call)
                stored = deepcopy(payload)
                if "result" in stored:
                    # The journal keeps the full audit; the ledger only needs a readable summary.
                    stored["result"] = bounded_tool_result(stored["result"])
                call.update(stored)
                del calls[:-DISPLAY_TOOLS]
                self._journal.event("tool-calls", {"event_type": event_type, **deepcopy(payload)})
                self._trace(event_type, payload, self.provider.event_context)
                self._emit(event_type, payload)
                return
            request_id = payload.get("model_request_id")
            request = next((item for item in self._state["model_requests"]
                            if item["model_request_id"] == request_id), None)
            metrics = self._state["metrics"]
            if event_type == "model_request_started":
                request = {**payload, "status": "generating", "reasoning": "", "content": ""}
                if self._generation_cancel is not None and self._generation_cancel.is_set():
                    request["status"] = "cancelling"
                self._state["model_requests"].append(request)
                self._state["model_requests"] = self._state["model_requests"][-DISPLAY_REQUESTS:]
                self._state["request_pending"] = True
                metrics["requests_used"] = self.provider.requests_used
                if payload.get("attempt_index", 0):
                    metrics["retries"] += 1
                if payload.get("format_repair") and not payload.get("attempt_index", 0):
                    metrics["format_repairs"] += 1
            elif event_type.endswith("_delta") and request is not None:
                field = "reasoning" if event_type == "model_reasoning_delta" else "content"
                request[field] += payload.get("text", "")
                if payload.get("first_delta_utc"):
                    request.setdefault("first_delta_utc", payload["first_delta_utc"])
                for name in ("last_delta_utc", "first_delta_elapsed_ms", "last_delta_elapsed_ms", "generation_ms"):
                    if name in payload:
                        request[name] = payload[name]
            elif event_type in {"model_request_completed", "model_request_failed", "model_request_cancelled"}:
                metadata = payload.get("metadata", payload)
                if request is not None:
                    request.update(deepcopy(metadata))
                    request["status"] = event_type.removeprefix("model_request_")
                self._state["request_pending"] = False
                if request_id not in self._state["finished_requests"]:
                    self._state["finished_requests"].append(request_id)
                    usage = metadata.get("usage", {})
                    for key in TOKEN_FIELDS:
                        if type(usage.get(key)) is int:
                            metrics[key] += usage[key]
                    if any(type(usage.get(key)) is not int for key in ("prompt_tokens", "completion_tokens")):
                        metrics["unknown_usage_requests"] += 1
                    self._journal.event("provider-usage", metadata)
            if not event_type.endswith("_delta"):
                self._trace("model_request", {"provider_event_type": event_type,
                    "payload": payload,
                    "response": deepcopy(request) if event_type in {
                        "model_request_completed", "model_request_failed", "model_request_cancelled"} else None}, payload)
            if event_type == "model_request_input":
                # Exact prompts belong in the replay archive, not every dashboard snapshot/SSE event.
                return
            self._emit(event_type, payload, persist=not event_type.endswith("_delta"))

    def _observe(self, *, settled: bool, stage: str = "observation", command_receipt=None) -> tuple[dict, str]:
        """Refresh public state and stable encounter identity, never a raw payload."""
        with self._timed(stage) as timing:
            health = self.bridge.health()
            self.provider.knowledge_game_version = health.get("game_version")
            state, token = self.bridge.settled() if settled else self.bridge.snapshot()
            internal = (getattr(self.bridge, "simulation_internal", None)
                        if getattr(self.bridge, "simulation_internal_token", None) == token else None)
            timing["settled"] = settled
            bridge_timing = getattr(self.bridge, "last_settle_metrics", None)
            if settled and isinstance(bridge_timing, dict):
                timing.update(deepcopy(bridge_timing))
        with self._condition:
            self._state["connection"] = {"connected": True, "pid": health["process_id"],
                "game_version": health["game_version"], "mod_version": health.get("mod_version"),
                "status": health.get("status"), "error": None}
            first_identity = self._state["bridge_identity"] is None
            self._state["bridge_identity"] = list(self.bridge.identity)
            if first_identity:
                manifest_path = self._journal.directory / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["bridge"] = health
                self._journal.document("manifest.json", manifest)
            screen = state.get("screen")
            alive = any(enemy.get("alive") for enemy in state.get("combat", {}).get("enemies", []))
            if (screen == "COMBAT" or alive) and not self._state["encounter_id"]:
                self._state["encounter_id"] = str(uuid.uuid4())
            elif screen in ROOM_SCREENS and not alive and self._state["encounter_id"]:
                self.instructions.expire_encounter(self._state["encounter_id"])
                self._state["encounter_id"] = None
            signature = _fingerprint(state)
            if settled and signature != self._state["public_fingerprint"]:
                self._state["episode_count"] += 1
                self._state["decision_episode_id"] = f"{self._state['run_id']}:{self._state['episode_count']}"
                self._state["public_fingerprint"] = signature
            episode = self._state["decision_episode_id"] or f"{self._state['run_id']}:0"
            obs = observation(state, self._state["run_id"], episode)
            scope = self._run_policy_scope(obs)
            if state.get("screen") not in SCENE_OVERLAYS:
                self._state["decision_scene"] = {"scope": scope, "screen": state.get("screen")}
            if self._state.get("upstream_selection_context", {}).get("scope") != scope:
                self._state.pop("upstream_selection_context", None)
            obs["public_history"] = self._public_history.observe(state, self._state["encounter_id"], self.bridge.identity)
            if settled and self.memory.sync_scene(obs):
                self._state["scene_actions"] = []
                # One automatic card-reward open is allowed per reward screen.
                self._state["card_reward_opened"] = False
            conversation = self.memory.conversation_scene(obs)
            previous_conversation = self._state.get("conversation_scene")
            if settled and conversation and conversation != previous_conversation:
                # Only a new combat encounter or act makes older observations misleading.
                self._state["conversation_scene"] = conversation
                if (previous_conversation is not None
                        and self.memory.starts_new_conversation(conversation, previous_conversation)):
                    self.provider.reset_context("strategy_scene_changed")
                    self._emit("strategy_scene_changed", {"scene_scope": self.memory.scene_key,
                        "conversation_scope": conversation})
            self._state["public_history_tracker"] = deepcopy(self._public_history.data)
            changed = self._state["observation"] != obs
            self._state["observation"] = obs
            if settled:
                self.memory.observe(obs)
            if changed:
                self._emit("observation", {"observation": obs, "settled": settled})
                self._journal.event("public-events", {"observation": obs})
            if changed or settled:
                self._trace("observation", {"observation": obs, "settled": settled}, obs)
            mirror_encounter = f"{self._state['run_id']}:{self._state['encounter_id']}"
        # Bind the filtered search root without nesting the simulator lock inside the session lock.
        self.simulator.set_internal_state(obs["state"], internal)
        if settled:
            completion = None
            if command_receipt is not None:
                command, receipt = command_receipt
                if receipt.get("status") == "pending":
                    receipt = self.bridge.receipt(command["action_id"])
                completion = {"action_id": command["action_id"], "action": command["action"],
                              "before_state": command.get("observation", {}).get("state", {}),
                              "receipt_status": receipt.get("status")}
            mirror = self.simulator.synchronize(obs, encounter=mirror_encounter, completion=completion)
            with self._condition:
                self._state["simulation_mirror"] = mirror
                if mirror.get("status") == "unavailable":
                    self._emit("simulation_mirror_warning", mirror)
        return obs, token

    def _generate(self, obs: dict, token: str) -> None:
        """Capture guidance once for a complete decision; late results stay visible but cannot execute."""
        with self._condition:
            if (self._budget_exhausted() or self._shutdown.is_set()
                    or not (self._state["desired_state"] == "running" or self._step_permit or self._prepare)):
                return
            decision_phase = self._decision_phase(obs)
            owner = self._decision_owner(obs)
            automatic = automatic_decision(obs, self.config,
                scene_actions=self._state.get("scene_actions", []),
                card_reward_opened=self._state.get("card_reward_opened", False)) if not self._prepare or self._step_permit else None
            carry = self._state.get("upstream_selection_context", {})
            context = carry.get("context") or {}
            if (owner in {"assist", "upstream"} and obs.get("state", {}).get("screen") == "REWARD"
                    and carry.get("scope") == self._run_policy_scope(obs)
                    and context.get("intent") == "reward_sequence" and context.get("claim_after_discard")):
                # Finish the accepted replacement's exact reward before generic page housekeeping.
                automatic = None
            # UI-only closing is not a RunAction; forced game choices still occupy wrapper slots.
            combat_choice = decision_phase == "combat" and (automatic is None or automatic["action"]["type"] in {"end_turn", "confirm_selection"})
            counter = self._allocate_combat_counter(obs) if combat_choice else {}
            if owner == "upstream" and combat_choice:
                automatic = None
            if automatic is not None:
                validate_action(obs, automatic["action"], self.config.get("policy"))
                self._cached = {**automatic, "decision_id": str(uuid.uuid4()),
                    "decision_epoch": self._state["decision_epoch"],
                    "decision_episode_id": self._state["decision_episode_id"], "token": token,
                    "instructions": [], "observation": obs,
                    "decision_owner": owner, "decision_phase": decision_phase, **counter,
                    "rules_identity": self._proposal_rule_identity()}
                self._state["decision"] = {key: deepcopy(value) for key, value in self._cached.items()
                    if key not in {"token", "observation", "instructions"}}
                self._state["decision"]["action_label"] = _action_label(
                    automatic["action"], obs, self._state["presentation_language"])
                self._state["phase"] = "decision_ready"
                self._journal.event("decisions", self._state["decision"])
                self._emit("decision", self._state["decision"])
                return
            if self._reset_provider:
                self.provider.reset_context("controller_redecision")
                self._reset_provider = False
            decision_id = str(uuid.uuid4())
            cancel_event = threading.Event()
            self._generation_cancel = cancel_event
            self._generation_id = decision_id
            epoch = self._state["decision_epoch"]
            guidance = [] if owner == "upstream" else self.instructions.reserve(decision_id, self._state["encounter_id"])
            if guidance:
                self._state["assistance_mode"] = "human_assisted"
                self._sync_manifest()
                self._emit("instructions_injected", {"decision_id": decision_id, "instructions": guidance})
            self._state["phase"] = "upstream_generating" if owner == "upstream" else "model_generating"
            self._state["decision_pending"] = True
            self.provider.event_context = {"run_id": self._state["run_id"], "decision_id": decision_id,
                "decision_episode_id": self._state["decision_episode_id"], "decision_epoch": epoch}
            self._persist()
        try:
            with self._timed("simulation_advice", self.provider.event_context):
                if decision_phase == "combat":
                    if owner == "upstream" and self.simulator.strategies != ("mcts",):
                        raise SessionError("上游直接战斗决策需要 MCTS；请调整搜索配置。")
                    simulation = self.simulator.advise(obs,
                        decision_counter=counter.get("decision_counter"),
                        decision_counter_source=counter.get("decision_counter_source"))
                elif owner in {"assist", "upstream"}:
                    saved_context = self._state.get("upstream_selection_context", {})
                    selection_context = (saved_context.get("context")
                        if saved_context.get("scope") == self._run_policy_scope(obs) else None)
                    simulation = self.simulator.advise_run(obs, public_map=self.memory.public_map,
                        selection_context=selection_context)
                else:
                    simulation = {"status": "skipped"}
            policy_observation = deepcopy(obs)
            if simulation.get("simulation_advice"):
                policy_observation["simulation_advice"] = simulation["simulation_advice"]
            with self._condition:
                if cancel_event.is_set():
                    raise ProviderCancelled("Model generation cancelled by the controller.")
                context = deepcopy(self.provider.event_context)
                if simulation.get("status") != "skipped" or simulation.get("simulation_advice"):
                    self._trace("simulation", {"result": simulation}, context)
                    self._state["simulation"] = {key: deepcopy(simulation.get(key))
                        for key in ("status", "backend", "rule_identity", "rule_snapshot", "search_budget",
                                    "diagnostics", "elapsed_ms", "simulation_advice", "decision_counter", "decision_counter_source")}
                    self._emit("simulation", {"decision_id": decision_id,
                        **deepcopy(self._state["simulation"])})
                memory = self.memory.context()
                self._trace("decision_input", {"observation": policy_observation,
                    "memory": memory, "instructions": guidance}, context)
            if owner == "upstream":
                advice = simulation.get("simulation_advice") or {}
                action = ((advice.get("strategies", {}).get("mcts") or {}).get("first_action")
                          if decision_phase == "combat" else ((simulation.get("result") or {}).get("best_action")
                          if simulation.get("status") == "advisory" else None))
                if not isinstance(action, dict):
                    diagnostics = (simulation.get("diagnostics") or (simulation.get("result") or {}).get("errors")
                                   or (simulation.get("result") or {}).get("support") or simulation.get("status"))
                    raise SessionError(f"上游{decision_phase}决策不可用；未调用语言模型。{json.dumps(diagnostics, ensure_ascii=False)[:180]}")
                decision = {"action": deepcopy(action), "reason": display_text(
                    self._state["presentation_language"], "upstream_decision"),
                    "decision_source": "upstream_mcts" if decision_phase == "combat" else "upstream_ppo",
                    "model_identity": deepcopy(((simulation.get("results") or {}).get("mcts") or {}).get("network_identity")
                        if decision_phase == "combat" else (simulation.get("result") or {}).get("model_identity")),
                    "instructions_used": False}
            else:
                with self._timed("model_decision", context):
                    decision = self.provider.decide(policy_observation, memory, instructions=guidance,
                                                    cancel_event=cancel_event)
                decision["decision_source"] = "llm_with_upstream_advice" if owner == "assist" else "llm"
            if cancel_event.is_set():
                raise ProviderCancelled("Model generation cancelled by the controller.")
            decision.update(decision_owner=owner, decision_phase=decision_phase)
            decision.update(counter)
            if decision_phase == "combat":
                decision["decision_identity"] = deepcopy(((simulation.get("results") or {}).get("mcts") or {}).get("decision_identity"))
            if self.config.get("simulation", {}).get("prediction_mode", "inline") == "background":
                prediction = {"status": "deferred", "meaning": "audit_of_frozen_root_after_admission"}
            else:
                with self._timed("selected_action_prediction", context):
                    prediction = self.simulator.predict(obs, decision["action"])
            if prediction.get("status") != "skipped":
                self._trace("simulation", {"prediction": prediction}, context)
                with self._condition:
                    self._state.setdefault("simulation", {})["prediction"] = deepcopy(prediction)
                    self._emit("simulation", {"decision_id": decision_id,
                        **deepcopy(self._state["simulation"])})
        except Exception as error:
            with self._condition:
                if isinstance(error, ProviderCancelled) or cancel_event.is_set():
                    self._discard_cancelled_generation(decision_id)
                    return
                self.instructions.release(decision_id)
                self._state["decision_pending"] = False
                self._generation_cancel = None
                self._generation_id = None
            raise
        with self._condition:
            if cancel_event.is_set():
                self._discard_cancelled_generation(decision_id)
                return
            self._generation_cancel = None
            self._generation_id = None
            self._state["request_pending"] = False
            self._state["decision_pending"] = False
            if epoch != self._state["decision_epoch"]:
                self.instructions.release(decision_id)
                self._emit("decision_discarded", {"decision_id": decision_id, "reason": "superseded_epoch"})
                return
            self._state["phase"] = "evaluating"
            try:
                validate_action(obs, decision["action"], self.config.get("policy"))
                evaluation = verify_selected_plan(obs, decision["action"])
            except Exception:
                # A rejected proposal is not an accepted action and must not consume guidance.
                self.instructions.release(decision_id)
                self.provider.reset_context("decision_rejected")
                self._emit("decision_rejected", {"decision_id": decision_id,
                    "reason": "action_validation_or_evaluation_failed"})
                raise
            self._emit("calculation", {"decision_id": decision_id, "result": evaluation})
            self._journal.event("evaluator-results", {"decision_id": decision_id, "result": evaluation})
            self._cached = {**decision, "decision_id": decision_id, "decision_epoch": epoch,
                "decision_episode_id": self._state["decision_episode_id"], "token": token,
                "instructions": guidance, "observation": obs, "prediction": prediction,
                "rules_identity": self._proposal_rule_identity()}
            if decision_phase == "run" and owner in {"assist", "upstream"}:
                result = simulation.get("result") or {}
                next_context = result.get("next_context") if decision["action"] == result.get("best_action") else None
                self._cached["upstream_next_context"] = {"scope": self._run_policy_scope(obs), "context": deepcopy(next_context)}
            self._state["decision"] = {key: deepcopy(value) for key, value in self._cached.items()
                                       if key not in {"token", "observation", "instructions", "prediction"}}
            self._state["decision"]["action_label"] = _action_label(
                decision["action"], obs, self._state["presentation_language"])
            self._state["phase"] = "decision_ready"
            self._prepare = False
            self._journal.event("decisions", self._state["decision"])
            self._emit("decision", self._state["decision"])
            if self._state["desired_state"] == "paused" and not self._step_permit:
                self._write_result("user_paused")

    def _admit(self, obs: dict, token: str) -> dict | None:
        """Linearize every native submission with controls before releasing the network operation."""
        with self._condition:
            if self._discard_rule_stale_proposal():
                return None
            proposal = self._cached
            if self._state["desired_state"] != "running" and not self._step_permit:
                return None
            if self._state["inflight"] or not proposal or self._shutdown.is_set():
                return None
            if proposal["decision_epoch"] != self._state["decision_epoch"]:
                return None
            if proposal["token"] != token:
                self.instructions.release(proposal["decision_id"])
                self._cached = None
                self._state["decision"] = None
                self.provider.reset_context("public_state_changed")
                self._emit("decision_discarded", {"decision_id": proposal["decision_id"], "reason": "public_state_changed"})
                return None
            if self._state["metrics"]["actions_completed"] >= self._state["metrics"]["max_actions"] or self._elapsed() >= self._state["metrics"]["max_wall_seconds"]:
                return None
            validate_action(obs, proposal["action"], self.config.get("policy"))
            action_id = str(uuid.uuid4())
            admitted = {"action_id": action_id, "action": deepcopy(proposal["action"]),
                "expected_observation_token": token, "decision_id": proposal["decision_id"],
                "decision_episode_id": proposal["decision_episode_id"], "decision_epoch": proposal["decision_epoch"],
                "accepted": False, "observation": deepcopy(proposal["observation"]),
                "prediction": deepcopy(proposal.get("prediction")),
                **{key: deepcopy(proposal[key]) for key in
                   ("plan", "build_plan", "scene_plan", "decision_source", "decision_owner", "decision_phase",
                    "model_identity", "upstream_next_context", "decision_counter", "decision_counter_source",
                    "decision_identity") if key in proposal}}
            self._record_submission(admitted)
            return deepcopy(admitted)

    def _record_submission(self, admitted: dict) -> None:
        """Persist every model or operator submission before its single native POST."""
        self._state["inflight"] = admitted
        self._state["phase"] = "executing"
        self._step_permit = 0
        self._prepare = False
        write_json(self.root / "local" / "pending-action.json", {
            "instance": list(self.bridge.identity), "run_id": self._state["run_id"], **admitted})
        self._journal.event("execution-requests", admitted)
        self._emit("action_submitted", admitted)

    def _settle_receipt(self, command: dict, receipt: dict) -> None:
        """Consume once on acceptance, then wait for a settled successor before the next action."""
        with self._condition:
            self._journal.event("execution-receipts", receipt)
            self._trace("action_receipt", {"receipt": receipt}, command)
            self._emit("action_receipt", receipt)
            if receipt["status"] not in {"completed", "pending"}:
                self._state["desired_state"] = "paused"
                self._state["phase"] = "reconciling"
                self._state["error"] = "Action outcome requires receipt reconciliation."
                self._persist()
                return
            self._accept_receipt(command)
            self._state["phase"] = "waiting_settlement"
            self._persist()
        successor, _ = self._observe(settled=True, stage="post_action_observation", command_receipt=(command, receipt))
        with self._condition:
            successor["public_history"] = self._public_history.completed(command, receipt, successor)
            self._state["public_history_tracker"] = deepcopy(self._public_history.data)
            self._state["observation"] = deepcopy(successor)
            if not command.get("operator_navigation"):
                self._state["metrics"]["actions_completed"] += 1
            self._state["inflight"] = None
            self._cached = None
            if self._state.get("decision"):
                self._state["decision"]["status"] = "executed"
            self._state["phase"] = "observing" if self._state["desired_state"] == "running" else "paused"
            self._state["error"] = None
            pending = self.root / "local" / "pending-action.json"
            if pending.exists():
                pending.unlink()
            self._emit("action_completed", {"action_id": command["action_id"], "action": command["action"],
                "decision_id": command["decision_id"],
                "decision_episode_id": command.get("decision_episode_id"),
                "observation": successor, "receipt": receipt,
                "actions_completed": self._state["metrics"]["actions_completed"]})
            if self._record_game_over(successor):
                return
            if self._state["desired_state"] == "paused":
                self._write_result("user_paused")

    @staticmethod
    def _opens_card_reward(command: dict) -> bool:
        """Remember a claim that opened this reward screen's card choice, whoever opened it."""
        action = command.get("action") or {}
        if action.get("type") != "claim_reward":
            return False
        reward = ((command.get("observation") or {}).get("state") or {}).get("reward") or {}
        for option in reward.get("rewards") or []:
            if not isinstance(option, dict) or option.get("i") != action.get("option_index"):
                continue
            kind = option.get("reward_type") or option.get("kind")
            if not isinstance(kind, str) or not kind:
                kind = str(option.get("line", "")).split(":", 1)[0]
            return kind.strip().upper() == "CARD"
        return False

    def _accept_receipt(self, command: dict) -> None:
        """Consume accepted-action memory once under the session condition lock."""
        if not self._state["inflight"].get("accepted") and "upstream_next_context" in command:
            self._state["upstream_selection_context"] = deepcopy(command["upstream_next_context"])
        if not self._state["inflight"].get("accepted") and not command.get("operator_navigation"):
            self._state.setdefault("scene_actions", []).append(command["action"]["type"])
            if self._opens_card_reward(command):
                self._state["card_reward_opened"] = True
            self.instructions.accept(command["decision_id"])
            if command.get("observation"):
                self.memory.observe(command["observation"], command["action"])
            build = command.get("build_plan", command.get("plan"))
            if build is not None:
                self.memory.set_plan(build)
                self._state["plan"] = build
            if "scene_plan" in command:
                self.memory.set_scene_plan(command["scene_plan"])
        self._state["inflight"]["accepted"] = True

    def reconcile_completed(self, action_id: str) -> dict:
        """Use persisted completion proof only; caller must hold both offline writer locks."""
        with self._condition:
            if (self._thread and self._thread.is_alive()) or self._writer or self._state["request_pending"]:
                raise SessionError("Stop the controller before offline reconciliation.")
            pending_path = self.root / "local/pending-action.json"
            pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else None
            command = self._state.get("inflight")
            recovery = self._state.get("historical_action_recovery", {})
            # A crash after the ledger commit may leave only the matching pending file to retire.
            if command is None and recovery.get("action_id") == action_id:
                if pending is not None:
                    if (pending.get("action_id") != action_id or pending.get("run_id") != self._state["run_id"]
                            or pending.get("instance") != recovery["bridge_identity"]
                            or pending.get("action") != recovery["action"]):
                        raise SessionError("Pending submission does not match historical recovery.")
                    pending_path.unlink()
                return deepcopy(recovery)
            identity = self._state.get("bridge_identity")
            if (not command or command.get("action_id") != action_id or not identity
                    or not pending or pending.get("run_id") != self._state["run_id"]
                    or pending.get("instance") != identity
                    or any(pending.get(key) != command.get(key) for key in
                           ("action_id", "action", "decision_id", "expected_observation_token"))):
                raise SessionError("Matching persisted action, run and bridge identity are required.")
            proof = None
            for record in read_jsonl(self._journal.directory / "transitions.jsonl"):
                if (record.get("event_type") == "action_receipt" and record.get("action_id") == action_id
                        and record.get("run_id") == self._state["run_id"]
                        and record.get("bridge_identity") == {"pid": identity[0], "instance_id": identity[1]}):
                    proof = record
            receipt = proof.get("data", {}).get("receipt", {}) if proof else {}
            public = receipt.get("public_state")
            token = receipt.get("public_observation_token")
            if (receipt.get("action_id") != action_id or receipt.get("status") != "completed"
                    or receipt.get("stable") is not True or not isinstance(public, dict)
                    or public.get("protocol_version") != 1 or public.get("supported") is not True
                    or not isinstance(public.get("screen"), str) or public.get("screen") == "UNKNOWN"
                    or not isinstance(public.get("available_actions"), list)
                    or not isinstance(token, str) or not token):
                raise SessionError("No matching completed, stable public receipt proof.")
            recovery = {"status": "historical_completion_reconciled", "run_id": self._state["run_id"],
                "action_id": action_id, "action": deepcopy(command["action"]),
                "bridge_identity": deepcopy(identity), "proof_event_id": proof.get("event_id"),
                "receipt": deepcopy(receipt), "fresh_observation_required": True}
            self._accept_receipt(command)
            if not command.get("operator_navigation"):
                self._state["metrics"]["actions_completed"] += 1
            self._state.update(inflight=None, desired_state="paused", phase="paused", error=None,
                               observation=None, decision=None, historical_action_recovery=recovery)
            self._cached = None
            self._step_permit = 0
            self._prepare = False
            self._state["connection"] = {"connected": False, "error": "fresh_observation_required"}
            self.provider.reset_context("historical_completion_reconciled")
            self._trace("historical_action_reconciled", recovery, command)
            # Commit the counted completion before removing its duplicate pending submission.
            self._emit("historical_action_reconciled", recovery)
            pending_path.unlink()
            return deepcopy(recovery)

    def _dispatch(self, command: dict) -> None:
        """Send one admitted action; only bridge-rejected pre-admission requests can be retired."""
        self._start_prediction_audit(command)
        try:
            with self._timed("native_dispatch", {"action_id": command["action_id"],
                                                  "decision_id": command["decision_id"]}):
                receipt = self.bridge.execute(command["action"], command["action_id"], command["expected_observation_token"])
        except BridgeError as error:
            if error.code not in {"stale_observation", "action_in_flight"}:
                raise
            with self._condition:
                self.instructions.release(command["decision_id"])
                self._state["inflight"] = None
                self._cached = None
                self._state["desired_state"] = "paused"
                self._state["phase"] = "paused"
                (self.root / "local" / "pending-action.json").unlink(missing_ok=True)
                self._emit("action_rejected", {"action_id": command["action_id"],
                    "decision_id": command["decision_id"], "error_code": error.code})
            return
        self._settle_receipt(command, receipt)

    def _start_prediction_audit(self, command: dict) -> None:
        """Run an admitted action's optional audit concurrently using its frozen public root."""
        if (command.get("prediction") or {}).get("status") != "deferred":
            return
        with self._condition:
            if command["action_id"] in self._prediction_actions:
                return
            self._prediction_actions.add(command["action_id"])
            if self._prediction_executor is None:
                self._prediction_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sts2-prediction")
            self._prediction_executor.submit(self._run_prediction_audit, deepcopy(command))

    def _run_prediction_audit(self, command: dict) -> None:
        """Archive an audit without changing actions, current facts, or an unrelated decision's UI."""
        context = {key: command[key] for key in (
            "action_id", "decision_id", "decision_episode_id", "decision_epoch") if key in command}
        try:
            with self._timed("background_prediction", context):
                prediction = self.simulator.predict(command["observation"], command["action"])
            with self._condition:
                self._trace("simulation", {"prediction": prediction, "audit_mode": "background"}, context)
                if (self._state.get("decision") or {}).get("decision_id") == command["decision_id"]:
                    self._state.setdefault("simulation", {})["prediction"] = deepcopy(prediction)
                    self._emit("simulation", {"decision_id": command["decision_id"],
                        **deepcopy(self._state["simulation"])})
        except Exception as error:
            # An optional audit cannot retroactively reject or repeat an admitted native action.
            logging.warning("Deferred simulation audit failed: %s", type(error).__name__)
            with self._condition:
                self._state["prediction_audit_warning"] = type(error).__name__

    def _close_prediction_worker(self) -> None:
        """Finish bounded audit work before closing its simulator or releasing the writer lock."""
        if self._prediction_executor is not None:
            self._prediction_executor.shutdown(wait=True)
            self._prediction_executor = None

    def _recover_pending(self) -> None:
        """Reconcile persisted submissions by same-process receipt without repeating an action POST."""
        pending_path = self.root / "local" / "pending-action.json"
        with self._condition:
            command = deepcopy(self._state["inflight"])
        if command is None and pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            if pending.get("run_id") != self._state["run_id"]:
                raise BridgeError("unresolved_action_from_another_session")
            if pending.get("instance") != list(self.bridge.identity):
                raise BridgeError("unresolved_action_from_previous_bridge_process")
            command = pending
            with self._condition:
                self._state["inflight"] = command
        if command:
            receipt = self.bridge.receipt(command["action_id"])
            if receipt["status"] in {"completed", "pending"}:
                self._settle_receipt(command, receipt)

    def _record_game_over(self, obs: dict) -> bool:
        """Record bridge-public terminal evidence before pause or exhausted-budget handling."""
        state = obs.get("state", {})
        if state.get("screen") != "GAME_OVER":
            return False
        with self._condition:
            if self._state.get("terminal_evidence"):
                return True
            self._state["terminal_elapsed_seconds"] = self._elapsed()
            self._state["desired_state"] = "paused"
            self._state["phase"] = "game_over"
            self._state["error"] = None
            self._step_permit = 0
            self._prepare = False
            self._state["victory"] = state.get("game_over", {}).get("victory")
            self._state["terminal_evidence"] = {
                "source": "bridge.public_state.game_over", "screen": "GAME_OVER",
                "game_over": deepcopy(state.get("game_over", {})),
            }
            self._emit("game_over", {"victory": self._state["victory"],
                                    "evidence": self._state["terminal_evidence"]})
            self._write_result("native_game_over")
            return True

    def _write_result(self, status: str) -> None:
        """Record all stopped sessions without mistaking a room win for a native terminal result."""
        if self._state.get("terminal_evidence"):
            status = "native_game_over"
            result_path = self._journal.directory / "result.json"
            if result_path.exists():
                try:
                    recorded = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    recorded = {}
                if recorded.get("run_id") == self._state["run_id"] and recorded.get("status") == status:
                    return
        result = {"run_id": self._state["run_id"], "status": status,
            "actions_completed": self._state["metrics"]["actions_completed"],
            "provider_requests": self._state["metrics"]["requests_used"], "wall_seconds": round(self._elapsed(), 2),
            "assistance_mode": self._state["assistance_mode"], "ended_utc": utc_now(),
            "simulation_settings": deepcopy(self._state.get("simulation_settings")),
            "simulation_settings_history": deepcopy(self._state.get("simulation_settings_history", [])),
            "victory": self._state.get("victory"), "error": self._state.get("error"),
            "terminal_evidence": deepcopy(self._state.get("terminal_evidence"))}
        self._journal.document("result.json", result)
        self._sync_manifest()

    def _sync_manifest(self) -> None:
        """Keep assistance and the current configured ceilings visible before process shutdown."""
        manifest_path = self._journal.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(assistance_mode=self._state["assistance_mode"],
                        manual_gameplay_intervention=self._state["assistance_mode"] != "autonomous",
                        current_limits=deepcopy(self.config["limits"]),
                        current_provider=deepcopy(self.config["provider"]),
                        simulation=deepcopy(self.config.get("simulation", {"enabled": False})),
                        initial_simulation_settings=deepcopy(self._state.get("initial_simulation_settings")),
                        simulation_settings_baseline_scope=self._state.get("simulation_settings_baseline_scope"),
                        simulation_settings_history=deepcopy(self._state.get("simulation_settings_history", [])),
                        replay={"schema_version": TRANSITION_SCHEMA_VERSION, "stream": "transitions.jsonl",
                                "scope": "public_decision_action_trajectory",
                                "engine_deterministic_replay": False})
        if self._state.get("knowledge"):
            manifest["current_knowledge"] = deepcopy(self._state["knowledge"])
        network_identity = self.simulator.network_identity()
        if isinstance(network_identity, dict):
            manifest["network_identity"] = network_identity
        self._journal.document("manifest.json", manifest)

    def _record_artifacts(self) -> None:
        """Bind each process segment to the actual controller, simulator, rules, and installed bridge."""
        paths = list((self.root / "src/sts2_agent").rglob("*.py"))
        paths.extend((self.root / "src/sts2_agent/web").glob("*"))
        experiment = self.root / "experiments/combat_simulation"
        paths.extend((experiment / "host").glob("*.cs"))
        paths.extend([experiment / "host/CombatSimulation.Host.csproj",
            experiment / "host/bin/Release/net9.0/CombatSimulation.Host.dll",
            experiment / "third_party/evangambit/ORIGIN.json", self.root / "configs/agent.schema.json"])
        mentor = self.root / "experiments/mentor_simulation"
        paths.extend((mentor / "host/src").glob("*.rs"))
        paths.extend((mentor / "patches").glob("*.patch"))
        paths.extend([mentor / "host/Cargo.toml", mentor / "host/Cargo.lock", mentor / "build.ps1",
                      self.root / "local/simulation/mentor/mentor-simulation.exe",
                      self.root / "local/simulation/mentor/source-provenance.json"])
        paths.extend((self.root / "knowledge").rglob("sources.lock.json"))
        paths.extend((self.root / "knowledge").rglob("entities.jsonl"))
        paths.extend((self.root / "knowledge/strategies").glob("*.json"))
        paths.append(self.root / "knowledge/current.json")
        machine_path = self.root / "configs/machine.json"
        if machine_path.exists():
            machine = json.loads(machine_path.read_text(encoding="utf-8"))
            paths.append(Path(machine["game_root"]) / "mods/STS2AIAgent.dll")
            for mod in ("SpeedX", "ModConfig"):
                paths.extend((Path(machine["game_root"]) / "mods" / mod).glob("*"))
        files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(set(paths)) if path.is_file()}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        name = f"artifacts-{stamp}.json"
        self._journal.document(name, {"timestamp_utc": utc_now(), "run_id": self._state["run_id"],
            "config": self.config, "simulation_rule_identity": self._proposal_rule_identity()["simulation"],
            "files_sha256": files})
        self._trace("process_started", {"artifacts": name, "simulation": self.config.get("simulation", {})})

    def _work(self) -> None:
        """Observe while paused and run one complete decision/action at a time when permitted."""
        try:
            while not self._shutdown.is_set():
                try:
                    self._tick()
                except (BridgeError, ProviderError, ActionError, ValueError, RuntimeError, OSError) as error:
                    with self._condition:
                        self._state["desired_state"] = "paused"
                        self._step_permit = 0
                        self._prepare = False
                        budget_stop = isinstance(error, ProviderError) and self._budget_exhausted()
                        self._state["phase"] = "budget_stop" if budget_stop else "error"
                        self._state["error"] = None if budget_stop else str(error)[:300]
                        if isinstance(error, BridgeError):
                            self._state["connection"].update(connected=False, error=error.code)
                        try:
                            self._emit("budget_stop" if budget_stop else "error",
                                {} if budget_stop else {"message": self._state["error"]})
                            self._write_result("budget_stop" if budget_stop else "technical_interruption")
                        except OSError:
                            # Keep admission paused and the error visible even when the disk cannot record it.
                            logging.exception("Controller paused; interruption could not be persisted.")
                            self._condition.notify_all()
                    self._shutdown.wait(1)
                self._shutdown.wait(OBSERVE_INTERVAL)
        finally:
            with self._condition:
                try:
                    self._persist()
                    status = {"budget_stop": "budget_stop", "game_over": "native_game_over",
                              "error": "technical_interruption"}.get(self._state["phase"], "user_paused")
                    self._write_result(status)
                except OSError:
                    # Disk failure must not prevent releasing the controller's writer lock.
                    logging.exception("Controller stopped; final state could not be persisted.")
            self._close_prediction_worker()
            if hasattr(self.bridge, "client"):
                self.bridge.client.close()
            self.simulator.close()
            if hasattr(self.provider, "close"):
                self.provider.close()
            if self._writer:
                self._writer.__exit__()
                self._writer = None

    def _refresh_terminal_connection(self) -> None:
        """Refresh bridge liveness without changing the finished run or observing another game."""
        try:
            health = self.bridge.health()
            connection = {"connected": True, "pid": health["process_id"],
                "game_version": health["game_version"], "mod_version": health.get("mod_version"),
                "status": health.get("status"), "error": None}
        except BridgeError as error:
            connection = {"connected": False, "error": error.code}
        with self._condition:
            if connection != self._state["connection"]:
                self._state["connection"] = connection
                self._emit("connection_changed", connection)

    def _tick(self) -> None:
        """Keep model waits outside the command lock and check admission immediately before POST."""
        with self._condition:
            terminal = (self._state.get("terminal_evidence") and not self._state["inflight"]
                    and not (self.root / "local/pending-action.json").exists())
            if terminal:
                self._state["desired_state"] = "paused"
                self._state["phase"] = "game_over"
            inflight = bool(self._state["inflight"])
            active = self._state["desired_state"] == "running" or self._step_permit or self._prepare
            failed = self._state.get("error") is not None
        if terminal:
            self._refresh_terminal_connection()
            return
        if inflight or (self.root / "local" / "pending-action.json").exists():
            self.bridge.health()
            self._recover_pending()
            return
        if not active or failed:
            obs, _ = self._observe(settled=False)
            self._record_game_over(obs)
            return
        with self._condition:
            exhausted = self._native_budget_exhausted() or (self._budget_exhausted() and self._cached is None)
        # A terminal public frame is evidence even if the last action used the final budget unit.
        obs, token = self._observe(settled=not exhausted, stage="decision_observation")
        if self._record_game_over(obs):
            return
        with self._condition:
            if self._native_budget_exhausted() or (self._budget_exhausted() and self._cached is None):
                self._state["desired_state"] = "paused"
                self._state["phase"] = "budget_stop"
                self._prepare = False
                self._step_permit = 0
                self._emit("budget_stop", {})
                self._write_result("budget_stop")
                return
        with self._condition:
            allowed = self._state["desired_state"] == "running" or self._step_permit or self._prepare
            cached = deepcopy(self._cached)
        if not allowed:
            return
        if not obs["available_actions"]:
            raise BridgeError("no_allowed_nosl_actions")
        if cached and cached["token"] != token:
            with self._condition:
                self.instructions.release(cached["decision_id"])
                self._cached = None
                self._reset_provider = True
        if self._cached is None:
            self._generate(obs, token)
            with self._condition:
                if self._cached is None:
                    return
            # Generation may outlast a native transition; admit only against a fresh settled frame.
            obs, token = self._observe(settled=True, stage="admission_observation")
            if self._record_game_over(obs):
                return
        command = self._admit(obs, token)
        if command:
            self._dispatch(command)

"""Read complete public run history through incremental byte-offset indexes.

The index is disposable; journals and controller state are never changed.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import threading


# Only public journals emitted by the controller are eligible for browsing.
HISTORY_FILES = ("transitions.jsonl", "dashboard-events.jsonl")
# These dashboard records duplicate the canonical transition journal.
MIRRORED_EVENTS = frozenset({
    "observation", "decision", "action_submitted", "action_receipt", "action_completed",
    "action_rejected", "decision_discarded", "decision_rejected", "error", "simulation",
    "model_request_started", "model_request_completed", "model_request_failed", "model_request_cancelled", "context_reset",
    "tool_call_started", "tool_call_completed", "tool_call_failed", "historical_action_reconciled",
})
# A complete native receipt, rather than a model proposal, determines execution status.
DECISION_STATES = {
    "decision": "proposed", "action_submitted": "submitted", "action_completed": "executed",
    "action_rejected": "rejected", "decision_rejected": "rejected", "decision_discarded": "discarded",
}
# Keep process memory bounded when a user browses several archived runs.
CACHED_RUNS = 3
# After the first build, incremental reads only append new records and hold the
# lock for milliseconds. Waiting briefly keeps responses honest: a transient
# refresh must not be reported as "still indexing" while the data is readable.
INDEX_WAIT_SECONDS = 5.0
# Diagnostics are representative locations, with a separate total count.
ISSUE_SAMPLES = 30
# IDs are directory names, never user-supplied filesystem paths.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
# Sequence numbers let the browser replace indexed live events without dropping newer ones.
EVENT_SEQUENCE = re.compile(rb'"seq"\s*:\s*(\d+)')
# Short decision fields are enough for the paginated list.
SUMMARY_FIELDS = (
    "decision_id", "decision_epoch", "decision_episode_id", "action", "action_label", "reason",
    "plan", "build_plan", "scene_plan", "decision_source", "model_identity", "status", "timestamp_utc",
)


@dataclass(slots=True)
class RecordRef:
    """Locate one complete public record without retaining its potentially large payload."""

    file: str
    offset: int
    size: int
    kind: str
    timestamp: str
    ident: str
    decision_id: str | None


@dataclass
class DecisionGroup:
    """Retain a compact summary and references for one explicitly identified decision."""

    cursor: int
    summary: dict
    records: list = field(default_factory=list)
    requests: set = field(default_factory=set)
    tools: set = field(default_factory=set)


def _object(value) -> dict:
    """Accept only JSON objects where an envelope is required."""
    return value if isinstance(value, dict) else {}


def _envelope(row: dict, file: str) -> tuple[str, dict]:
    """Normalize the two existing public journal formats without inventing associations."""
    if file == "transitions.jsonl":
        data = _object(row.get("data"))
        if row.get("event_type") == "model_request":
            return str(data.get("provider_event_type", "model_request")), _object(data.get("payload"))
        return str(row.get("event_type", "event")), data
    return str(row.get("type", "event")), _object(row.get("payload"))


class RunHistory:
    """Index one run in a background reader and expose small snapshots of its progress."""

    def __init__(self, directory: Path, run_id: str):
        """Create an empty, rebuildable in-memory index for a verified run directory."""
        self.directory = directory
        self.run_id = run_id
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.groups = {}
        self.session_events = []
        self.actions = {}
        self.positions = {file: 0 for file in HISTORY_FILES}
        self.signatures = None
        self.ready = False
        self.refreshing = False
        self.error = None
        self.issues = []
        self.issue_count = 0
        self.pending_tail = set()
        self.through_seq = 0
        self.mirrors = {}

    def _sizes(self) -> tuple:
        """Inspect fixed journal sizes without opening or scanning their content."""
        return tuple((self.directory / file).stat().st_size if (self.directory / file).is_file() else 0
                     for file in HISTORY_FILES)

    def refresh(self) -> None:
        """Start at most one incremental read while ordinary control requests remain independent."""
        with self.refresh_lock:
            sizes = self._sizes()
            if self.refreshing or sizes == self.signatures:
                return
            self.refreshing = True
            threading.Thread(target=self._update, args=(sizes,), daemon=True,
                             name="dashboard-history").start()

    def _issue(self, file: str, offset: int, code: str) -> None:
        """Expose malformed evidence locations without copying arbitrary error text."""
        self.issue_count += 1
        if len(self.issues) < ISSUE_SAMPLES:
            self.issues.append({"file": file, "offset": offset, "code": code})

    def _update(self, sizes: tuple) -> None:
        """Read newly appended complete lines, rebuilding only after a journal shrinks."""
        try:
            with self.lock:
                if any(size < self.positions[file] for file, size in zip(HISTORY_FILES, sizes)):
                    self.groups.clear()
                    self.session_events.clear()
                    self.actions.clear()
                    self.positions = {file: 0 for file in HISTORY_FILES}
                    self.issues.clear()
                    self.issue_count = 0
                    self.through_seq = 0
                    self.mirrors.clear()
                session_start = len(self.session_events)
                for file, size in zip(HISTORY_FILES, sizes):
                    self._scan(file, size)
                # Source files interleave in time; order each append batch before exposing cursors.
                self.session_events[session_start:] = sorted(self.session_events[session_start:],
                    key=lambda ref: (ref.timestamp, ref.ident))
                self.signatures = sizes
                self.ready = True
                self.error = None
        except (OSError, ValueError, TypeError):
            self.error = "历史日志暂时无法读取，请稍后重试。"
        finally:
            with self.refresh_lock:
                self.refreshing = False

    def _scan(self, file: str, size: int) -> None:
        """Skip streaming deltas and keep only offsets for complete public records."""
        if size <= self.positions[file]:
            return
        path = self.directory / file
        if path.resolve().parent != self.directory:
            raise ValueError("Unexpected journal location")
        self.pending_tail.discard(file)
        with path.open("rb") as stream:
            stream.seek(self.positions[file])
            while stream.tell() < size:
                offset = stream.tell()
                raw = stream.readline()
                if not raw.endswith(b"\n"):
                    self.pending_tail.add(file)
                    break
                self.positions[file] = stream.tell()
                # Journal envelope keys precede payloads; avoid parsing a million text deltas.
                header = raw[:220]
                if file == "dashboard-events.jsonl":
                    sequence = EVENT_SEQUENCE.search(header)
                    if sequence:
                        self.through_seq = max(self.through_seq, int(sequence.group(1)))
                if file == "dashboard-events.jsonl" and (
                    b'"type": "model_reasoning_delta"' in header
                    or b'"type": "model_content_delta"' in header
                ):
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    self._issue(file, offset, "invalid_json")
                    continue
                if not isinstance(row, dict):
                    self._issue(file, offset, "non_object_record")
                    continue
                kind, payload = _envelope(row, file)
                if kind.endswith("_delta"):
                    continue
                recorded_run = row.get("run_id", payload.get("run_id"))
                if recorded_run and recorded_run != self.run_id:
                    self._issue(file, offset, "foreign_run_record")
                    continue
                self._index(row, payload, file, offset, len(raw), kind)

    def _index(self, row: dict, payload: dict, file: str, offset: int, size: int, kind: str) -> None:
        """Attach records only through declared decision or action identities."""
        decision = _object(payload.get("decision")) if kind == "decision" and file == "transitions.jsonl" else payload
        decision_id = row.get("decision_id") or payload.get("decision_id") or decision.get("decision_id")
        action_id = row.get("action_id") or payload.get("action_id") or _object(payload.get("receipt")).get("action_id")
        if action_id and decision_id:
            self.actions[action_id] = decision_id
        if not decision_id and action_id:
            decision_id = self.actions.get(action_id)
        request_id = row.get("model_request_id") or payload.get("model_request_id")
        tool_id = payload.get("tool_call_id")
        # A matching lifecycle record is the same fact in two public journals.
        # Counts retain unmatched dashboard records when older transition evidence is absent.
        mirror = (kind, decision_id, action_id, request_id, tool_id)
        if kind in MIRRORED_EVENTS:
            if file == "transitions.jsonl":
                self.mirrors[mirror] = self.mirrors.get(mirror, 0) + 1
            elif self.mirrors.get(mirror, 0):
                self.mirrors[mirror] -= 1
                return
        timestamp = str(row.get("timestamp_utc", ""))
        ident = ("transition:" + str(row.get("event_id", offset))) if file == "transitions.jsonl" else "dashboard:" + str(row.get("seq", offset))
        ref = RecordRef(file, offset, size, kind, timestamp, ident, decision_id)
        if not decision_id:
            self.session_events.append(ref)
            return
        if decision_id not in self.groups:
            self.groups[decision_id] = DecisionGroup(len(self.groups) + 1,
                {"decision_id": decision_id, "timestamp_utc": timestamp, "status": "pending"})
        group = self.groups[decision_id]
        group.records.append(ref)
        if kind == "decision":
            group.summary.update({key: decision[key] for key in SUMMARY_FIELDS if key in decision})
            group.summary["timestamp_utc"] = timestamp
        elif kind == "action_submitted":
            group.summary.update({key: payload[key] for key in SUMMARY_FIELDS if key in payload
                                  and key not in group.summary})
        if kind in DECISION_STATES:
            group.summary["status"] = DECISION_STATES[kind]
        if request_id:
            group.requests.add(request_id)
        if tool_id:
            group.tools.add(tool_id)

    def _metadata(self) -> dict:
        """Return reader health without allowing optional history to disable the dashboard."""
        return {"run_id": self.run_id, "indexing": not self.ready, "refreshing": self.refreshing,
                "issues": list(self.issues), "issue_count": self.issue_count,
                "pending_tail": sorted(self.pending_tail), "error": self.error,
                "through_seq": self.through_seq}

    def page(self, before: int | None, limit: int) -> dict:
        """Return newest decision summaries with a stable creation-order cursor."""
        self.refresh()
        if not self.ready:
            return {"run_id": self.run_id, "indexing": True, "decisions": [], "has_more": False}
        if not self.lock.acquire(timeout=INDEX_WAIT_SECONDS):
            return {"run_id": self.run_id, "indexing": True, "decisions": [], "has_more": False}
        try:
            groups = [group for group in self.groups.values() if before is None or group.cursor < before]
            groups.sort(key=lambda group: group.cursor, reverse=True)
            selected = groups[:limit]
            return {**self._metadata(), "decisions": [{**group.summary, "history_cursor": group.cursor,
                    "counts": {"tool": len(group.tools), "request": len(group.requests), "event": len(group.records)}}
                    for group in selected], "total_decisions": len(self.groups),
                    "next_before": selected[-1].cursor if len(groups) > limit else None,
                    "has_more": len(groups) > limit}
        finally:
            self.lock.release()

    def _read(self, ref: RecordRef) -> dict:
        """Seek directly to one indexed record; no list request reloads full journal bodies."""
        with (self.directory / ref.file).open("rb") as stream:
            stream.seek(ref.offset)
            return json.loads(stream.read(ref.size))

    def _event(self, ref: RecordRef, row: dict | None = None) -> dict:
        """Expose original public payloads with source identity and explicit correlation."""
        row = self._read(ref) if row is None else row
        kind, payload = _envelope(row, ref.file)
        context = {key: row[key] for key in ("run_id", "bridge_identity", "decision_id", "decision_episode_id", "decision_epoch", "action_id", "model_request_id") if key in row}
        if ref.decision_id:
            context["decision_id"] = ref.decision_id
        event = {"id": ref.ident, "timestamp_utc": ref.timestamp, "type": kind,
                 "payload": {**context, **payload}, "source": {"file": ref.file, "offset": ref.offset}}
        if ref.file == "dashboard-events.jsonl" and "seq" in row:
            event["seq"] = row["seq"]
        return event

    def detail(self, decision_id: str) -> dict:
        """Load full request, tool, simulation and receipt records for one explicit decision."""
        self.refresh()
        if not self.ready:
            return {"run_id": self.run_id, "indexing": True, "complete": False}
        if not self.lock.acquire(timeout=INDEX_WAIT_SECONDS):
            return {"run_id": self.run_id, "indexing": True, "complete": False}
        try:
            group = self.groups.get(decision_id)
            if group is None:
                raise KeyError(decision_id)
            refs, summary = list(group.records), dict(group.summary)
            metadata = self._metadata()
        finally:
            self.lock.release()
        requests, tools, events, simulations = {}, {}, [], []
        for ref in refs:
            row = self._read(ref)
            kind, payload = _envelope(row, ref.file)
            events.append(self._event(ref, row))
            request_id = row.get("model_request_id") or payload.get("model_request_id")
            if request_id:
                request = requests.setdefault(request_id, {"model_request_id": request_id, "decision_id": decision_id})
                if kind == "model_request_input":
                    request["input"] = payload
                elif kind.startswith("model_request_"):
                    request.update(_object(payload.get("metadata")) or payload)
                    response = _object(_object(row.get("data")).get("response"))
                    request.update(response)
                    if kind in {"model_request_completed", "model_request_failed", "model_request_cancelled"}:
                        request["status"] = kind.removeprefix("model_request_")
                    elif kind == "model_request_started":
                        request["status"] = "generating"
            tool_id = payload.get("tool_call_id")
            if tool_id and kind.startswith("tool_call_"):
                tools.setdefault(tool_id, {}).update(payload)
            if kind == "simulation":
                simulations.append({"timestamp_utc": ref.timestamp, **payload})
        return {**metadata, "decision": summary, "model_requests": list(requests.values()),
                "tool_calls": list(tools.values()), "events": events, "simulation": simulations,
                "complete": not metadata["indexing"]}

    def events(self, before: int | None, limit: int) -> dict:
        """Page independently unassociated session events without guessing a decision owner."""
        self.refresh()
        if not self.ready:
            return {"run_id": self.run_id, "indexing": True, "events": [], "has_more": False}
        if not self.lock.acquire(timeout=INDEX_WAIT_SECONDS):
            return {"run_id": self.run_id, "indexing": True, "events": [], "has_more": False}
        try:
            end = min(before if before is not None else len(self.session_events), len(self.session_events))
            begin = max(0, end - limit)
            refs = list(reversed(self.session_events[begin:end]))
            metadata = self._metadata()
            total = len(self.session_events)
        finally:
            self.lock.release()
        return {**metadata, "events": [self._event(ref) for ref in refs], "total_events": total,
                "next_before": begin if begin else None, "has_more": begin > 0}


class HistoryStore:
    """Resolve known run directories and cache a few disposable readers."""

    def __init__(self, root: Path):
        """Bind history to the project's fixed public runs directory."""
        self.directory = (Path(root) / "runs").resolve()
        self.lock = threading.Lock()
        self.runs = {}

    def get(self, run_id: str) -> RunHistory:
        """Accept a named existing manifest, never a caller-provided path or configuration."""
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run ID")
        directory = (self.directory / run_id).resolve()
        if directory.parent != self.directory or not (directory / "manifest.json").is_file():
            raise KeyError(run_id)
        with self.lock:
            if run_id not in self.runs:
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
                if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
                    raise KeyError(run_id)
                self.runs[run_id] = RunHistory(directory, run_id)
            history = self.runs.pop(run_id)
            self.runs[run_id] = history
            while len(self.runs) > CACHED_RUNS:
                self.runs.pop(next(iter(self.runs)))
            return history

"""Observe two live roots using public journals only; never call games or providers."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time


# Only these public runtime files are opened; no directory-wide/save discovery occurs.
JOURNALS = ("transitions.jsonl", "stage-timings.jsonl")
# Usage missing from a response remains unknown instead of silently becoming zero.
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
# These fields identify intended conditions without copying secrets from config.
PROVIDER_FIELDS = ("model", "reasoning_effort", "thinking", "context_max_turns",
                   "context_max_characters", "compact_simulation_tool_results",
                   "simulation_tool_compression_mode")
# Two runs are acceptance observations, not an estimate of relative win probability.
EXPERIMENT_LIMITS = [
    "Two independent live runs; identical save/unlocks do not guarantee identical random starts or paths.",
    "Effort is the intended treatment; reported configuration differences require separate review.",
    "No causal win-rate, non-inferiority, or matched-seed claim follows from two outcomes.",
    "Observed usage is not a provider invoice; failures and unknown usage remain included.",
    "Stage timings are inclusive and background work can overlap; do not sum stage totals as wall time.",
    "No save, hidden seed, hidden game state, model request, or game action is accessed by this monitor.",
]


def canonical(value):
    """Serialize evidence consistently for equality and compact content hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    """Hash public evidence without retaining large states in the live monitor."""
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(path):
    """Read one known public file, tolerating a concurrent write or absent file."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError) as error:
        return {}, type(error).__name__


def distribution(values):
    """Return exact nearest-rank timing summaries for the observed interval."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "total_ms": round(sum(ordered), 3),
            "p50_ms": ordered[max(0, (len(ordered) * 50 + 99) // 100 - 1)],
            "p95_ms": ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)]}


def classify_outcome(result):
    """Separate native outcome flags, assistance and technical stops from a win claim."""
    status = result.get("status")
    if status == "native_game_over":
        if result.get("victory") is True:
            return "native_victory_requires_terminal_definition"
        return "native_defeat" if result.get("victory") is False else "native_terminal_outcome_unknown"
    if status in {"technical_interruption", "budget_stop"}:
        return status
    return "other_stop" if result else "unfinished"


class Tail:
    """Read only complete newly appended JSONL records, retaining partial final lines."""

    def __init__(self, path):
        """Start from byte zero so a restarted monitor does not omit earlier spending."""
        self.path, self.offset, self.invalid, self.truncated = path, 0, 0, False

    def rows(self):
        """Stream new complete records; refuse silent double-counting after truncation."""
        if not self.path.exists() or self.truncated:
            return
        if self.path.stat().st_size < self.offset:
            self.truncated = True
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                self.offset = stream.tell()
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("non-object journal record")
                except (ValueError, UnicodeError):
                    self.invalid += 1
                    continue
                yield row


def expand_single_hop(envelope):
    """Decode locally and reject generated references inside referenced definitions."""
    definitions = envelope["definitions"]

    def walk(value, in_definition=False):
        """Interpret escaped literal dictionaries while counting actual reference hops."""
        if isinstance(value, list):
            return [walk(child, in_definition) for child in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"$ref"}:
            if in_definition:
                raise ValueError("nested reference")
            return walk(definitions[value["$ref"]], True)
        body = value["$literal"] if set(value) == {"$literal"} else value
        return {key: walk(child, in_definition) for key, child in body.items()}

    return walk(envelope["payload"])


class RunMonitor:
    """Keep a compact incremental evidence summary for one explicitly named runtime root."""

    def __init__(self, label, root, replay=None):
        """Initialize independent counters without changing the monitored runtime."""
        self.label, self.root, self.replay = label, Path(root), replay
        self.run_id, self.tails, self.snapshot = None, {}, {}
        self.requests, self.tools, self.tool_inputs = {}, {}, {}
        self.submissions, self.audits, self.exports = {}, {}, {}
        self.stages, self.support = defaultdict(list), Counter()
        self.errors, self.polls = Counter(), Counter()
        self.process_segments, self.technical_events = [], []
        self.last_progress = time.monotonic()
        self.last_marker = None
        self.read_errors = {}

    @staticmethod
    def identity(row):
        """Bind audits to the exact run, process, decision and native action."""
        return canonical([row.get("run_id"), row.get("bridge_identity"),
                          row.get("decision_id"), row.get("action_id")])

    def transition(self, row):
        """Reduce authoritative public events, preserving failures and observed identities."""
        kind, data = row.get("event_type"), row.get("data", {})
        if kind == "model_request":
            event, payload = data.get("provider_event_type", ""), data.get("payload", {})
            if event in {"model_request_completed", "model_request_failed"}:
                metadata = payload.get("metadata", payload)
                self.requests[payload.get("model_request_id", row.get("model_request_id"))] = {
                    key: metadata.get(key) for key in ("actual_model", "system_fingerprint", "requested_model",
                        "usage", "duration_ms", "error", "attempt_index", "repair_index", "finish_reason")}
                self.requests[payload.get("model_request_id", row.get("model_request_id"))]["event"] = event
            if event == "model_request_input":
                for message in payload.get("messages", []):
                    if message.get("role") != "tool":
                        continue
                    content = message.get("content", "")
                    tool_id = message.get("tool_call_id")
                    try:
                        body = json.loads(content)
                    except (ValueError, TypeError):
                        self.tool_inputs[digest([tool_id, content])] = {"tool_call_id": tool_id, "encoding": "invalid_json"}
                        continue
                    encoded = isinstance(body, dict) and body.get("encoding") == "public-shared-values-v1"
                    result = {"tool_call_id": tool_id, "encoding": "plain-json", "encoded_characters": len(content)}
                    if encoded:
                        result.update(encoding=body.get("compression_profile", "unknown"),
                                      local_scope=body.get("reference_scope") == "this_tool_result_only")
                        try:
                            restored = expand_single_hop(body)
                            result.update(single_hop=True, restored_hash=digest(restored),
                                          raw_characters=len(canonical(restored)))
                        except (ValueError, KeyError, TypeError) as error:
                            result.update(single_hop=False, error=type(error).__name__)
                    content_key = digest([tool_id, content])
                    request_ids = self.tool_inputs.get(content_key, {}).get("request_ids", [])
                    request_id = payload.get("model_request_id", row.get("model_request_id"))
                    if request_id not in request_ids:
                        request_ids.append(request_id)
                    self.tool_inputs[content_key] = {**result, "request_ids": request_ids}
        elif kind in {"tool_call_completed", "tool_call_failed"}:
            self.tools.setdefault(data.get("tool_call_id"), set()).add(digest(data.get("result")))
            if data.get("name") == "simulation_evaluate":
                self.collect_support(data.get("result", {}), "tool")
        elif kind == "action_submitted":
            self.submissions[self.identity(row)] = {
                "deferred": (data.get("prediction") or {}).get("status") == "deferred"}
        elif kind == "simulation":
            prediction = data.get("prediction")
            if isinstance(prediction, dict):
                self.collect_support(prediction, "prediction")
                if data.get("audit_mode") == "background":
                    self.audits[self.identity(row)] = digest(prediction)
            if isinstance(data.get("result"), dict):
                self.collect_support(data["result"], "advice")
        elif kind == "process_started":
            self.process_segments.append({"timestamp_utc": row.get("timestamp_utc"), "artifacts": data.get("artifacts")})
        elif kind in {"error", "action_rejected", "decision_rejected"}:
            reason = next((data[key] for key in ("error", "error_code", "message", "reason") if data.get(key)), None)
            self.errors[canonical({"event": kind, "error": reason})] += 1
            self.technical_events.append({"timestamp_utc": row.get("timestamp_utc"), "event": kind, "reason": reason})

    def collect_support(self, value, scope):
        """Count explicit statuses by scope; unsupported samples stay in denominators."""
        if not isinstance(value, dict):
            return
        self.support[scope + ":" + str(value.get("status", "unspecified"))] += 1
        result = value.get("result", value)
        if isinstance(result, dict):
            for candidate in result.get("candidates", []):
                for sample in candidate.get("samples", []):
                    self.support[scope + "_sample:" + str(sample.get("status", "unspecified"))] += 1

    def poll(self):
        """Read named public files, keeping run changes explicit rather than merging them."""
        state, error = read_json(self.root / "local/dashboard-session.json")
        if error:
            self.read_errors["snapshot"] = error
        else:
            self.read_errors.pop("snapshot", None)
        observed_id = state.get("run_id") if not error else self.snapshot.get("run_id")
        if not observed_id:
            return
        if self.run_id and observed_id != self.run_id:
            self.read_errors["run_changed"] = f"{self.run_id} -> {observed_id}; restart monitor for new run"
            return
        if not error:
            self.snapshot = state
        if not self.run_id:
            self.run_id = observed_id
            directory = self.root / "runs" / self.run_id
            self.tails = {name: Tail(directory / name) for name in JOURNALS}
            if self.replay:
                self.tails["export"] = Tail(Path(self.replay) / "public-replay.jsonl")
        marker = (self.snapshot.get("seq"), self.snapshot.get("phase"), self.snapshot.get("metrics", {}).get("actions_completed"))
        if marker != self.last_marker:
            self.last_progress, self.last_marker = time.monotonic(), marker
        for name, tail in self.tails.items():
            for row in tail.rows():
                if name == "transitions.jsonl":
                    self.transition(row)
                elif name == "export":
                    if row.get("record_type") == "transition":
                        self.exports[self.identity(row)] = digest(row.get("prediction"))
                else:
                    if isinstance(row.get("elapsed_ms"), (int, float)):
                        self.stages[row.get("stage", "unknown")].append(row["elapsed_ms"])
                    for key in ("polls", "not_ready_polls", "token_changes"):
                        self.polls[key] += row.get("details", {}).get(key, 0)

    def summary(self, stall_seconds):
        """Describe observed evidence and missing checks without treating absence as success."""
        state = self.snapshot
        directory = self.root / "runs" / self.run_id if self.run_id else None
        manifest, manifest_error = read_json(directory / "manifest.json") if directory else ({}, "run_unknown")
        result, _ = read_json(directory / "result.json") if directory else ({}, None)
        identities, usage, unknown = Counter(), Counter(), Counter()
        for request in self.requests.values():
            identities[canonical({key: request.get(key) for key in ("actual_model", "system_fingerprint", "requested_model")})] += 1
            for key in TOKEN_FIELDS:
                value = (request.get("usage") or {}).get(key)
                if type(value) is int:
                    usage[key] += value
                else:
                    unknown[key] += 1
        compressed = []
        for item in self.tool_inputs.values():
            if item["encoding"] in {"plain-json", "invalid_json"}:
                continue
            source = self.tools.get(item["tool_call_id"])
            completed_requests = sum(self.requests.get(request_id, {}).get("event") == "model_request_completed"
                                     for request_id in item.get("request_ids", []))
            compressed.append({**item, "completed_requests_containing_reply": completed_requests,
                "matches_original": item.get("restored_hash") in source if source else None})
        audits = [{"correlation": key, "submission_found": key in self.submissions,
                   "was_deferred": self.submissions.get(key, {}).get("deferred"),
                   "export_matches": self.exports[key] == value if key in self.exports else None}
                  for key, value in self.audits.items()]
        provider = manifest.get("config", {}).get("provider", {})
        segments = []
        for segment in self.process_segments:
            filename = segment.get("artifacts")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.startswith("artifacts-") or not filename.endswith(".json"):
                segments.append({**segment, "read_error": "invalid_artifact_filename"})
                continue
            artifact, artifact_error = read_json(directory / filename)
            segments.append({**segment, "read_error": artifact_error,
                "public_artifact_manifest_sha256": digest(artifact) if not artifact_error else None,
                "files_sha256": artifact.get("files_sha256"),
                "simulation_rule_identity": artifact.get("simulation_rule_identity"),
                "provider": {key: artifact.get("config", {}).get("provider", {}).get(key) for key in PROVIDER_FIELDS}})
        observation = state.get("observation") or {}
        public = observation.get("state", observation)
        idle = round(time.monotonic() - self.last_progress, 1)
        return {"label": self.label, "root": str(self.root.resolve()), "run_id": self.run_id,
            "snapshot": {key: state.get(key) for key in ("phase", "desired_state", "error", "request_pending",
                "assistance_mode", "terminal_evidence", "prediction_audit_warning", "metrics", "performance")},
            "public_progress": {"screen": public.get("screen"), "run": public.get("run")},
            "result": result, "outcome_classification": classify_outcome(result),
            "manifest_read_error": manifest_error,
            "intended_provider": {key: provider.get(key) for key in PROVIDER_FIELDS},
            "process_segments": segments,
            "paid_requests_observed": len(self.requests),
            "failed_requests": sum(row["event"] == "model_request_failed" for row in self.requests.values()),
            "paid_model_identities": [{"identity": json.loads(key), "requests": value} for key, value in identities.items()],
            "usage_known_totals": dict(usage), "usage_unknown_requests_by_field": dict(unknown),
            "request_duration": distribution([row["duration_ms"] for row in self.requests.values() if isinstance(row.get("duration_ms"), (int, float))]),
            "compression": {"unique_tool_replies_seen_in_requests": len(self.tool_inputs),
                "actual_compressed_replies": len(compressed), "checks": compressed,
                "scope": "Recorded request inputs, with completed-response linkage separately counted. Zero means not yet observed, not validated."},
            "background_audits": {"completed": len(audits), "checks": audits,
                "deferred_without_audit": sum(value["deferred"] and key not in self.audits for key, value in self.submissions.items()),
                "export_supplied": bool(self.replay)},
            "simulation_status_counts": dict(self.support),
            "stage_timings": {name: distribution(values) for name, values in self.stages.items()},
            "observation_polls": dict(self.polls), "technical_event_counts": dict(self.errors),
            "technical_events": self.technical_events,
            "inactivity": {"snapshot_unchanged_seconds": idle,
                "suspected_stall": idle >= stall_seconds and state.get("desired_state") == "running",
                "meaning": "Observation only; a long paid generation or intentionally paused controller is not a confirmed fault."},
            "read_errors": self.read_errors,
            "journal_integrity": {name: {"bytes_read": tail.offset, "invalid_complete_lines": tail.invalid,
                                         "truncated": tail.truncated} for name, tail in self.tails.items()}}


def paired_summary(monitors, stall_seconds):
    """Show actual treatment differences and independent outcomes without causal claims."""
    runs = [monitor.summary(stall_seconds) for monitor in monitors]
    differences = {key: {run["label"]: run["intended_provider"].get(key) for run in runs}
                   for key in PROVIDER_FIELDS
                   if len({canonical(run["intended_provider"].get(key)) for run in runs}) > 1}
    return {"created_utc": datetime.now(timezone.utc).isoformat(), "runs": runs,
            "provider_differences": differences, "experiment_limits": EXPERIMENT_LIMITS}


def main():
    """Poll two explicit roots, writing only the caller-selected monitoring output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--high-root", required=True, type=Path)
    parser.add_argument("--low-root", required=True, type=Path)
    parser.add_argument("--high-replay", type=Path)
    parser.add_argument("--low-replay", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=0)
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument("--stall-seconds", type=float, default=300)
    args = parser.parse_args()
    roots = [args.high_root.resolve(), args.low_root.resolve()]
    if roots[0] == roots[1]:
        parser.error("Two independent roots are required")
    if any(args.output.resolve().is_relative_to(root) for root in roots):
        parser.error("Monitoring output must be outside both runtime roots")
    monitors = [RunMonitor("high", roots[0], args.high_replay), RunMonitor("low", roots[1], args.low_replay)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0, args.duration_seconds)
    while True:
        for monitor in monitors:
            monitor.poll()
        result = paired_summary(monitors, args.stall_seconds)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({run["label"]: {"run_id": run["run_id"], "phase": run["snapshot"]["phase"],
            "paid_requests": run["paid_requests_observed"], "compressed": run["compression"]["actual_compressed_replies"],
            "audits": run["background_audits"]["completed"]} for run in result["runs"]}), flush=True)
        if time.monotonic() >= deadline:
            return
        time.sleep(min(max(0.1, args.interval_seconds), max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()

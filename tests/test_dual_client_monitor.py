"""Check read-only dual-run evidence accounting and exact compression correlation."""

import json
from pathlib import Path
import sys

import pytest

# Import the standalone experiment without changing production import paths.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/dual_client"))
from monitor import RunMonitor, Tail, classify_outcome, digest, expand_single_hop


def test_tail_waits_for_complete_line_and_refuses_truncation(tmp_path):
    """Concurrent partial writes must neither disappear nor count twice."""
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":')
    reader = Tail(path)
    assert list(reader.rows()) == [{"a": 1}]
    assert list(reader.rows()) == []
    with path.open("ab") as stream:
        stream.write(b'2}\n')
    assert list(reader.rows()) == [{"b": 2}]
    path.write_bytes(b'{}\n')
    assert list(reader.rows()) == []
    assert reader.truncated


def test_single_hop_rejects_nested_but_preserves_literal_references():
    """Literal marker-shaped data is not confused with a generated second hop."""
    envelope = {"definitions": {"a": {"$literal": {"$ref": "original"}}}, "payload": {"$ref": "a"}}
    assert expand_single_hop(envelope) == {"$ref": "original"}
    envelope["definitions"]["a"] = {"$ref": "b"}
    envelope["definitions"]["b"] = 1
    with pytest.raises(ValueError, match="nested"):
        expand_single_hop(envelope)


def test_live_evidence_is_deduplicated_and_failures_keep_usage(tmp_path):
    """Actual compressed sends, failed paid requests and exact audit bindings survive polling."""
    root = tmp_path / "high"
    (root / "local").mkdir(parents=True)
    run = root / "runs/r1"
    run.mkdir(parents=True)
    (root / "local/dashboard-session.json").write_text(json.dumps({"run_id": "r1", "seq": 2}), encoding="utf-8")
    (run / "manifest.json").write_text('{"config":{"provider":{"reasoning_effort":"high"}}}', encoding="utf-8")
    (run / "artifacts-first.json").write_text('{"files_sha256":{"session.py":"first"}}', encoding="utf-8")
    monitor = RunMonitor("high", root)
    base = {"run_id": "r1", "bridge_identity": {"pid": 1, "instance_id": "one"},
            "decision_id": "d1", "action_id": "a1"}
    raw = {"result": {"status": "unsupported", "text": "preserved"}}
    packed = {"encoding": "public-shared-values-v1", "compression_profile": "static-and-state-single-hop-v1",
              "reference_scope": "this_tool_result_only", "definitions": {"a": raw}, "payload": {"$ref": "a"}}
    prediction = {"status": "unsupported", "errors": ["known gap"]}
    records = [
        {**base, "event_type": "process_started", "data": {"artifacts": "artifacts-first.json"}},
        {**base, "event_type": "tool_call_completed", "data": {"tool_call_id": "t1", "name": "simulation_evaluate", "result": raw}},
        {**base, "event_type": "model_request", "data": {"provider_event_type": "model_request_input", "payload": {"messages": [
            {"role": "tool", "tool_call_id": "t1", "content": json.dumps(packed)}]}}},
        {**base, "event_type": "model_request", "data": {"provider_event_type": "model_request_failed", "payload": {
            "model_request_id": "paid1", "actual_model": "deepseek-flash", "system_fingerprint": "fp",
            "usage": {"completion_tokens": 32768}, "duration_ms": 1200}}},
        {**base, "event_type": "action_submitted", "data": {"prediction": {"status": "deferred"}}},
        {**base, "event_type": "simulation", "data": {"audit_mode": "background", "prediction": prediction}},
        {**base, "bridge_identity": {"pid": 2, "instance_id": "other"}, "event_type": "simulation",
            "data": {"audit_mode": "background", "prediction": prediction}},
    ]
    (run / "transitions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    (run / "stage-timings.jsonl").write_text('{"stage":"background_prediction","elapsed_ms":12,"inclusive":true}\n', encoding="utf-8")
    monitor.poll()
    monitor.poll()
    report = monitor.summary(300)
    assert report["paid_requests_observed"] == 1
    assert report["failed_requests"] == 1
    assert report["usage_known_totals"]["completion_tokens"] == 32768
    assert report["usage_unknown_requests_by_field"]["prompt_tokens"] == 1
    assert report["compression"]["actual_compressed_replies"] == 1
    assert report["compression"]["checks"][0]["matches_original"] is True
    checks = report["background_audits"]["checks"]
    assert [row["submission_found"] for row in checks] == [True, False]
    assert all(row["export_matches"] is None for row in checks)
    monitor.exports[monitor.identity(base)] = digest(prediction)
    assert monitor.summary(300)["background_audits"]["checks"][0]["export_matches"] is True
    assert report["stage_timings"]["background_prediction"]["count"] == 1
    assert report["process_segments"][0]["files_sha256"] == {"session.py": "first"}


def test_terminal_flags_are_not_promoted_to_wins():
    """Special victory and assistance remain separate from an autonomous full-game win."""
    assert classify_outcome({"status": "native_game_over", "victory": True}) == "native_victory_requires_terminal_definition"
    assert classify_outcome({"status": "technical_interruption"}) == "technical_interruption"
    assert classify_outcome({}) == "unfinished"

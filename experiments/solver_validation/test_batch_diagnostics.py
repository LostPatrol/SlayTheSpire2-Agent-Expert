"""Check diagnostic coverage, literal replay identities, and real child resource accounting."""

import json
import os
import sys

from batch_diagnostics import error_families, fixture_groups, json_digest, replay_cells, replay_comparison, run_batch, summarize


def diagnostic_record():
    """Describe repeated and newly discovered failures beyond one cell's first failure."""
    projection = {"public_state": {"screen": "COMBAT", "run": {"character_id": "IRONCLAD"}}}
    first = {"kind": "combat_import", "floor": 7, "projection": projection, "import_errors": ["A"]}
    second = {"kind": "combat_import", "floor": 8, "projection": projection, "import_errors": ["B"]}
    return {"cell": "audit-ironclad-0", "request": {"op": "alignment_run", "mode": "audit"},
        "response": {"ok": True, "outcome": "won", "batch_diagnostics": {
            "checks": 8, "failed_checks": 3, "checks_after_first_failure": 5, "first_failure_step": 10,
            "errors": [{"step": 10, "signature": "A", "group_key": "combat_import|7|A"},
                       {"step": 11, "signature": "A", "group_key": "combat_import|7|A"},
                       {"step": 12, "signature": "B", "group_key": "combat_import|8|B"}],
            "groups": [{"signature": "A", "group_key": "combat_import|7|A", "count": 2,
                        "first_step": 10, "last_step": 11, "first_record": first},
                       {"signature": "B", "group_key": "combat_import|8|B", "count": 1,
                        "first_step": 12, "last_step": 12, "first_record": second}]}}}


def test_clustering_keeps_complete_groups_and_post_failure_discoveries(tmp_path):
    """Coverage includes later checks and exact signatures while every original group is replayable."""
    record = diagnostic_record()
    coverage = fixture_groups([record, {**record, "cell": "audit-silent-0"}], tmp_path)
    assert coverage["checks"] == 16
    assert coverage["checks_after_first_failure"] == 10
    assert coverage["exported_groups"] == coverage["replayable_groups"] == 4
    assert coverage["signatures_found_only_after_first_failures_across_panel"] == ["B"]
    clusters = json.loads((tmp_path / "clusters.json").read_text(encoding="utf-8"))
    assert clusters[0]["signature"] == "A"
    assert clusters[0]["occurrences"] == 4
    assert clusters[0]["distinct_cells"] == 2
    fixtures = replay_cells(tmp_path)
    assert len(fixtures) == 4
    _, request, baseline = fixtures[0]
    assert request["kind"] == "combat_import"
    assert request["projection"] == record["response"]["batch_diagnostics"]["groups"][0]["first_record"]["projection"]
    comparison = replay_comparison({"cell": "replay", "status": "completed", "request_sha256": json_digest(request),
        "response": {"ok": True, "errors": []}}, baseline)
    assert comparison["input_request_hash_equal"] is True
    assert comparison["removed_errors"] == ["A"]
    assert comparison["baseline_signature_absent"] is True


def test_replay_rejects_changed_input_and_keeps_unavailable_unknown(tmp_path):
    """A modified fixture cannot silently become a different regression input."""
    fixture_groups([diagnostic_record()], tmp_path)
    sources = sorted((tmp_path / "fixtures").rglob("*.json"))
    fixture = json.loads(sources[0].read_text(encoding="utf-8"))
    fixture["request"]["projection"]["public_state"]["screen"] = "MAP"
    sources[0].write_text(json.dumps(fixture), encoding="utf-8")
    try:
        replay_cells(tmp_path)
    except ValueError as error:
        assert "hash changed" in str(error)
    else:
        raise AssertionError("changed input was accepted")
    comparison = replay_comparison({"cell": "replay", "status": "timeout", "request_sha256": "hash"},
        {"baseline_errors": ["A"], "signature": "A", "input_request_sha256": "hash"})
    assert comparison["removed_errors"] is None
    assert comparison["baseline_signature_absent"] is None


def test_audit_terminal_never_becomes_public_winrate():
    """Audit driver wins are labeled separately, while strict public halts stay in the denominator."""
    rows = [{"cell": "a", "status": "completed", "outcome": "won", "terminal": True},
            {"cell": "b", "status": "noncompletion", "outcome": "policy_halt", "terminal": False},
            {"cell": "c", "status": "response_error", "outcome": "won", "terminal": False},
            {"cell": "d", "status": "timeout"}]
    audit = summarize(rows, "audit")
    assert "public_policy" not in audit
    assert audit["diagnostic_driver"]["terminal"] == 1
    strict = summarize(rows, "public")["public_policy"]
    assert strict["wins"] == 1
    assert strict["incomplete"] == 3
    assert strict["win_fraction_all_attempts"] == 0.25


def test_parallel_children_preserve_bytes_and_measured_resources(tmp_path):
    """Exercise real concurrent processes, bounded timeout, and Windows CPU/working-set counters."""
    # A real child consumes CPU before sleeping, making resource readings independently observable.
    script = """import json, os, sys, time
request = json.loads(sys.stdin.readline())
sys.stderr.write('child stderr evidence\\n')
sys.stderr.flush()
started = time.process_time()
while time.process_time() - started < 0.12:
    pass
time.sleep(0.08 if request['seed'] != 3 else 5)
print('non-json startup line')
print(json.dumps({'ok': True, 'outcome': 'won', 'echo': request, 'pid': os.getpid()}))
"""
    cells = [(f"cell-{seed}", {"op": "alignment_run", "mode": "public", "seed": seed}, None)
             for seed in (1, 2, 3)]
    # Windows venv python.exe redirects to a descendant; measure the project's actual base interpreter.
    executable = sys._base_executable if os.name == "nt" else sys.executable
    summary = run_batch([executable, "-c", script], cells, tmp_path, jobs=2,
                        timeout=1, interval=0.05, mode="public")
    assert summary["attempts"] == 3
    assert summary["statuses"] == {"completed": 2, "timeout": 1}
    assert summary["resources"]["peak_concurrent_processes"] == 2
    assert summary["resources"]["total_wall_seconds"] >= 1
    assert (tmp_path / "cell-1" / "stderr.log").read_text(encoding="utf-8") == "child stderr evidence\n"
    assert "non-json startup line" in (tmp_path / "cell-1" / "stdout.jsonl").read_text(encoding="utf-8")
    response = json.loads((tmp_path / "cell-1" / "response.json").read_text(encoding="utf-8"))
    assert response["echo"] == cells[0][1]
    attempt = json.loads((tmp_path / "cell-1" / "attempt.json").read_text(encoding="utf-8"))
    assert response["pid"] == attempt["pid"]
    samples = [json.loads(line) for line in (tmp_path / "resource-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(samples) > 3
    if os.name == "nt":
        resources = summary["resources"]
        assert resources["cpu_known_children"] == 3
        assert sum(resources["batch_child_cpu_seconds"].values()) > 0.2
        assert resources["batch_cpu_percent_of_logical_capacity"] > 0
        assert resources["system_cpu_busy_percent"] is not None
        assert resources["peak_sampled_combined_working_set_bytes"] > 0
        assert any(sample["memory_known_active_children"] == 2 for sample in samples)


def test_replay_batch_compares_added_errors_and_preserves_unreplayable(tmp_path):
    """Literal replays keep repaired signatures, newly reported errors, and missing projections distinct."""
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    row = diagnostic_record()
    missing = {"signature": "missing_projection", "group_key": "projection|9|missing_projection", "count": 1,
               "first_record": {"kind": "projection", "floor": 9, "projection_error": "missing_projection"}}
    row["response"]["batch_diagnostics"]["groups"].append(missing)
    fixture_groups([row], baseline)
    output = tmp_path / "replay"
    output.mkdir()
    script = """import json, sys
request = json.loads(sys.stdin.readline())
assert request['op'] == 'alignment_run' and request['diagnostic'] == 'public_fixture'
print(json.dumps({'ok': False, 'errors': ['new_error'], 'stage_milliseconds': {'import': 2}}))
"""
    result = run_batch([sys.executable, "-c", script], replay_cells(baseline), output,
                       jobs=2, timeout=5, interval=0.05, mode="replay")
    assert result["attempts"] == 3
    assert result["statuses"] == {"fixture_unreplayable": 1, "response_error": 2}
    assert result["replay"]["unknown_or_unreplayable"] == 1
    assert result["replay"]["fixtures_with_added_errors"] == 2
    assert result["host_stage_milliseconds"]["import"] == {
        "sum_reported": 4, "known_cells": 2, "unknown_cells": 1}
    attempts = (output / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(attempts) == 3
    comparisons = json.loads((output / "replay-comparisons.json").read_text(encoding="utf-8"))
    assert all(row["input_request_hash_equal"] for row in comparisons)
    assert all(row["added_errors"] == ["new_error"] for row in comparisons if row["replay_errors"] is not None)


def test_symptom_families_strip_only_verified_card_handle_and_keep_numeric_evidence():
    """Different handles merge, while card IDs, costs, and non-card prefixes remain separate."""
    suffix = "FRANTIC_ESCAPE:card_energy_cost_not_reconstructible:2:core_expected:1"
    changed_cost = "FRANTIC_ESCAPE:card_energy_cost_not_reconstructible:3:core_expected:1"
    signatures = ["s21:" + suffix, "h0:" + suffix, "s99:" + changed_cost,
                  "e21:" + suffix, "nested:s21:" + suffix]
    record = {"cell": "audit", "response": {"batch_diagnostics": {"groups": [
        {"signature": signature, "count": 1} for signature in signatures]}}}
    families = error_families([record])
    assert len(families) == 4
    merged = next(row for row in families if row["family"] == suffix)
    assert merged["occurrences"] == 2
    assert merged["signatures"] == ["h0:" + suffix, "s21:" + suffix]
    assert any(row["family"] == changed_cost for row in families)
    assert any(row["family"] == "e21:" + suffix for row in families)
    assert any(row["family"] == "nested:s21:" + suffix for row in families)


def test_strict_cell_summary_retains_last_known_floor_action_count_and_adapter_reason():
    """Compact summaries preserve original counters and distinguish inferred floors from exact terminal state."""
    rows = [{"cell": "public", "status": "noncompletion", "outcome": "policy_halt", "terminal": False,
        "request": {"character": "silent", "seed": 719}, "response": {
            "steps": [{"before": {"floor": 7}}, {"before": {"floor": 8}}],
            "decisions": [{"floor": 9}], "action_trace_count": 2,
            "native_commands_selected": 3, "adapter_error": "alignment_public_combat_blocked"}}]
    cell = summarize(rows, "public")["cells"][0]
    assert cell["floor"] == 9
    assert cell["floor_source"] == "latest_recorded_pre_action_or_decision_floor"
    assert cell["action_count"] == 2
    assert cell["native_commands_selected"] == 3
    assert cell["adapter_error"] == "alignment_public_combat_blocked"

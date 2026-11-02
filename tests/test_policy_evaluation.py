"""Validate experimental evidence classification and lossless history reconstruction."""

import importlib.util
import json
from pathlib import Path

# Load the standalone experiment without making it a production dependency.
SCRIPT = Path(__file__).resolve().parents[1] / "experiments/policy_evaluation/benchmark.py"
SPEC = importlib.util.spec_from_file_location("policy_benchmark", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_shortened_history_rebuilds_dropped_shared_definitions():
    """A remaining root must expand even when its original definition owner is removed."""
    known, messages = {}, [{"role": "system", "content": "fixed"}]
    roots = []
    for index in range(4):
        root = {"observation": {"state": {"turn": index, "rules": ["long stable rule" * 60]}}, "memory": {}}
        roots.append(root)
        projection = MODULE.compact_public(root, known)
        known.update(projection["definitions"])
        messages.append({"role": "user", "content": MODULE.serialize_projection(projection)})
        if index < 3:
            messages.append({"role": "assistant", "content": "{}", "reasoning_content": "complete reasoning"})
    shortened, dropped = MODULE.rebuild(messages, 1, 640000)
    assert dropped == 2
    reconstructed = MODULE.expanded_turns(shortened)
    assert [row["payload"] for row in reconstructed] == roots[-2:]
    assert reconstructed[0]["messages"][1]["reasoning_content"] == "complete reasoning"


def test_tool_continuation_remains_in_the_current_turn():
    """Trimming old turns must never split an active tool-call protocol exchange."""
    payload = {"observation": {"state": {"turn": 1}}}
    projection = MODULE.compact_public(payload)
    messages = [{"role": "system", "content": "fixed"},
                {"role": "user", "content": MODULE.serialize_projection(projection)},
                {"role": "assistant", "reasoning_content": "full", "content": "", "tool_calls": [{"id": "t"}]},
                {"role": "tool", "tool_call_id": "t", "content": "result"}]
    rebuilt, dropped = MODULE.rebuild(messages, 0, 1)
    assert rebuilt[2:] == messages[2:]
    assert dropped == 0


def test_character_budget_applies_after_reintroducing_shared_values():
    """Dropped definition owners cannot hide a retained turn's reconstructed cost."""
    known, messages = {}, [{"role": "system", "content": "fixed"}]
    for index in range(3):
        root = {"observation": {"turn": index, "rules": "large complete public rule " * 300}}
        projection = MODULE.compact_public(root, known)
        known.update(projection["definitions"])
        messages.append({"role": "user", "content": MODULE.serialize_projection(projection)})
        if index < 2:
            messages.append({"role": "assistant", "content": "{}"})
    rebuilt, dropped = MODULE.rebuild(messages, 1, 1000)
    assert dropped == 2
    assert len(MODULE.expanded_turns(rebuilt)) == 1
    assert MODULE.expanded_turns(rebuilt)[0]["payload"]["observation"]["turn"] == 2


def test_victory_flag_does_not_imply_autonomous_or_frozen(tmp_path):
    """Special native victory and manual assistance remain independent dimensions."""
    (tmp_path / "manifest.json").write_text(json.dumps({"run_id": "r", "assistance_mode": "human_assisted"}))
    (tmp_path / "result.json").write_text(json.dumps({"status": "native_game_over", "victory": True}))
    (tmp_path / "provider-usage.jsonl").write_text(json.dumps({"usage": {"prompt_tokens": None}}) + "\n")
    result = MODULE.summarize(tmp_path)
    assert result["outcome_class"] == "native_victory_requires_terminal_definition"
    assert result["assistance_mode"] == "human_assisted"
    assert result["frozen_experiment"] is False
    assert result["unknown_usage"] == 1


def test_missing_terminal_outcome_and_budget_stop_are_not_losses(tmp_path):
    """Technical, budget, and missing native outcomes must not bias a defeat denominator."""
    for terminal, expected in [({"status": "native_game_over"}, "native_terminal_outcome_unknown"),
                               ({"status": "technical_interruption"}, "technical_interruption"),
                               ({"status": "budget_stop"}, "budget_stop")]:
        (tmp_path / "result.json").write_text(json.dumps(terminal))
        assert MODULE.summarize(tmp_path)["outcome_class"] == expected


def test_legal_action_set_respects_target_and_enabled_state():
    """Historical contract labels exclude stale targets and disabled options."""
    observation = {"available_actions": ["play_card", "end_turn"], "state": {"combat": {"hand": [
        {"i": 3, "playable": True, "targets": [8]}, {"i": 4, "playable": False, "targets": []}]}}}
    assert MODULE.legal_actions(observation) == [{"type": "play_card", "card_index": 3, "target_index": 8}, {"type": "end_turn"}]


def test_evidence_outputs_do_not_overwrite_previous_results(tmp_path):
    """Experimental reruns preserve earlier artifacts instead of replacing their provenance."""
    target = tmp_path / "result.json"
    MODULE.write_new(target, {"old": True})
    try:
        MODULE.write_new(target, {"new": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing evidence was overwritten")
    assert json.loads(target.read_text()) == {"old": True}


def test_inclusive_stage_metrics_are_separate_from_wall_time(tmp_path):
    """Nested timings preserve their distributions without double-counted wall estimates."""
    events = [{"stage": "model_decision", "elapsed_ms": 1000, "inclusive": True},
              {"stage": "ledger_persist", "elapsed_ms": 50, "inclusive": True},
              {"stage": "decision_observation", "elapsed_ms": 400, "inclusive": True,
               "details": {"polls": 3, "not_ready_polls": 1, "token_changes": 1}}]
    (tmp_path / "stage-timings.jsonl").write_text("\n".join(json.dumps(row) for row in events) + "\n")
    result = MODULE.summarize(tmp_path)
    assert result["stage_timings_ms"]["model_decision"]["sum"] == 1000
    assert result["observation_poll_work"]["decision_observation"]["polls"] == 3
    assert result["wall_seconds"] is None
    assert "overlap" in result["stage_timing_limit"]


def test_freeze_drift_is_reported_without_claiming_live_identity(tmp_path):
    """Changing one captured source produces a reviewable drift record, not a runtime gate."""
    source = tmp_path / "source.txt"
    source.write_text("before")
    manifest = {"files": [MODULE.file_identity(source)]}
    source.write_text("after")
    result = MODULE.verify_freeze(manifest)
    assert not result["unchanged"]
    assert len(result["changes"]) == 1
    assert result["live_process_verified"] is False


def test_freeze_includes_the_entire_production_python_chain(tmp_path, monkeypatch):
    """Previously omitted execution and memory modules must participate in drift checks."""
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    package = tmp_path / "src/sts2_agent"
    package.mkdir(parents=True)
    names = ["session.py", "public.py", "automatic.py", "memory.py", "instructions.py", "bridge.py", "rules.py"]
    for name in names:
        (package / name).write_text("# before\n")
    manifest = MODULE.freeze({"provider": {"model": "deepseek-flash"}})
    included = {Path(item["path"]).name for item in manifest["files"]}
    assert set(names).issubset(included)
    (package / "automatic.py").write_text("# changed policy\n")
    drift = MODULE.verify_freeze(manifest)
    assert not drift["unchanged"]
    assert any(Path(item["path"]).name == "automatic.py" for item in drift["changes"])

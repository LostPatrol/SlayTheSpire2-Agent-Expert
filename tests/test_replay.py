"""Exercise public replay correlation, settlement evidence, and damaged journals."""

from copy import deepcopy
import json

from sts2_agent.replay import build_replay, compare_prediction, export_replay


def state(revision=4, *, screen="COMBAT", ready=True, turn=1):
    """Create a small public frame with explicit action admission gates."""
    return {"screen": screen, "action_revision": revision, "turn": turn, "supported": True,
            "available_actions": ["play_card"],
            "combat": {"action_readiness": {key: ready for key in (
                "can_use_combat_actions", "actions_settled", "snapshot_stable")},
                "player": {"hp": "20/30", "block": 0, "energy": 3, "stars": 0, "powers": []},
                "enemies": [{"i": 0, "enemy_id": "A", "hp": "10/10", "block": 0, "powers": []}]}}


def observation(value):
    """Wrap a public frame in its real controller session identity."""
    return {"run_id": "run", "decision_episode_id": "run:1", "state": value}


def event(kind, second, data, *, action_id=None, identity=None, run_id="run"):
    """Build the agreed independent transition journal envelope."""
    return {"schema_version": 1, "timestamp_utc": f"2026-10-27T00:00:{second:02d}+00:00",
            "event_type": kind, "run_id": run_id, "decision_id": "d1", "decision_episode_id": "run:1",
            "bridge_identity": identity or {"pid": 10, "instance_id": "instance"},
            "action_id": action_id, "data": data}


def journal(tmp_path, rows):
    """Save one immutable synthetic public journal for a focused association test."""
    (tmp_path / "manifest.json").write_text(json.dumps({"run_id": "run"}), encoding="utf-8")
    (tmp_path / "transitions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def action_rows():
    """Provide one accepted action whose receipt embeds a misleading unstable frame."""
    before, after = state(), state(5)
    after["combat"]["enemies"][0]["hp"] = "4/10"
    prediction = {"ok": True, "state": deepcopy(after["combat"])}
    return [event("action_submitted", 1, {"action": {"type": "play_card", "card_index": 0},
                                         "observation": observation(before), "prediction": prediction}, action_id="a1"),
            event("action_receipt", 2, {"receipt": {"action_id": "a1", "status": "completed",
                                                     "stable": True, "public_state": state(5, ready=False)}}, action_id="a1"),
            event("observation", 3, {"observation": observation(state(5, ready=False)), "settled": True}),
            event("observation", 4, {"observation": observation(after), "settled": True})]


def transitions(result):
    """Select exported action records without hiding observation or decision records."""
    return [row for row in result["records"] if row["record_type"] == "transition"]


def test_true_settlement_wins_over_receipt_and_unready_frame(tmp_path):
    """Receipt.stable and a controller flag cannot bypass the three combat gates."""
    journal(tmp_path, action_rows())
    result = build_replay(tmp_path)
    row = transitions(result)[0]
    assert row["after_source"]["line"] == 4
    assert row["comparison"]["status"] == "match"
    assert result["manifest"]["summary"]["observations"] == 2


def test_deferred_prediction_uses_only_the_matching_completed_audit(tmp_path):
    """Late audits replace a placeholder without borrowing another action or process result."""
    rows = action_rows()
    prediction = rows[0]["data"]["prediction"]
    rows[0]["data"]["prediction"] = {"status": "deferred"}
    audit = {"prediction": prediction, "audit_mode": "background"}
    matching = event("simulation", 5, audit, action_id="a1")
    rows.extend([matching, event("simulation", 6, audit, action_id="other"),
                 event("simulation", 7, audit),
                 event("simulation", 8, audit, action_id="a1",
                       identity={"pid": 99, "instance_id": "foreign"})])
    journal(tmp_path, rows)
    assert transitions(build_replay(tmp_path))[0]["comparison"]["status"] == "match"
    rows.remove(matching)
    journal(tmp_path, rows)
    exported = transitions(build_replay(tmp_path))[0]
    assert exported["prediction"] == {"status": "deferred"}
    assert exported["comparison"]["status"] == "unavailable"


def test_revision_and_receipt_prevent_stale_proposal_borrowing(tmp_path):
    """An unaccepted proposal cannot borrow another action's revision or successor."""
    rows = action_rows()
    stale = deepcopy(rows[0])
    stale["action_id"] = "stale"
    stale["timestamp_utc"] = "2026-10-27T00:00:00+00:00"
    rows.insert(0, stale)
    rows[2]["data"]["receipt"]["public_state"]["action_revision"] = 6
    journal(tmp_path, rows)
    result = transitions(build_replay(tmp_path))
    assert all(row["after"] is None for row in result)
    assert "accepted_receipt_missing" in result[0]["gaps"]
    assert "receipt_revision_mismatch" in result[1]["gaps"]


def test_foreign_identity_and_later_action_cannot_supply_after(tmp_path):
    """A successor must remain inside its action's session and submission interval."""
    rows = action_rows()
    rows[3]["bridge_identity"] = {"pid": 11, "instance_id": "another"}
    rows.append(event("action_submitted", 5, {"action": {"type": "end_turn"},
                                            "observation": observation(state(5))}, action_id="a2"))
    rows.append(event("observation", 6, {"observation": observation(state(5)), "settled": True}))
    journal(tmp_path, rows)
    row = transitions(build_replay(tmp_path))[0]
    assert row["accepted"] is True
    assert row["after"] is None
    assert "settled_successor_missing_at_exact_revision" in row["gaps"]


def test_screen_transition_and_end_turn_remain_visible(tmp_path):
    """UI boundaries and next-turn actions are exported without false numeric mismatches."""
    rows = action_rows()
    rows[3]["data"]["observation"] = observation(state(5, screen="CARD_SELECTION"))
    rows.append(event("action_submitted", 5, {"action": {"type": "end_turn"},
                                            "observation": observation(state(5))}, action_id="a2"))
    rows.append(event("action_completed", 6, {"observation": observation(state(6, turn=2)),
                                               "receipt": {"action_id": "a2", "status": "completed",
                                                           "public_state": state(6)}}, action_id="a2"))
    rows.append(event("action_receipt", 6, {"receipt": {"action_id": "a2", "status": "completed",
                                                        "public_state": state(6)}}, action_id="a2"))
    journal(tmp_path, rows)
    result = transitions(build_replay(tmp_path))
    assert result[0]["category"] == "screen:COMBAT->CARD_SELECTION"
    assert result[0]["comparison"]["status"] == "boundary"
    assert result[1]["category"] == "combat_turn"


def test_corrupt_tail_and_duplicate_are_reported_with_lines(tmp_path):
    """Incomplete or repeated material cannot silently become a complete replay."""
    rows = action_rows()
    journal(tmp_path, rows)
    with (tmp_path / "transitions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(rows[0]) + "\n")
        stream.write('{"event_type":')
    result = build_replay(tmp_path)
    issues = result["manifest"]["issues"]
    assert any(item["code"] == "duplicate_record" and item["line"] == 5 for item in issues)
    assert any(item["code"] == "invalid_json" and item["line"] == 6 for item in issues)
    assert any(item["code"] == "unterminated_line" and item["line"] == 6 for item in issues)


def test_legacy_joins_public_decision_metadata_and_marks_weaker_screen_evidence(tmp_path):
    """Old public logs remain useful without inventing model inputs or stable tokens."""
    (tmp_path / "manifest.json").write_text(json.dumps({"run_id": "run"}), encoding="utf-8")
    rows = action_rows()
    request = {**rows[0]["data"], "timestamp_utc": rows[0]["timestamp_utc"],
               "action_id": "a1", "decision_id": "d1", "decision_episode_id": "run:1"}
    receipt = {**rows[1]["data"]["receipt"], "timestamp_utc": rows[1]["timestamp_utc"]}
    after = {"timestamp_utc": rows[3]["timestamp_utc"], "observation": observation(state(5, screen="REWARD"))}
    streams = {"execution-requests": request, "execution-receipts": receipt, "public-events": after,
               "decisions": {"decision_id": "d1", "action": request["action"], "timestamp_utc": rows[0]["timestamp_utc"]},
               "provider-usage": {"decision_id": "d1", "model_request_id": "m1", "timestamp_utc": rows[0]["timestamp_utc"]}}
    for name, row in streams.items():
        (tmp_path / (name + ".jsonl")).write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = build_replay(tmp_path)
    row = transitions(result)[0]
    assert row["category"] == "screen:COMBAT->REWARD"
    assert "legacy_noncombat_token_stability_not_recorded" in row["gaps"]
    decision = next(item for item in result["records"] if item["record_type"] == "decision")
    assert decision["model_requests"][0]["payload"]["model_request_id"] == "m1"
    paths = export_replay(tmp_path, tmp_path / "export")
    assert paths["summary"]["actions"] == 1


def test_public_comparator_keeps_missing_values_and_survivor_identity():
    """Missing energy and a Power amount error remain visible after an enemy dies."""
    after = state()
    after["combat"]["enemies"][0]["powers"] = ["VULNERABLE_POWER 2 [debuff]"]
    prediction = deepcopy(after["combat"])
    prediction["enemies"].insert(0, {"i": 8, "enemy_id": "DEAD", "hp": "0/8", "alive": False})
    prediction["enemies"][1]["i"] = 12
    prediction["enemies"][1]["powers"] = ["VULNERABLE_POWER 1 [debuff]"]
    del prediction["player"]["energy"]
    result = compare_prediction(prediction, after)
    assert {item["field"] for item in result["differences"]} == {"player.energy", "enemies[0].powers"}


def test_native_completion_can_restore_receipt_and_preserves_decision_inputs(tmp_path):
    """The independent journal alone carries model inputs, projection, and settled action evidence."""
    rows = action_rows()
    after = rows[3]["data"]["observation"]
    prediction = {"status": "predicted", "state": deepcopy(after["state"]["combat"])}
    rows[0]["data"]["prediction"] = prediction
    completed = event("action_completed", 4, {"observation": after,
                      "receipt": rows[1]["data"]["receipt"]}, action_id="a1")
    rows = [event("decision_input", 0, {"observation": observation(state()), "memory": {}, "instructions": []}),
            event("model_request", 0, {"provider_event_type": "model_request_started",
                                        "payload": {"messages": [{"role": "user", "content": "public input"}]}}),
            event("simulation", 0, {"result": {"results": {"beam": {"score": 3}}}, "prediction": prediction}),
            event("decision", 0, {"decision": {"action": {"type": "play_card"}, "reason": "Damage"}}),
            rows[0], rows[3], completed]
    journal(tmp_path, rows)
    result = build_replay(tmp_path)
    row = transitions(result)[0]
    assert row["after_source"]["line"] == 7
    assert row["accepted"] is True
    assert row["comparison"]["status"] == "match"
    assert "action_completed_not_recorded" not in row["gaps"]
    decision = next(item for item in result["records"] if item["record_type"] == "decision")
    assert decision["inputs"][0]["observation"]["state"]["action_revision"] == 4
    assert decision["model_requests"][0]["payload"]["messages"][0]["content"] == "public input"
    assert decision["gaps"] == []


def test_duplicate_logical_submission_is_explicit_and_not_inferred(tmp_path):
    """Conflicting repeated command identities cannot produce a trusted successor."""
    rows = action_rows()
    duplicate = deepcopy(rows[0])
    duplicate["timestamp_utc"] = "2026-10-27T00:00:02+00:00"
    duplicate["data"]["action"]["card_index"] = 2
    rows.append(duplicate)
    journal(tmp_path, rows)
    result = build_replay(tmp_path)
    assert result["manifest"]["summary"]["issues"]["duplicate_action_submitted"] == 1
    row = transitions(result)[0]
    assert row["after"] is None
    assert "duplicate_action_submission" in row["gaps"]


def test_independent_journal_infers_identity_without_an_external_manifest(tmp_path):
    """A copied native journal remains usable while its absent provenance is explicit."""
    journal(tmp_path, action_rows())
    (tmp_path / "manifest.json").unlink()
    result = build_replay(tmp_path)
    assert result["manifest"]["run_id"] == "run"
    assert transitions(result)[0]["after"] is not None
    assert result["manifest"]["summary"]["issues"]["missing_run_manifest"] == 1


def test_rejected_projection_with_snapshot_is_a_gap_not_a_numeric_prediction(tmp_path):
    """The host's unchanged diagnostic state cannot masquerade as a completed card result."""
    rows = action_rows()
    rows[0]["data"]["prediction"] = {"status": "unsupported", "state": deepcopy(state()["combat"]),
                                      "result": {"ok": False, "errors": [{"error": "unknown_draw_boundary"}]}}
    journal(tmp_path, rows)
    compared = transitions(build_replay(tmp_path))[0]["comparison"]
    assert compared["status"] == "unavailable"
    assert compared["errors"] == [{"error": "unknown_draw_boundary"}]


def test_projection_from_another_public_state_is_not_compared(tmp_path):
    """A recorded projection must agree with its own command and observed state identity."""
    rows = action_rows()
    rows[0]["data"]["prediction"].update(public_state_hash="another-frame", action={"type": "end_turn"})
    journal(tmp_path, rows)
    compared = transitions(build_replay(tmp_path))[0]["comparison"]
    assert compared["reason"] == "prediction_correlation_mismatch"
    assert set(compared["fields"]) == {"public_state_hash", "action"}


def test_version_two_preserves_instance_fields_history_and_artifact_identity(tmp_path):
    """A standalone export retains complete public instances and per-process rule provenance."""
    rows = action_rows()
    card = {"i": 0, "card_id": "STRIKE_IRONCLAD", "energy_cost": 0, "star_cost": 2,
            "costs_x": False, "star_costs_x": False, "card_type": "Attack",
            "dynamic_values": [{"name": "Damage", "base_value": 6, "enchanted_value": 8}],
            "effective_keywords": ["ETHEREAL"], "keyword_state_complete": True,
            "affliction": {"affliction_id": "TAINTED", "amount": 2},
            "enchantment": {"enchantment_id": "SHARP", "amount": 2}}
    for row in rows:
        row["schema_version"] = 2
        observed = row["data"].get("observation")
        if observed:
            observed["state"]["observation_schema_version"] = 2
            observed["state"]["combat"]["hand"] = [deepcopy(card)]
            observed["public_history"] = {"schema_version": 1, "permafrost_first_power_used": True}
    journal(tmp_path, rows)
    (tmp_path / "artifacts-test.json").write_text(json.dumps({
        "simulation_rule_identity": {"backend": "mentor", "rule_snapshot": "actual-rule"},
        "files_sha256": {"host.exe": "actual-hash"}, "config": {"secret": "must-not-export"}}), encoding="utf-8")
    result = build_replay(tmp_path)
    row = transitions(result)[0]
    assert row["before"]["combat"]["hand"] == row["after"]["combat"]["hand"] == [card]
    assert row["before_public_history"]["permafrost_first_power_used"] is True
    assert row["after_public_history"]["permafrost_first_power_used"] is True
    assert row["before_observation_coverage"]["observation_schema_version"] == 2
    artifact = result["manifest"]["provenance"]["artifacts"][0]
    assert artifact["simulation_rule_identity"]["rule_snapshot"] == "actual-rule"
    assert artifact["files_sha256"] == {"host.exe": "actual-hash"}
    assert "must-not-export" not in json.dumps(result)


def test_unready_version_two_completion_cannot_supply_an_after_frame(tmp_path):
    """The new envelope does not weaken settled-successor admission."""
    rows = action_rows()[:2]
    rows.append(event("action_completed", 4, {"observation": observation(state(5, ready=False)),
                      "receipt": rows[1]["data"]["receipt"]}, action_id="a1"))
    for row in rows:
        row["schema_version"] = 2
    journal(tmp_path, rows)
    row = transitions(build_replay(tmp_path))[0]
    assert row["after"] is None
    assert "settled_successor_missing_at_exact_revision" in row["gaps"]

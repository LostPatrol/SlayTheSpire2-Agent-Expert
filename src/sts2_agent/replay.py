"""Export correlated public trajectories without loading hidden game state.

This is an auditable public replay, not deterministic original-engine replay.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from .public_history import observation_coverage


# Combat settlement requires every public admission gate, independently of receipts.
READY_FIELDS = ("can_use_combat_actions", "actions_settled", "snapshot_stable")
# Both statuses prove admission; only later public observations prove settlement.
ACCEPTED_STATUSES = frozenset({"pending", "completed"})
# Old journals contain reviewed public material, never the bridge's raw response.
LEGACY_STREAMS = {
    "execution-requests": "action_submitted", "execution-receipts": "action_receipt",
    "public-events": "observation", "decisions": "decision",
    "evaluator-results": "simulation", "provider-usage": "model_request",
}
# Preserve absent values rather than silently interpreting them as zero.
MISSING = {"missing": True}
# Public Power notation includes a signed amount and optional visible debuff marker.
POWER_PATTERN = re.compile(r"^(\S+)(?:\s+(-?\d+))?(?:\s+(\[debuff\]))?$")
# These quantities are outside the single-card public numeric comparison.
IGNORED_FIELDS = ("hand_identity", "pile_order", "intents", "focus", "pets", "orbs", "readiness")


def _issue(issues: list, code: str, source: dict, **details) -> None:
    """Retain a source location for every malformed or ambiguous journal record."""
    issues.append({"code": code, **source, **details})


def _read_jsonl(path: Path, issues: list) -> list[tuple[dict, dict]]:
    """Read complete object lines and explicitly report corruption and duplicates."""
    records = []
    seen = {}
    if not path.exists():
        return records
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            source = {"file": path.name, "line": number}
            if not raw.endswith(b"\n"):
                _issue(issues, "unterminated_line", source)
            try:
                line = raw.decode("utf-8-sig" if number == 1 else "utf-8")
            except UnicodeDecodeError as error:
                _issue(issues, "invalid_utf8", source, byte_offset=error.start)
                continue
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                _issue(issues, "invalid_json", source, column=error.colno, message=error.msg)
                continue
            if not isinstance(record, dict):
                _issue(issues, "non_object_record", source)
                continue
            key = json.dumps(record, sort_keys=True, ensure_ascii=False)
            if key in seen:
                _issue(issues, "duplicate_record", source, duplicate_of=seen[key])
                continue
            seen[key] = source
            records.append((source, record))
    return records


def _time(value: object) -> float | None:
    """Require an explicit timezone for cross-file event ordering."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _manifest(run_dir: Path, issues: list) -> dict:
    """Read only run identity and public provenance from the optional manifest."""
    path = run_dir / "manifest.json"
    if not path.exists():
        _issue(issues, "missing_run_manifest", {"file": path.name})
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, UnicodeError) as error:
        _issue(issues, "invalid_run_manifest", {"file": path.name}, message=str(error))
        return {}
    _issue(issues, "invalid_run_manifest", {"file": path.name})
    return {}


def _bridge(manifest: dict) -> dict:
    """Extract public process identity without copying configuration or credentials."""
    bridge = manifest.get("bridge", {})
    return {"pid": bridge.get("process_id"),
            "instance_id": bridge.get("external_protocol", {}).get("instance_id")}


def _artifact_provenance(run_dir: Path, issues: list) -> list:
    """Carry each recorded process segment's source identities into the standalone export."""
    artifacts = []
    for path in sorted(run_dir.glob("artifacts-*.json")):
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("artifact manifest is not an object")
        except (OSError, UnicodeError, ValueError) as error:
            _issue(issues, "invalid_artifact_provenance", {"file": path.name}, message=str(error))
            continue
        artifacts.append({"file": path.name, "sha256": sha256(raw).hexdigest(),
                          "timestamp_utc": value.get("timestamp_utc"),
                          "simulation_rule_identity": deepcopy(value.get("simulation_rule_identity")),
                          "files_sha256": deepcopy(value.get("files_sha256", {}))})
    return artifacts


def _events(run_dir: Path, manifest: dict, issues: list) -> tuple[list, str, list, str]:
    """Normalize the independent journal or the older public journal collection."""
    native = run_dir / "transitions.jsonl"
    events = []
    sources = []
    event_ids = {}
    if native.exists():
        streams = [(native, None)]
        mode = "transition_journal"
    else:
        streams = [(run_dir / (name + ".jsonl"), kind) for name, kind in LEGACY_STREAMS.items()]
        mode = "legacy_public_journals"
    run_id = manifest.get("run_id")
    for path, kind in streams:
        if not path.exists():
            continue
        sources.append(path.name)
        for source, row in _read_jsonl(path, issues):
            if kind is None:
                if row.get("schema_version") not in {1, 2} or not isinstance(row.get("data"), dict):
                    _issue(issues, "invalid_transition_envelope", source)
                    continue
                if run_id is None:
                    run_id = row.get("run_id")
                event = {**row, "source": source, "native": True}
                if row.get("event_id"):
                    if row["event_id"] in event_ids:
                        _issue(issues, "duplicate_event_id", source, event_id=row["event_id"],
                               first_source=event_ids[row["event_id"]])
                    event_ids[row["event_id"]] = source
            else:
                if run_id is None:
                    run_id = row.get("run_id", row.get("observation", {}).get("run_id"))
                data = {"receipt": row} if kind == "action_receipt" else row
                if kind == "decision":
                    data = {"decision": row}
                elif kind == "model_request":
                    data = {"provider_event_type": "legacy_usage_metadata", "payload": row}
                event = {"event_type": kind, "timestamp_utc": row.get("timestamp_utc"),
                         "run_id": row.get("run_id", row.get("observation", {}).get("run_id", run_id or run_dir.name)),
                         "bridge_identity": _bridge(manifest), "data": data,
                         "source": source, "native": False}
                for field in ("decision_id", "decision_episode_id", "decision_epoch", "action_id", "model_request_id"):
                    if field in row:
                        event[field] = row[field]
            event["time"] = _time(event.get("timestamp_utc"))
            if event["time"] is None:
                _issue(issues, "invalid_timestamp", source)
            if event.get("run_id") != run_id:
                _issue(issues, "foreign_run_record", source, recorded_run_id=event.get("run_id"))
            events.append(event)
    events.sort(key=lambda event: (event["time"] if event["time"] is not None else float("inf"),
                                   event["source"]["file"], event["source"]["line"]))
    return events, mode, sources, run_id or run_dir.name


def _observation(event: dict) -> dict:
    """Resolve the reviewed observation wrapper without accepting raw bridge state."""
    value = event.get("data", {}).get("observation", {})
    return value if isinstance(value, dict) else {}


def _same_identity(left: dict, right: dict) -> bool:
    """Keep explicit run and bridge identities inside one correlation domain."""
    if left.get("run_id") != right.get("run_id"):
        return False
    first, second = left.get("bridge_identity"), right.get("bridge_identity")
    if left.get("native") or right.get("native"):
        return (isinstance(first, dict) and first == second
                and first.get("pid") is not None and bool(first.get("instance_id")))
    return first == second


def _settlement(event: dict) -> str | None:
    """Check combat gates and retain the weaker evidence available in old logs."""
    state = _observation(event).get("state", {})
    if not state.get("supported", True) or state.get("screen") in {None, "UNKNOWN"}:
        return None
    if state.get("screen") == "COMBAT":
        readiness = state.get("combat", {}).get("action_readiness", {})
        if not all(readiness.get(name) is True for name in READY_FIELDS):
            return None
    elif not state.get("available_actions") and state.get("screen") != "GAME_OVER":
        return None
    if event.get("native"):
        if event["event_type"] != "action_completed" and event["data"].get("settled") is not True:
            return None
        return "controller_settled_and_public_gates"
    return "legacy_combat_public_gates" if state.get("screen") == "COMBAT" else "legacy_public_actionable_frame"


def _hp(entity: dict) -> dict:
    """Normalize public current/max strings and the simulator's HP objects."""
    value = entity.get("hp", MISSING)
    if isinstance(value, str) and "/" in value:
        try:
            current, maximum = value.split("/", 1)
            return {"current": int(current), "max": int(maximum)}
        except ValueError:
            pass
    if isinstance(value, dict):
        return {"current": value.get("current", MISSING), "max": value.get("max", MISSING)}
    return {"current": value, "max": entity.get("max_hp", MISSING)}


def _powers(entity: dict) -> object:
    """Preserve Power IDs, amounts, debuff flags, and unexpected representations."""
    values = entity.get("powers", MISSING)
    if not isinstance(values, list):
        return values
    result = []
    for value in values:
        if isinstance(value, str) and (match := POWER_PATTERN.fullmatch(value)):
            result.append({"id": match[1], "amount": int(match[2]) if match[2] else None,
                           "debuff": bool(match[3])})
        else:
            result.append({"unparsed": value})
    return sorted(result, key=lambda value: json.dumps(value, sort_keys=True))


def compare_prediction(prediction: dict, after: dict) -> dict:
    """Compare only deterministic public resources and visible Power quantities."""
    predicted = prediction.get("state", prediction)
    predicted = predicted.get("combat", predicted)
    expected = after.get("state", after)
    expected = expected.get("combat", expected)
    differences = []
    compared = []

    def check(field: str, actual: object, wanted: object) -> None:
        """Keep both values whenever a checked public quantity differs."""
        compared.append(field)
        if actual != wanted:
            differences.append({"field": field, "expected": deepcopy(wanted), "actual": deepcopy(actual)})

    def living(combat: dict) -> list:
        """Keep survivor order without treating frame-local indices as identities."""
        return [enemy for enemy in combat.get("enemies", [])
                if enemy.get("alive") is not False
                and not (type(_hp(enemy)["current"]) in (int, float) and _hp(enemy)["current"] <= 0)]

    player, wanted_player = predicted.get("player", {}), expected.get("player", {})
    for field in ("current", "max"):
        check("player.hp." + field, _hp(player)[field], _hp(wanted_player)[field])
    for field in ("block", "energy", "stars"):
        check("player." + field, player.get(field, MISSING), wanted_player.get(field, MISSING))
    check("player.powers", _powers(player), _powers(wanted_player))
    enemies, wanted_enemies = living(predicted), living(expected)
    check("enemies.count", len(enemies), len(wanted_enemies))
    for index, (enemy, wanted) in enumerate(zip(enemies, wanted_enemies)):
        path = f"enemies[{index}]"
        check(path + ".enemy_id", enemy.get("enemy_id", MISSING), wanted.get("enemy_id", MISSING))
        for field in ("current", "max"):
            check(path + ".hp." + field, _hp(enemy)[field], _hp(wanted)[field])
        check(path + ".block", enemy.get("block", MISSING), wanted.get("block", MISSING))
        check(path + ".powers", _powers(enemy), _powers(wanted))
    return {"status": "match" if not differences else "mismatch", "matches": not differences, "differences": differences,
            "compared_fields": compared, "ignored_fields": list(IGNORED_FIELDS)}


def _category(before: dict, after: dict | None) -> str:
    """Name turn changes and UI boundaries instead of silently excluding them."""
    if after is None:
        return "unresolved"
    first, second = before.get("screen"), after.get("screen")
    if first != second:
        return f"screen:{first}->{second}"
    if first == "COMBAT":
        return "combat_step" if before.get("turn") == after.get("turn") else "combat_turn"
    return "same_screen:" + str(first)


def _comparison(prediction: dict | None, action: dict, after: dict | None, before_obs: dict) -> dict:
    """Expose unsupported predictions and screen boundaries as coverage gaps."""
    if not isinstance(prediction, dict) or not prediction:
        return {"status": "unavailable", "reason": "prediction_not_recorded"}
    if (prediction.get("status") not in {None, "predicted"} or prediction.get("ok") is False
            or not isinstance(prediction.get("state"), dict)):
        return {"status": "unavailable", "reason": "prediction_not_supported",
                "errors": prediction.get("errors", (prediction.get("result") or {}).get("errors", [])),
                "diagnostics": prediction.get("diagnostics", []), "support": prediction.get("support")}
    correlation_errors = []
    for field in ("run_id", "observation_id", "decision_episode_id"):
        if prediction.get(field) is not None and prediction[field] != before_obs.get(field):
            correlation_errors.append(field)
    if prediction.get("action") is not None and prediction["action"] != action:
        correlation_errors.append("action")
    if prediction.get("public_state_hash") is not None:
        encoded = json.dumps(before_obs.get("state"), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if prediction["public_state_hash"] != sha256(encoded.encode("utf-8")).hexdigest():
            correlation_errors.append("public_state_hash")
    if correlation_errors:
        return {"status": "unavailable", "reason": "prediction_correlation_mismatch", "fields": correlation_errors}
    if after is None:
        return {"status": "unavailable", "reason": "settled_successor_not_recorded"}
    if action.get("type") != "play_card":
        return {"status": "boundary", "reason": "single_card_comparison_only"}
    if after.get("screen") != "COMBAT":
        return {"status": "boundary", "reason": "after_screen_" + str(after.get("screen"))}
    return compare_prediction(prediction, after)


def _unique(events: list, kind: str, field: str, issues: list) -> dict:
    """Index identities while reporting repeated or conflicting logical records."""
    grouped = defaultdict(list)
    for event in events:
        if event["event_type"] == kind:
            if event.get(field):
                grouped[event[field]].append(event)
            else:
                _issue(issues, "missing_" + field, event["source"], event_type=kind)
    for identity, group in grouped.items():
        if len(group) > 1:
            _issue(issues, "duplicate_" + kind, group[1]["source"], identity=identity,
                   first_source=group[0]["source"], occurrences=len(group))
    return grouped


def _transition(request: dict, submissions: list, receipts: list, observations: list,
                decisions: dict, simulations: dict, run_id: str) -> dict:
    """Bind one admitted action to its own exact-revision public successor."""
    data = request["data"]
    before_obs = _observation(request)
    before = before_obs.get("state", {})
    revision = before.get("action_revision")
    action_id = request.get("action_id")
    gaps = []
    if request.get("run_id") != run_id or before_obs.get("run_id") != run_id:
        gaps.append("before_run_identity_mismatch")
    if request.get("time") is None:
        gaps.append("submission_timestamp_missing")
    if type(revision) is not int:
        gaps.append("before_action_revision_missing")
    if before.get("screen") == "COMBAT" and not all(
            before.get("combat", {}).get("action_readiness", {}).get(name) is True for name in READY_FIELDS):
        gaps.append("before_combat_not_settled")
    duplicates = [event for event in submissions if event.get("action_id") == action_id]
    if len(duplicates) > 1:
        gaps.append("duplicate_action_submission")
    accepted = []
    for event in receipts:
        receipt = event["data"].get("receipt", {})
        if event.get("action_id") != action_id or receipt.get("action_id") != action_id:
            continue
        if not _same_identity(request, event):
            gaps.append("receipt_session_identity_mismatch")
            continue
        if receipt.get("status") not in ACCEPTED_STATUSES:
            continue
        if event.get("time") is None or request.get("time") is None or event["time"] < request["time"]:
            gaps.append("receipt_timestamp_invalid")
            continue
        if type(revision) is not int or receipt.get("public_state", {}).get("action_revision") != revision + 1:
            gaps.append("receipt_revision_mismatch")
            continue
        accepted.append(event)
    if not accepted:
        gaps.append("accepted_receipt_missing")
    next_times = [event["time"] for event in submissions
                  if event.get("action_id") != action_id and _same_identity(request, event)
                  and event.get("time") is not None and request.get("time") is not None
                  and event["time"] > request["time"]]
    upper = min(next_times) if next_times else float("inf")
    candidates = []
    if not gaps and accepted:
        for event in observations:
            observation = _observation(event)
            state = observation.get("state", {})
            if (event.get("time") is None or event["time"] < request["time"] or event["time"] >= upper
                    or not _same_identity(request, event) or observation.get("run_id") != run_id
                    or state.get("action_revision") != revision + 1):
                continue
            if event.get("action_id") and event.get("action_id") != action_id:
                continue
            if _settlement(event):
                candidates.append(event)
    completed = [event for event in candidates if event["event_type"] == "action_completed"
                 and event.get("action_id") == action_id]
    # A completion binds settlement to the exact native command, beyond revision matching.
    successor = completed[0] if request.get("native") and completed else (candidates[0] if candidates else None)
    if accepted and not successor:
        gaps.append("settled_successor_missing_at_exact_revision")
    after = deepcopy(_observation(successor).get("state")) if successor else None
    decision_id = request.get("decision_id")
    decision_group = [event for event in decisions.get(decision_id, []) if _same_identity(request, event)]
    simulation_group = [event for event in simulations.get(decision_id, []) if _same_identity(request, event)]
    prediction = data.get("prediction")
    if isinstance(prediction, dict) and prediction.get("status") == "deferred":
        # Background audits finish after submission and must bind to this exact native command.
        audits = [event for event in simulation_group if event.get("action_id") == action_id
                  and event["data"].get("audit_mode") == "background"
                  and isinstance(event["data"].get("prediction"), dict)]
        if audits:
            prediction = audits[-1]["data"]["prediction"]
    elif prediction is None and simulation_group:
        prediction = simulation_group[-1]["data"].get("prediction")
    receipt_rows = [{"source": event["source"], "receipt": event["data"].get("receipt")}
                    for event in receipts if event.get("action_id") == action_id]
    evidence = _settlement(successor) if successor else None
    if request.get("native") and successor and not completed:
        gaps.append("action_completed_not_recorded")
    if evidence == "legacy_public_actionable_frame":
        gaps.append("legacy_noncombat_token_stability_not_recorded")
    return {"record_type": "transition", "timestamp_utc": request.get("timestamp_utc"),
            "run_id": run_id, "decision_id": decision_id,
            "decision_episode_id": request.get("decision_episode_id", before_obs.get("decision_episode_id")),
            "decision_epoch": request.get("decision_epoch"), "action_id": action_id,
            "action": deepcopy(data.get("action", {})), "before": deepcopy(before), "after": after,
            "before_public_history": deepcopy(before_obs.get("public_history")),
            "after_public_history": deepcopy(_observation(successor).get("public_history")) if successor else None,
            "before_observation_coverage": deepcopy(data.get("observation_coverage", observation_coverage(before))),
            "after_observation_coverage": (deepcopy(successor["data"].get("observation_coverage",
                                            observation_coverage(after))) if successor else None),
            "bridge_identity": deepcopy(request.get("bridge_identity")),
            "identity_evidence": "per_event" if request.get("native") else "run_directory_and_manifest_only",
            "category": _category(before, after), "accepted": bool(accepted),
            "settlement_evidence": evidence, "gaps": list(dict.fromkeys(gaps)),
            "submission_source": request["source"], "after_source": successor["source"] if successor else None,
            "receipts": receipt_rows, "decision_recorded": bool(decision_group),
            "prediction": deepcopy(prediction),
            "comparison": _comparison(prediction, data.get("action", {}), after, before_obs)}


def build_replay(run_dir: Path) -> dict:
    """Build a complete public timeline and an honest correlation/coverage manifest."""
    run_dir = Path(run_dir)
    issues = []
    manifest = _manifest(run_dir, issues)
    artifacts = _artifact_provenance(run_dir, issues)
    events, mode, sources, run_id = _events(run_dir, manifest, issues)
    decisions = _unique(events, "decision", "decision_id", issues)
    submissions_by_id = _unique(events, "action_submitted", "action_id", issues)
    submissions = [group[0] for group in submissions_by_id.values()]
    all_submissions = [event for group in submissions_by_id.values() for event in group]
    receipts = [event for event in events if event["event_type"] == "action_receipt"]
    receipt_keys = {}
    for event in receipts:
        receipt = {key: value for key, value in event["data"].get("receipt", {}).items() if key != "timestamp_utc"}
        key = json.dumps([event.get("run_id"), event.get("bridge_identity"), receipt], sort_keys=True)
        if key in receipt_keys:
            _issue(issues, "duplicate_action_receipt", event["source"], first_source=receipt_keys[key],
                   action_id=event.get("action_id"))
        receipt_keys[key] = event["source"]
    # A recovered controller may record completion even when its earlier receipt line is absent.
    for event in events:
        if event["event_type"] == "action_completed" and isinstance(event["data"].get("receipt"), dict):
            receipts.append({**event, "event_type": "action_receipt",
                             "data": {"receipt": event["data"]["receipt"]}})
    observations = [event for event in events if event["event_type"] in {"observation", "action_completed"}
                    and _observation(event)]
    simulations = defaultdict(list)
    requests = defaultdict(list)
    inputs = defaultdict(list)
    for event in events:
        if event["event_type"] == "simulation":
            simulations[event.get("decision_id")].append(event)
        elif event["event_type"] == "model_request":
            requests[event.get("decision_id")].append(event)
        elif event["event_type"] == "decision_input":
            inputs[event.get("decision_id")].append(event)
    records = []
    for event in observations:
        records.append({"record_type": "observation", "timestamp_utc": event.get("timestamp_utc"),
                        "run_id": event.get("run_id"), "bridge_identity": event.get("bridge_identity"),
                        "decision_episode_id": event.get("decision_episode_id", _observation(event).get("decision_episode_id")),
                        "action_id": event.get("action_id"), "source": event["source"],
                        "observation_coverage": deepcopy(event["data"].get("observation_coverage",
                            observation_coverage(_observation(event).get("state", {})))),
                        "settlement_evidence": _settlement(event), "observation": deepcopy(_observation(event))})
    decision_ids = set(decisions) | set(requests) | set(simulations) | set(inputs)
    for decision_id in sorted(decision_ids, key=lambda value: str(value)):
        group = decisions.get(decision_id, [])
        related = group or requests.get(decision_id) or simulations.get(decision_id) or inputs[decision_id]
        first = related[0]
        records.append({"record_type": "decision", "timestamp_utc": first.get("timestamp_utc"),
                        "run_id": first.get("run_id"), "decision_id": decision_id,
                        "decision_episode_id": first.get("decision_episode_id"),
                        "decision_epoch": first.get("decision_epoch"),
                        "decisions": [{"source": event["source"], **deepcopy(event["data"])} for event in group],
                        "inputs": [{"source": event["source"], **deepcopy(event["data"])}
                                   for event in inputs.get(decision_id, [])],
                        "model_requests": [{"source": event["source"], "model_request_id": event.get("model_request_id"),
                                            "timestamp_utc": event.get("timestamp_utc"), **deepcopy(event["data"])}
                                           for event in requests.get(decision_id, [])],
                        "simulations": [{"source": event["source"], **deepcopy(event["data"])}
                                        for event in simulations.get(decision_id, [])],
                        "gaps": ([] if group else ["decision_not_recorded"])
                                + ([] if inputs.get(decision_id) else ["decision_input_not_recorded"])
                                + ([] if requests.get(decision_id) else ["model_request_not_recorded"])
                                + (["legacy_model_prompt_and_output_not_recorded"]
                                   if mode == "legacy_public_journals" else []),
                        "action_ids": [event.get("action_id") for event in submissions
                                       if event.get("decision_id") == decision_id]})
    grouped_types = {"observation", "action_completed", "decision", "decision_input", "simulation", "model_request",
                     "action_submitted", "action_receipt"}
    for event in events:
        orphan = (event["event_type"] in {"action_receipt", "action_completed"}
                  and event.get("action_id") not in submissions_by_id)
        if orphan:
            _issue(issues, "orphan_" + event["event_type"], event["source"], action_id=event.get("action_id"))
        if event["event_type"] not in grouped_types or orphan:
            records.append({"record_type": "event", "timestamp_utc": event.get("timestamp_utc"),
                            "event_type": event["event_type"], "run_id": event.get("run_id"),
                            "decision_id": event.get("decision_id"), "action_id": event.get("action_id"),
                            "source": event["source"], "data": deepcopy(event["data"])})
    transitions = [_transition(event, all_submissions, receipts, observations, decisions, simulations, run_id)
                   for event in submissions]
    records.extend(transitions)
    records.sort(key=lambda record: (_time(record.get("timestamp_utc")) or float("inf"), record["record_type"]))
    summary = {"actions": len(transitions), "accepted_actions": sum(row["accepted"] for row in transitions),
               "with_public_successor": sum(row["after"] is not None for row in transitions),
               "categories": dict(Counter(row["category"] for row in transitions)),
               "comparisons": dict(Counter(row["comparison"]["status"] for row in transitions)),
               "comparison_reasons": dict(Counter(row["comparison"]["reason"] for row in transitions
                                                  if "reason" in row["comparison"])),
               "gaps": dict(Counter(gap for row in transitions for gap in row["gaps"])),
               "observations": len(observations), "decisions": len(decision_ids),
               "observation_coverage": dict(Counter(row["observation_coverage"]["status"] for row in records
                                                     if row["record_type"] == "observation")),
               "decision_gaps": dict(Counter(gap for row in records if row["record_type"] == "decision"
                                              for gap in row["gaps"])),
               "issues": dict(Counter(issue["code"] for issue in issues))}
    exported_manifest = {"schema_version": 2, "run_id": run_id,
                         "created_utc": datetime.now(timezone.utc).isoformat(), "source_mode": mode,
                         "source_files": sources, "summary": summary, "issues": issues,
                         "provenance": {"bridge_identity": _bridge(manifest),
                                        "artifacts": artifacts,
                                        "game_version": manifest.get("bridge", {}).get("game_version"),
                                        "assistance_mode": manifest.get("assistance_mode"),
                                        "policy": deepcopy(manifest.get("policy"))},
                         "validation": {"status": "issues_found" if issues else "valid",
                                        "source_integrity": "incomplete" if any(issue["code"] in {
                                            "invalid_json", "invalid_utf8", "unterminated_line", "non_object_record",
                                            "invalid_transition_envelope"} for issue in issues) else "readable"},
                         "scope": "Correlated public replay only; no original-engine deterministic replay claim.",
                         "limitations": ["No hidden logs, real seed, hidden pile order, or future draw identity are read.",
                                         "Old noncombat frames lack recorded repeated-token settlement evidence.",
                                         "Numeric comparison covers one-card public resources and Powers only.",
                                         "A transition journal, when present, is authoritative for its recorded interval."]}
    return {"manifest": exported_manifest, "records": records}


def export_replay(run_dir: Path, output_dir: Path) -> dict:
    """Write the public timeline and its separate validation/coverage manifest."""
    result = build_replay(run_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timeline = output_dir / "public-replay.jsonl"
    manifest = output_dir / "public-replay.manifest.json"
    with timeline.open("w", encoding="utf-8") as stream:
        for record in result["records"]:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    manifest.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"timeline": str(timeline.resolve()), "manifest": str(manifest.resolve()),
            "summary": result["manifest"]["summary"]}

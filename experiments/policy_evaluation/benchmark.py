"""Freeze public experiment identities and evaluate policy contracts without game execution.

Historical corpus labels measure legality, not strategic optimality or win probability.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

# Use only the project's installed controller and dependencies.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from sts2_agent.config import load_config
from sts2_agent.prompt_context import serialize, stable_prefix
from sts2_agent.prompt_projection import compact_public, expand_public, serialize_projection, PROJECTION_GUIDE
from sts2_agent.public import ActionError, INDEX_SOURCES, validate_action, observation as public_observation
from sts2_agent.provider import DECISION_PROMPT, Provider
from sts2_agent.model_tools import TOOL_SPECS

# Usage rates are explicit input assumptions, not reconstructed historical bills.
OFF_PEAK_CNY = {"prompt_cache_hit_tokens": .02, "prompt_cache_miss_tokens": 1., "completion_tokens": 4.}
# Keep identity collection bounded to decision-affecting local sources and deployed artifacts.
IDENTITY_FILES = (
    "configs/agent.schema.json", "configs/speedx-agent.json", "knowledge/current.json",
    "knowledge/strategies/manifest.json", "knowledge/strategies/sections.json", "knowledge/strategies/relations.json",
    "patches/bridge-main-public-v1.patch", "local/simulation/mentor/source-provenance.json",
    "local/simulation/mentor/mentor-simulation.exe", "experiments/mentor_simulation/host/Cargo.lock",
)


def read_json(path: Path, default=None):
    """Read optional structured public data while preserving absence explicitly."""
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def rows(path: Path):
    """Stream immutable evidence, retaining exact one-based source line numbers."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                yield number, json.loads(line)


def file_identity(path: Path) -> dict:
    """Hash artifacts without loading credentials or game save files."""
    if not path.is_file():
        return {"path": str(path), "status": "missing", "sha256": None}
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "status": "present", "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def write_new(path: Path, value: dict):
    """Keep every experimental result immutable instead of replacing previous evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def safe_config(config: dict) -> dict:
    """Remove credential locations and values while retaining decision parameters."""
    copied = deepcopy(config)
    provider = copied.get("provider", {})
    for key in list(provider):
        if "key" in key.lower() or "secret" in key.lower() or "token" in key.lower() and key != "max_tokens":
            provider.pop(key)
    return copied


def freeze(config: dict, run_dir: Path | None = None, config_path: Path | None = None) -> dict:
    """Separate current disk identities from historical process and model observations."""
    machine = read_json(ROOT / "configs/machine.json", {})
    game = Path(machine.get("game_root", "missing-game-root"))
    paths = [ROOT / name for name in IDENTITY_FILES]
    # Every top-level module can affect policy, execution, recovery, or evidence semantics.
    paths.extend(sorted((ROOT / "src/sts2_agent").glob("*.py")))
    if config_path is not None:
        paths.append(config_path.resolve())
    paths += [game / "mods/STS2AIAgent.dll", game / "mods/STS2AIAgent.pck", game / "data_sts2_windows_x86_64/sts2.dll"]
    steam_manifest = game.parent.parent / "appmanifest_2868840.acf"
    paths.append(steam_manifest)
    build_match = re.search(r'"buildid"\s+"(\d+)"', steam_manifest.read_text(encoding="utf-8")) if steam_manifest.is_file() else None
    for mod in ("SpeedX", "ModConfig"):
        for extension in ("*.dll", "*.pck", "*.json"):
            paths.extend(sorted((game / "mods" / mod).glob(extension)))
    if os.environ.get("APPDATA"):
        paths.append(Path(os.environ["APPDATA"]) / "SlayTheSpire2/ModConfig/sts2.piyixiajiuhenfen.speedx.json")
    source_files = sorted((ROOT / "experiments/mentor_simulation/host/src").glob("*.rs"))
    paths += source_files
    snapshot = read_json(ROOT / "knowledge/current.json", {})
    snapshot_id = snapshot.get("snapshot_id")
    if snapshot_id:
        paths.extend(ROOT / "knowledge/snapshots" / snapshot_id / name for name in ("sources.lock.json", "entities.jsonl"))
    paths = list(dict.fromkeys(path.resolve() for path in paths))
    result = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
              "kind": "disk_candidate_not_live_process_attestation", "config": safe_config(config),
              "files": [file_identity(path) for path in paths],
              "prompt_sha256": sha256((stable_prefix(DECISION_PROMPT) + "\n" + PROJECTION_GUIDE).encode()).hexdigest(),
              "tools_sha256": sha256(serialize(TOOL_SPECS).encode()).hexdigest(),
              "requested_model": config["provider"]["model"], "actual_model": None,
              "system_fingerprint": None, "unlock_pool": {"status": "unknown", "reason": "not_exported_as_complete_public_pool"},
              "declared_client_id": machine.get("client_id"), "live_profile_verified": False,
              "installed_steam_build_id": build_match.group(1) if build_match else None,
              "live_game_operations": False, "historical_run": None}
    if run_dir:
        manifest = read_json(run_dir / "manifest.json", {})
        identities = Counter((row.get("actual_model"), row.get("system_fingerprint")) for _, row in rows(run_dir / "provider-usage.jsonl"))
        segments = [file_identity(path) for path in sorted(run_dir.glob("artifacts-*.json"))]
        unlocks = []
        for line, event in rows(run_dir / "public-events.jsonl"):
            facts = event.get("facts", {})
            if facts.get("screen") == "UNLOCK" or "unlock" in facts:
                unlocks.append({"line": line, "facts": facts})
        result["historical_run"] = {"run_id": manifest.get("run_id"), "manifest": file_identity(run_dir / "manifest.json"),
            "game_build_reference": manifest.get("current_knowledge", {}).get("target"),
            "bridge": manifest.get("bridge"), "process_artifacts": segments,
            "model_observations": [{"model": key[0], "fingerprint": key[1], "requests": value} for key, value in identities.items()],
            "observed_unlock_events": unlocks, "full_unlock_pool_known": False,
            "configuration_frozen_proven": False}
    return result


def verify_freeze(manifest: dict) -> dict:
    """Report candidate drift for experimental grouping without blocking ordinary gameplay."""
    changes = []
    for expected in manifest.get("files", []):
        current = file_identity(Path(expected["path"]))
        if current["sha256"] != expected.get("sha256") or current["status"] != expected.get("status"):
            changes.append({"path": expected["path"], "expected": expected, "current": current})
    return {"kind": "disk_identity_drift_review", "unchanged": not changes, "changes": changes,
            "live_process_verified": False, "unlock_pool_verified": False,
            "meaning": "Unchanged files do not prove a running process loaded these files or that hidden unlock conditions are fixed."}


def distribution(values: list) -> dict:
    """Report nearest-rank P95 and known-count totals without imputing missing values."""
    values = sorted(value for value in values if isinstance(value, (int, float)))
    if not values:
        return {"known": 0}
    middle = len(values) // 2
    return {"known": len(values), "sum": sum(values), "median": (values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2),
            "p95": values[min(len(values) - 1, (len(values) * 95 + 99) // 100 - 1)], "max": values[-1]}


def summarize(run_dir: Path) -> dict:
    """Classify historical outcomes independently of assistance and version freezing."""
    manifest = read_json(run_dir / "manifest.json", {})
    terminal = read_json(run_dir / "result.json", {})
    usage = [row for _, row in rows(run_dir / "provider-usage.jsonl")]
    totals = Counter()
    for row in usage:
        totals.update({key: value for key, value in (row.get("usage") or {}).items() if isinstance(value, (int, float))})
    native_end = terminal.get("status") == "native_game_over"
    if native_end:
        classification = ("native_victory_requires_terminal_definition" if terminal.get("victory") is True
                          else "native_defeat" if terminal.get("victory") is False else "native_terminal_outcome_unknown")
    elif terminal.get("status") in {"technical_interruption", "budget_stop"}:
        classification = terminal["status"]
    else:
        classification = "other_stop" if terminal else "unfinished"
    simulation = defaultdict(list)
    stages = defaultdict(list)
    observation_work = defaultdict(Counter)
    interruptions = Counter()
    for _, event in rows(run_dir / "stage-timings.jsonl"):
        stage = event.get("stage", "unknown")
        if isinstance(event.get("elapsed_ms"), (int, float)):
            stages[stage].append(event["elapsed_ms"])
        for key in ("polls", "not_ready_polls", "token_changes"):
            value = event.get("details", {}).get(key)
            if isinstance(value, (int, float)):
                observation_work[stage][key] += value
    for _, event in rows(run_dir / "transitions.jsonl"):
        kind, data = event.get("event_type"), event.get("data", {})
        if kind == "simulation":
            for key in ("result", "prediction"):
                value = data.get(key)
                if isinstance(value, dict) and isinstance(value.get("elapsed_ms"), (int, float)):
                    simulation[key].append(value["elapsed_ms"])
        if kind in {"error", "session_error", "budget_exhausted"}:
            interruptions[kind] += 1
    return {"run_id": manifest.get("run_id"), "outcome_class": classification,
            "native_victory_flag": terminal.get("victory"), "assistance_mode": terminal.get("assistance_mode", manifest.get("assistance_mode", "unknown")),
            "terminal_evidence": terminal.get("terminal_evidence"), "error": terminal.get("error"),
            "frozen_experiment": False, "freeze_reason": "historical_manifest_does_not_prove_unchanged_build_and_unlock_pool",
            "requests": len(usage), "unknown_usage": sum(not isinstance((row.get("usage") or {}).get("prompt_tokens"), (int, float)) for row in usage),
            "usage": dict(totals), "cache_hit_rate": totals["prompt_cache_hit_tokens"] / totals["prompt_tokens"] if totals["prompt_tokens"] else None,
            "cost_assumption": {"currency": "CNY", "rates_per_million": OFF_PEAK_CNY, "kind": "uniform_current_off_peak_repricing_not_bill"},
            "known_token_cost": sum(totals[key] * price / 1e6 for key, price in OFF_PEAK_CNY.items()),
            "timing": {field: distribution([row.get(field) for row in usage]) for field in ("duration_ms", "first_delta_elapsed_ms", "generation_ms")},
            "simulation_elapsed_ms": {key: distribution(value) for key, value in simulation.items()},
            "stage_timings_ms": {key: distribution(value) for key, value in stages.items()},
            "observation_poll_work": {key: dict(value) for key, value in observation_work.items()},
            "stage_timing_limit": "Inclusive stage durations and background work overlap; never sum them as total wall time.",
            "raw_journal_error_events": dict(interruptions),
            "error_event_limit": "Repeated paused-state errors are separate events, not independent technical interruptions.",
            "wall_seconds": terminal.get("wall_seconds"), "actions_completed": terminal.get("actions_completed"),
            "phase_timing_limit": "API intervals overlap; no complete game-wall-time decomposition is inferred"}


def expanded_turns(messages: list) -> list:
    """Expand every original projection before rebuilding a shortened protocol history."""
    known, groups, current = {}, [], None
    for message in messages[1:]:
        projection = None
        if message.get("role") == "user":
            try:
                value = json.loads(message.get("content") or "")
                if isinstance(value, dict) and "encoding" in value:
                    projection = value
            except ValueError:
                pass
        if projection is not None:
            current = {"payload": expand_public(projection, known), "reference": projection.get("current_action_reference"),
                       "messages": [deepcopy(message)]}
            known.update(projection.get("definitions", {}))
            groups.append(current)
        elif current is not None:
            current["messages"].append(deepcopy(message))
    return groups


def legal_actions(observation: dict, policy: dict | None = None) -> list:
    """Enumerate public primitive actions and validate every handle through production rules."""
    candidates = []
    state = observation.get("state", {})
    for name in observation.get("available_actions", []):
        if name == "play_card":
            choices, field = state.get("combat", {}).get("hand", []), "card_index"
        elif name in INDEX_SOURCES:
            section, collection = INDEX_SOURCES[name]
            choices, field = state.get(section, {}).get(collection, []), "option_index"
        elif name == "choose_bundle":
            choices, field = state.get("bundles", []), "option_index"
        elif name.startswith("crystal_"):
            continue
        else:
            candidates.append({"type": name})
            continue
        for choice in choices:
            action = {"type": name, field: choice.get("i")}
            targets = choice.get("targets", choice.get("valid_target_indices", []))
            candidates.extend([{**action, "target_index": target} for target in targets] if targets else [action])
    valid = []
    for action in candidates:
        try:
            validate_action(observation, action, policy)
        except (ActionError, KeyError, TypeError):
            continue
        valid.append(action)
    return valid


def collect(run_dir: Path, count: int) -> dict:
    """Select diverse real roots with exact legality labels, without claiming best strategy."""
    manifest = read_json(run_dir / "manifest.json", {})
    buckets, seen = defaultdict(list), set()
    for line, record in rows(run_dir / "transitions.jsonl"):
        payload = record.get("data", {}).get("payload", {})
        messages = payload.get("messages")
        decision = payload.get("decision_id")
        if not messages or decision in seen:
            continue
        groups = expanded_turns(messages)
        if not groups:
            continue
        latest = groups[-1]["payload"]
        obs = latest.get("observation", {})
        accepted = legal_actions(obs, manifest.get("policy"))
        if not accepted:
            continue
        seen.add(decision)
        state = obs.get("state", {})
        run = state.get("run", {})
        case = {"id": str(decision), "kind": "public_action_legality", "source": {"run_id": manifest.get("run_id"),
            "file": "transitions.jsonl", "line": line, "model_request_id": payload.get("model_request_id")},
            "tags": [state.get("screen", "UNKNOWN"), "current_handles", "target_namespace"],
            "observation": obs, "memory": latest.get("memory", {}), "instructions": latest.get("instructions", []),
            "accepted_actions": accepted, "label_scope": "legality_only_not_tactical_optimality",
            "policy": manifest.get("policy", {})}
        buckets[(state.get("screen", "UNKNOWN"), str(run.get("act_id")), str(run.get("floor")))].append(case)
    # Balance screen categories first, then floors, rather than taking a lexical prefix.
    cases = []
    by_screen = defaultdict(list)
    for key in sorted(buckets):
        by_screen[key[0]].append(key)
    cursors = Counter()
    while len(cases) < count and any(buckets.values()):
        for screen in sorted(by_screen):
            keys = by_screen[screen]
            for _ in range(len(keys)):
                key = keys[cursors[screen] % len(keys)]
                cursors[screen] += 1
                if buckets[key] and len(cases) < count:
                    cases.append(buckets[key].pop(0))
                    break
    return {"schema_version": 1, "source": file_identity(run_dir / "transitions.jsonl"), "requested": count,
            "collected": len(cases), "labels": "production_public_contract_not_model_opinion",
            "strategy_labels": 0, "limitations": ["Legality cannot distinguish a legal lethal move from a safe move.",
                "Historical developer-run prompts are not a frozen policy experiment.", "Crystal coordinates are excluded from this first corpus."], "cases": cases}


def witnessed_case(evidence_path: Path) -> dict:
    """Import the reviewed strict death-versus-block witness without inventing native proof."""
    evidence = read_json(evidence_path)
    by_name = {row["case"]: row for row in evidence["results"]}
    attack = by_name["lethal_vs_defend_strict_bludgeon"]
    defend = by_name["lethal_vs_defend_strict_defend"]
    if (not attack["response"].get("ok") or not defend["response"].get("ok")
            or attack["request"]["public_state"] != defend["request"]["public_state"]
            or attack["response"]["state"]["player"]["hp"]["current"] != 0
            or defend["response"]["state"]["player"]["hp"]["current"] != 1
            or attack["response"]["turn_stages"]["boundary"]["stage"] != "enemy_actions_end"):
        raise ValueError("Reviewed deterministic death/block witness does not match its declared contract")
    obs = public_observation(defend["request"]["public_state"], "offline-witness", "defend-lethal")
    action = defend["request"]["actions"][0]
    validate_action(obs, action)
    return {"id": "strict-defend-versus-lethal", "kind": "deterministic_tactical_witness", "source": file_identity(evidence_path),
            "tags": ["COMBAT", "survival", "strict_enemy_actions_end"], "observation": obs, "memory": {}, "instructions": [],
            "accepted_actions": [action], "label_scope": "strict_simulator_survival_witness_not_native_full_turn_verified",
            "evidence": {"attack_hp": 0, "defend_hp": 1, "host_sha256": evidence.get("executable_sha256")},
            "policy": {"character": "IRONCLAD", "ascension": 0}}


def rebuild(messages: list, turns: int, characters: int, *, groups: list | None = None, cache: dict | None = None) -> tuple[list, int]:
    """Keep recent complete turns and rebuild all references with lossless current facts."""
    groups = expanded_turns(messages) if groups is None else groups
    if not groups:
        return deepcopy(messages), 0
    cache = {} if cache is None else cache
    start = max(0, len(groups) - turns - 1)
    while True:
        if start not in cache:
            rebuilt, known, history_chars = [deepcopy(messages[0])], {}, 0
            for index, group in enumerate(groups[start:], start):
                projection = compact_public(group["payload"], known)
                if group["reference"] is not None:
                    projection["current_action_reference"] = group["reference"]
                assert expand_public(projection, known) == group["payload"]
                known.update(projection["definitions"])
                turn = [{**group["messages"][0], "content": serialize_projection(projection)}, *deepcopy(group["messages"][1:])]
                rebuilt.extend(turn)
                if index < len(groups) - 1:
                    history_chars += sum(Provider._message_characters(message) for message in turn)
            cache[start] = (rebuilt, history_chars)
        rebuilt, history_chars = cache[start]
        if history_chars <= characters or start == len(groups) - 1:
            return rebuilt, start
        # Re-encoding can enlarge the first retained turn; enforce the rebuilt size.
        start += 1


def common_prefix(left: str, right: str) -> int:
    """Measure exact adjacent reuse only, never label this quantity API cache hits."""
    limit = min(len(left), len(right))
    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if left[:middle] == right[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def window_audit(run_dir: Path) -> dict:
    """Explore shorter histories while preserving original scene-reset boundaries."""
    variants = [(8, 160000), (16, 320000), (32, 640000)]
    stats = {f"{turns}/{chars}": {"requests": 0, "chars": 0, "adjacent_prefix_chars": 0, "dropped_prior_turns": 0} for turns, chars in variants}
    previous = {key: "" for key in stats}
    for _, record in rows(run_dir / "transitions.jsonl"):
        messages = record.get("data", {}).get("payload", {}).get("messages")
        if not messages:
            continue
        groups, cache = expanded_turns(messages), {}
        for turns, chars in variants:
            key = f"{turns}/{chars}"
            rebuilt, dropped = rebuild(messages, turns, chars, groups=groups, cache=cache)
            text = serialize(rebuilt)
            reuse = common_prefix(previous[key], text)
            previous[key] = text
            stats[key]["requests"] += 1
            stats[key]["chars"] += len(text)
            stats[key]["adjacent_prefix_chars"] += reuse
            stats[key]["dropped_prior_turns"] += dropped
        completed = next(iter(stats.values()))["requests"]
        if completed % 50 == 0:
            print(json.dumps({"operation": "window", "completed_requests": completed}), flush=True)
    for value in stats.values():
        value["input_cost_character_proxy_1_to_50"] = value["adjacent_prefix_chars"] / 50 + value["chars"] - value["adjacent_prefix_chars"]
    return {"kind": "offline_rolling_suffix_history_ablation", "variants": stats,
            "current_facts_equal": True, "scene_resets": "original_requests_already_bound_each_scene",
            "character_budget_basis": "actual_rebuilt_retained_turns_excluding_complete_current_turn",
            "limitations": ["Retained conversation changes policy information even though current facts are equal.",
                "Character proxy is not token usage, service cache measurement, or a quality result.",
                "This explores rolling retained suffixes, not production's full-history reset when its budget is exceeded.",
                "Only adjacent request prefixes are compared; service cache persistence is not simulated."]}


def live(corpus: dict, config: dict, count: int, request_limit: int, output: Path, config_path: Path | None = None):
    """Run a bounded explicit low/high protocol experiment with no game/tool execution."""
    if output.exists():
        raise FileExistsError(output)
    results, used = [], 0
    frozen = freeze(config, config_path=config_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    journal = output.with_suffix(".requests.jsonl")
    if journal.exists():
        raise FileExistsError(journal)
    for index, case in enumerate(corpus["cases"][:count]):
        # Alternate order to reduce time-of-run confounding in a small paired pilot.
        efforts = ("high", "low") if index % 2 == 0 else ("low", "high")
        for effort in efforts:
            if used >= request_limit:
                break
            candidate = deepcopy(config)
            candidate["provider"].update(reasoning_effort=effort, transient_retries=0, format_repair_attempts=0)
            candidate["limits"]["max_provider_requests_per_run"] = request_limit - used
            events = []
            provider = Provider(candidate, ROOT, event_sink=lambda kind, data: events.append({"type": kind, "data": deepcopy(data)}) if kind == "model_request_input" else None)
            result = {"case_id": case["id"], "effort": effort, "tools_enabled": False,
                      "label_scope": case["label_scope"], "decision": None, "error": None}
            try:
                decision = provider.decide(case["observation"], case["memory"], instructions=case["instructions"])
                result["decision"] = decision
                result["accepted"] = decision["action"] in case["accepted_actions"]
            except Exception as error:
                result["error"] = type(error).__name__ + ": " + str(error)
                result["accepted"] = False
            finally:
                used += provider.requests_used
                result["usage_records"] = provider.metadata
                result["request_inputs"] = events
                if hasattr(provider, "close"):
                    provider.close()
            results.append(result)
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(json.dumps({"case": case["id"], "effort": effort, "accepted": result["accepted"], "requests_used": used}), flush=True)
    write_new(output, {"kind": "paired_same_model_effort_pilot", "freeze": frozen, "requests": used,
                       "request_limit": request_limit, "results": results,
                       "limitations": ["No tools and no conversation history: not the production policy distribution.",
                           "Historical labels test legality; any simulator tactical sentinel remains separately scoped and is not native proof.",
                           "Small pilot is not an effort non-inferiority trial."]})


def pilot_summary(path: Path) -> dict:
    """Summarize paired requests by label scope, keeping failures and unknown usage visible."""
    evidence = read_json(path)
    grouped = defaultdict(list)
    paired = defaultdict(dict)
    identities = Counter()
    for result in evidence["results"]:
        grouped[(result["effort"], result["label_scope"])].append(result)
        inputs = [{key: event["data"].get(key) for key in ("messages", "tools", "tool_choice")}
                  for event in result.get("request_inputs", [])]
        paired[result["case_id"]][result["effort"]] = sha256(serialize(inputs).encode()).hexdigest() if inputs else None
        for usage in result["usage_records"]:
            identities[(usage.get("actual_model"), usage.get("system_fingerprint"))] += 1
    summaries = []
    for (effort, scope), results in grouped.items():
        records = [record for result in results for record in result["usage_records"]]
        totals = Counter()
        for record in records:
            totals.update({key: value for key, value in record.get("usage", {}).items() if isinstance(value, (int, float))})
        summaries.append({"effort": effort, "label_scope": scope, "cases": len(results),
            "accepted": sum(result["accepted"] for result in results), "errors": sum(result["error"] is not None for result in results),
            "requests": len(records), "unknown_usage": sum(not isinstance(record.get("usage", {}).get("prompt_tokens"), (int, float)) for record in records),
            "usage": dict(totals), "duration_ms": distribution([record.get("duration_ms") for record in records]),
            "off_peak_known_token_cost_cny": sum(totals[key] * price / 1e6 for key, price in OFF_PEAK_CNY.items())})
    return {"source": file_identity(path), "groups": summaries,
            "paired_input_identity": [{"case_id": case_id, "input_hashes": values,
                "equal": set(values) == {"low", "high"} and None not in values.values() and len(set(values.values())) == 1}
                for case_id, values in paired.items()],
            "model_observations": [{"model": key[0], "fingerprint": key[1], "requests": value} for key, value in identities.items()],
            "limitations": evidence["limitations"], "production_default_change_supported": False}


def main():
    """Expose only explicit local evidence or bounded opt-in API operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["freeze", "verify", "summarize", "collect", "window", "live", "pilot-summary"])
    parser.add_argument("--run", type=Path)
    parser.add_argument("--config", default="configs/a0-simulation.json")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--witnessed", type=Path, help="Reviewed strict simulator probe evidence to prepend as one tactical sentinel")
    parser.add_argument("--input", type=Path, help="Existing freeze or pilot evidence for read-only review")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--request-limit", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config, ROOT)
    config_path = Path(args.config) if Path(args.config).is_absolute() else ROOT / args.config
    if args.operation == "live":
        if not args.corpus or not 1 <= args.request_limit <= 40:
            parser.error("live requires --corpus and explicit --request-limit 1..40")
        live(read_json(args.corpus), config, args.count, args.request_limit, args.out, config_path)
    else:
        if args.operation in {"verify", "pilot-summary"} and not args.input:
            parser.error("this operation requires --input")
        if args.operation not in {"freeze", "verify", "pilot-summary"} and not args.run:
            parser.error("this operation requires --run")
        if args.operation == "verify":
            value = verify_freeze(read_json(args.input))
        elif args.operation == "pilot-summary":
            value = pilot_summary(args.input)
        else:
            value = freeze(config, args.run, config_path) if args.operation == "freeze" else summarize(args.run) if args.operation == "summarize" else collect(args.run, args.count) if args.operation == "collect" else window_audit(args.run)
        if args.operation == "collect" and args.witnessed:
            value["cases"].insert(0, witnessed_case(args.witnessed))
            value["collected"] = len(value["cases"])
            value["strategy_labels"] = 1
            value["limitations"].append("One synthetic tactical sentinel is backed by a strict simulator witness, not a native full-turn oracle.")
        write_new(args.out, value)
    print(json.dumps({"output": str(args.out), "operation": args.operation}, ensure_ascii=False))


if __name__ == "__main__":
    main()

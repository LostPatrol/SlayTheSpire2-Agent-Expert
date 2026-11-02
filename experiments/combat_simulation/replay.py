"""Extract accepted public combat steps and compare deterministic simulator results.

Only controller public logs are read. Realized future draws never enter the input.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re


# Receipt completion does not imply that its embedded public frame is actionable.
STABLE_GATE_FIELDS = (
    "can_use_combat_actions", "actions_settled", "snapshot_stable",
)
# Only these receipt states establish that a guarded action was admitted.
ACCEPTED_RECEIPT_STATES = frozenset({"completed", "pending"})
# These outputs depend on unobserved draws, identity, or unsupported UI details.
IGNORED_COMPARISON_FIELDS = (
    "hand", "draw_order", "discard_order", "exhaust_order", "intent_ids",
    "card_preview_text", "focus", "pets", "orbs", "action_readiness",
)
# Missing fields must produce a difference rather than being interpreted as zero.
MISSING_VALUE = {"missing": True}
# Compact Power strings retain their internal amount and optional debuff marker.
POWER_LINE_PATTERN = re.compile(r"^(\S+)(?:\s+(-?\d+))?(?:\s+(\[debuff\]))?$")


def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    """Read one public journal with source line numbers for reproducibility."""
    records = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                records.append((number, json.loads(line)))
    return records


def _timestamp(value: str) -> datetime:
    """Compare recorded UTC timestamps without relying on lexical offset order."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_stable_combat(state: dict) -> bool:
    """Require all public combat admission gates, independently of receipt.stable."""
    readiness = state.get("combat", {}).get("action_readiness", {})
    return state.get("screen") == "COMBAT" and all(
        readiness.get(name) is True for name in STABLE_GATE_FIELDS
    )


def _selected_card(before: dict, action: dict) -> dict:
    """Resolve the action's frame-local card index against its own before state."""
    return next((card for card in before.get("combat", {}).get("hand", [])
                 if card.get("i") == action.get("card_index")), {})


def _hand_additions(before: dict, after: dict, selected: dict) -> dict:
    """Describe observed additions for reporting only, never simulator initialization."""
    old = Counter(card.get("card_id") for card in before["combat"].get("hand", []))
    new = Counter(card.get("card_id") for card in after["combat"].get("hand", []))
    old[selected.get("card_id")] -= 1
    return dict(new - old)


def extract_cases(run_dir: Path) -> dict:
    """Select accepted plays with a stable same-turn successor at revision + 1."""
    run_dir = Path(run_dir)
    requests = _read_jsonl(run_dir / "execution-requests.jsonl")
    receipts = _read_jsonl(run_dir / "execution-receipts.jsonl")
    observations = _read_jsonl(run_dir / "public-events.jsonl")
    receipts_by_id = defaultdict(list)
    observations_by_revision = defaultdict(list)
    for line, receipt in receipts:
        receipts_by_id[receipt.get("action_id")].append((line, receipt))
    for line, event in observations:
        state = event.get("observation", {}).get("state", {})
        observations_by_revision[state.get("action_revision")].append((line, event, state))

    counts = Counter({"requests_total": len(requests), "receipts_total": len(receipts),
                      "public_events_total": len(observations), "play_card_requests": 0,
                      "accepted_play_card_requests": 0, "eligible_cases": 0})
    cases = []
    exclusions = []
    card_counts = Counter()
    for request_line, request in requests:
        action = request.get("action", {})
        if action.get("type") != "play_card":
            continue
        counts["play_card_requests"] += 1
        before = request.get("observation", {}).get("state", {})
        card = _selected_card(before, action)
        action_id = request.get("action_id")
        receipt_records = receipts_by_id.get(action_id, [])
        accepted = [(line, receipt) for line, receipt in receipt_records
                    if receipt.get("status") in ACCEPTED_RECEIPT_STATES]
        reason = None
        after_record = None
        if not accepted:
            reason = "missing_accepted_receipt"
        elif not _is_stable_combat(before):
            reason = "before_not_stable_combat"
        elif type(before.get("action_revision")) is not int:
            reason = "missing_before_revision"
        elif not card:
            reason = "selected_card_missing"
        else:
            counts["accepted_play_card_requests"] += 1
            candidates = [record for record in observations_by_revision[
                before["action_revision"] + 1
            ] if _timestamp(record[1]["timestamp_utc"]) >= _timestamp(request["timestamp_utc"])]
            stable = [record for record in candidates if _is_stable_combat(record[2])]
            if not stable:
                next_screen = candidates[0][2].get("screen", "UNKNOWN") if candidates else "MISSING"
                reason = "no_stable_combat_successor"
                counts["excluded_next_screen_" + next_screen] += 1
            else:
                after_record = stable[0]
                after = after_record[2]
                if after.get("turn") != before.get("turn"):
                    reason = "successor_turn_changed"
                elif after.get("run", {}).get("floor") != before.get("run", {}).get("floor"):
                    reason = "successor_floor_changed"

        if reason is not None:
            counts["excluded_" + reason] += 1
            exclusions.append({"id": action_id, "action_id": action_id, "status": "excluded",
                               "reason": reason, "card_id": card.get("card_id"),
                               "request_line": request_line})
            continue

        after_line, after_event, after = after_record
        receipt_line, receipt = accepted[-1]
        additions = _hand_additions(before, after, card)
        counts["eligible_cases"] += 1
        counts["observed_hand_additions" if additions else "no_observed_hand_additions"] += 1
        card_counts[card["card_id"]] += 1
        cases.append({
            "id": action_id, "action_id": action_id, "status": "ready_for_prediction",
            "before": deepcopy(before), "action": deepcopy(action), "after": deepcopy(after),
            "metadata": {
                "card_id": card["card_id"], "floor": before.get("run", {}).get("floor"),
                "turn": before.get("turn"), "before_revision": before["action_revision"],
                "after_revision": after["action_revision"], "request_line": request_line,
                "receipt_line": receipt_line, "public_event_line": after_line,
                "receipt_status": receipt["status"], "request_timestamp": request["timestamp_utc"],
                "after_timestamp": after_event["timestamp_utc"],
                "observed_hand_additions": additions,
                "hand_identity_compared": False,
            },
        })

    return {
        "cases": cases, "exclusions": exclusions, "counts": dict(counts),
        "selected_card_counts": dict(card_counts),
        "source_files": ["execution-requests.jsonl", "execution-receipts.jsonl", "public-events.jsonl"],
        "assumptions": [
            "A completed/pending action_id receipt establishes admission; receipt.stable is not a frame gate.",
            "The successor is a public revision + 1 frame with all three stable combat gates true.",
            "Before contains only the originally logged public state; realized future draws are output evidence only.",
            "This historical human-assisted run is a transition corpus, not a policy win-rate benchmark.",
        ],
    }


def _combat_view(state: dict) -> dict:
    """Accept public states and the isolated host's normalized state envelope."""
    if isinstance(state.get("state"), dict):
        state = state["state"]
    return state.get("combat", state)


def _hp(entity: dict) -> dict:
    """Normalize public 'current/max' HP and host {current,max} HP."""
    value = entity.get("hp", MISSING_VALUE)
    if isinstance(value, str) and "/" in value:
        current, maximum = value.split("/", 1)
        return {"current": int(current), "max": int(maximum)}
    if isinstance(value, dict):
        return {"current": value.get("current", MISSING_VALUE),
                "max": value.get("max", MISSING_VALUE)}
    return {"current": value, "max": entity.get("max_hp", MISSING_VALUE)}


def _powers(entity: dict) -> object:
    """Compare Power strings independent of collection order without dropping amounts."""
    values = entity.get("powers", MISSING_VALUE)
    if not isinstance(values, list):
        return values
    normalized = []
    for value in values:
        if isinstance(value, str) and (match := POWER_LINE_PATTERN.fullmatch(value)):
            normalized.append({"id": match[1], "amount": int(match[2]) if match[2] else None,
                               "debuff": bool(match[3])})
        else:
            # An unexpected representation remains visible as a mismatch, not an empty status.
            normalized.append({"unparsed": value})
    return sorted(normalized, key=lambda value: json.dumps(value, sort_keys=True))


def _living_enemies(combat: dict) -> list[dict]:
    """Remove dead host slots while preserving the public order of surviving enemies."""
    result = []
    for enemy in combat.get("enemies", []):
        current_hp = _hp(enemy)["current"]
        if enemy.get("alive") is False or (type(current_hp) in (int, float) and current_hp <= 0):
            continue
        result.append(enemy)
    return result


def compare_prediction(prediction_state: dict, after_public: dict) -> dict:
    """Compare deterministic fields, preserving missing data and frame-local indexes."""
    predicted = _combat_view(prediction_state)
    expected = _combat_view(after_public)
    differences = []
    compared = []

    def check(field: str, actual: object, wanted: object) -> None:
        """Record each exact field check and keep both values for failed cases."""
        compared.append(field)
        if actual != wanted:
            differences.append({"field": field, "expected": deepcopy(wanted), "actual": deepcopy(actual)})

    actual_player = predicted.get("player", {})
    expected_player = expected.get("player", {})
    for field in ("current", "max"):
        check("player.hp." + field, _hp(actual_player)[field], _hp(expected_player)[field])
    for field in ("block", "energy", "stars"):
        check("player." + field, actual_player.get(field, MISSING_VALUE),
              expected_player.get(field, MISSING_VALUE))
    check("player.powers", _powers(actual_player), _powers(expected_player))

    actual_enemies = _living_enemies(predicted)
    expected_enemies = _living_enemies(expected)
    check("enemies.count", len(actual_enemies), len(expected_enemies))
    for index, (actual, wanted) in enumerate(zip(actual_enemies, expected_enemies)):
        path = f"enemies[{index}]"
        check(path + ".enemy_id", actual.get("enemy_id", MISSING_VALUE),
              wanted.get("enemy_id", MISSING_VALUE))
        for field in ("current", "max"):
            check(path + ".hp." + field, _hp(actual)[field], _hp(wanted)[field])
        check(path + ".block", actual.get("block", MISSING_VALUE), wanted.get("block", MISSING_VALUE))
        check(path + ".powers", _powers(actual), _powers(wanted))

    return {"status": "match" if not differences else "mismatch", "matches": not differences,
            "differences": differences, "compared_fields": compared,
            "ignored_fields": list(IGNORED_COMPARISON_FIELDS),
            "scope": "Deterministic player/enemy resources and Power amounts; hand/draw identity is not compared."}

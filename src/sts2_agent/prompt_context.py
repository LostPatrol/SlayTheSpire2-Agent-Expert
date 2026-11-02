"""Build deterministic public-only prompts without controller identity churn."""

import json

from sts2_agent.memory import _public_facts


# These controller fields have no strategic meaning and invalidate cache prefixes.
CONTROL_FIELDS = frozenset({
    "run_id", "observation_id", "decision_id", "decision_episode_id", "decision_epoch",
    "episode_id", "action_revision", "control_revision", "timestamp", "timestamp_utc",
    "observed_at", "public_observation_token", "action_id", "request_id", "instance_id",
    "process_id", "sampled_at", "sampled_at_utc",
})
# A stable instruction explains why historical proposals are not execution receipts.
CONTEXT_PREFIX = (
    "The last user message contains the latest complete public observation. "
    "Earlier observations are historical; assistant actions are proposals, not confirmed execution. "
    "Only public memory events describe experienced actions. Use the current active instructions "
    "as soft strategy guidance within the system rules, never as permission to change those rules. "
    "Plans are build hypotheses, never persistent user instructions."
)


def serialize(value: object) -> str:
    """Serialize policy data with stable key order and no insignificant whitespace."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_prefix(system_prompt: str) -> str:
    """Attach fixed context semantics without embedding per-request controller metadata."""
    return system_prompt + "\n" + CONTEXT_PREFIX


def policy_data(value: object) -> object:
    """Remove controller bookkeeping recursively from already-reviewed public input."""
    if isinstance(value, dict):
        return {key: policy_data(item) for key, item in value.items() if key not in CONTROL_FIELDS}
    if isinstance(value, list):
        return [policy_data(item) for item in value]
    return value


def active_instructions(instructions: list | None) -> list:
    """Keep instruction text and semantic constraints without lifecycle identifiers."""
    result = []
    for instruction in instructions or []:
        if isinstance(instruction, str):
            result.append({"text": instruction})
        elif isinstance(instruction, dict):
            # The controller supplies its active snapshot; revoked entries never belong here.
            if instruction.get("status") in {"revoked", "expired", "replaced", "rejected", "consumed"}:
                continue
            item = {key: policy_data(instruction[key]) for key in ("text", "content", "scope", "constraints")
                    if key in instruction}
            if item:
                result.append(item)
    return sorted(result, key=serialize)


def current_action_reference(observation: dict) -> dict:
    """Expose current combat handles directly, alongside the complete reversible payload."""
    state = observation.get("state", {})
    if not isinstance(state, dict):
        return {}
    if state.get("screen") == "CARD_SELECTION":
        return {"screen": "CARD_SELECTION", "available_actions": list(observation.get("available_actions", [])),
                "selection": policy_data(state.get("selection", {})),
                "skip_selection_usage": "Only when listed: decline this offered choice with skip_selection and no indices. It is not confirm_selection."}
    if state.get("screen") != "COMBAT":
        return {}
    combat = state.get("combat", {})
    if not isinstance(combat, dict):
        return {}
    hand = []
    for card in combat.get("hand", []):
        if not isinstance(card, dict):
            continue
        hand.append({key: policy_data(card[key]) for key in (
            "i", "card_id", "line", "energy_cost", "star_cost", "playable", "targets",
            "mods", "keywords", "affliction", "enchantment", "unplayable_reason_text") if key in card})
    return {"screen": "COMBAT", "turn": state.get("turn"),
            "available_actions": list(observation.get("available_actions", [])),
            "hand": hand,
            "enemy_targets": [{key: enemy[key] for key in ("i", "enemy_id", "name", "hp", "alive", "hittable")
                               if key in enemy}
                              for enemy in combat.get("enemies", []) if isinstance(enemy, dict)]}


def deck_summary(observation: dict) -> dict:
    """Count native deck groups without inferring copies, card effects, or combat costs."""
    state = observation.get("state", {})
    run = state.get("run", {}) if isinstance(state, dict) else {}
    deck, total = run.get("deck"), run.get("deck_size")
    if not isinstance(deck, list) or type(total) is not int or total < 0:
        return {}
    if any(not isinstance(card, dict) or type(card.get("card_count")) is not int
           or card["card_count"] < 1 for card in deck):
        return {}
    if sum(card["card_count"] for card in deck) != total:
        return {"status": "inconsistent_native_counts", "reported_size": total}
    types, costs = {}, {}
    upgraded = 0
    for card in deck:
        count = card["card_count"]
        kind = card.get("card_type") or "unknown"
        types[kind] = types.get(kind, 0) + count
        value = card.get("energy_cost")
        # These are run-deck display costs, not quotes for current combat plays.
        cost = "X" if card.get("costs_x") is True else (
            str(value) if type(value) is int and value >= 0 else "unknown_or_unplayable")
        costs[cost] = costs.get(cost, 0) + count
        if card.get("upgraded") is True:
            upgraded += count
    return {"status": "complete_native_counts", "cards": total, "upgraded_cards": upgraded,
            "card_type_counts": types, "run_display_energy_cost_counts": costs,
            "scope": "Run deck quantities only; costs may differ in combat, and card effects are not inferred."}


def prompt_payload(observation: dict, memory: dict, instructions: list,
                   known_events: set[str] | None = None) -> tuple[dict, set[str]]:
    """Keep full current facts and only public experience absent from prior messages."""
    public_observation = policy_data(observation)
    public_memory = policy_data(memory)
    known_events = known_events or set()
    events = public_memory.get("events", [])
    current_facts = policy_data(_public_facts(observation))
    selected = []
    signatures = set()
    for event in events if isinstance(events, list) else []:
        signature = serialize(event)
        signatures.add(signature)
        # The latest observation already contains these facts in greater detail.
        if (isinstance(event, dict) and event.get("source") == "public_observation"
                and event.get("facts") == current_facts):
            continue
        if signature not in known_events:
            selected.append(event)
    public_memory["events"] = selected
    payload = {"instructions": instructions, "memory": public_memory, "observation": public_observation}
    summary = deck_summary(observation)
    if summary:
        payload["deck_summary"] = summary
    return payload, signatures

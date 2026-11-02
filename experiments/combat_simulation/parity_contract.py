"""Compare declared public state fields without turning absent observations into defaults."""

from __future__ import annotations

from collections import Counter
import json

from native_parity import hp_value, identifier, powers

# Schema 1 evidence keeps its historical narrow contract; new evidence uses this version.
CONTRACT_VERSION = 2
# Instance IDs are engine-local, so copies are compared by their visible attributes instead.
CARD_FIELDS = {
    "id": ("card_id", "id"), "upgraded": ("upgraded",),
    "energy_cost": ("cost", "energy_cost"), "star_cost": ("star_cost",),
    "effective_keywords": ("effective_keywords",), "affliction": ("affliction",),
    "enchantment": ("enchantment",), "dynamic_values": ("dynamic_values",),
}
# Public counters must be native completed-play totals, never dispatch-only counters.
PLAY_FIELDS = ("cards_played_this_turn", "attacks_played_this_turn", "skills_played_this_turn",
               "manual_card_plays_this_turn", "non_auto_card_plays_this_turn")
# An absent property differs from an explicitly observed null (for example, no affliction).
MISSING = object()


def canonical(value):
    """Return a stable representation for multiset elements and diagnostic values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def first(row, names):
    """Read the first present schema alias without interpreting missing as zero."""
    return next((row[name] for name in names if name in row), MISSING)


def modifier(value, kind):
    """Normalize a public modifier's identity while preserving its visible amount."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return MISSING
    name = first(value, (kind + "_id", "id"))
    if name is MISSING or value.get("amount") is None:
        return MISSING
    return {"id": identifier(name), "amount": value["amount"]}


def card_value(card, field):
    """Normalize one independently comparable visible card attribute."""
    value = first(card, CARD_FIELDS[field])
    if value is MISSING:
        return MISSING
    if value is None and field not in {"affliction", "enchantment"}:
        return MISSING
    availability = card.get("availability", {})
    if availability.get(field) is False or availability.get(field) == "unknown":
        return MISSING
    if field == "energy_cost" and card.get("costs_x") is True:
        return "X"
    if field == "star_cost" and card.get("star_costs_x") is True:
        return "X"
    if field == "id":
        return identifier(value)
    if field == "effective_keywords":
        return sorted(identifier(item) for item in value)
    if field in {"affliction", "enchantment"}:
        return modifier(value, field)
    if field == "dynamic_values":
        if any(any(row.get(key) is None for key in ("name", "base_value", "current_value", "enchanted_value")) for row in value):
            return MISSING
        return sorted(({key: row[key] for key in ("name", "base_value", "current_value", "enchanted_value")
                        if key in row} for row in value), key=canonical)
    return value


def expanded_pile(rows):
    """Expand explicit public multiplicities without treating pile order as known."""
    result = []
    for row in rows:
        entry = dict(row)
        ids = entry.get("card_ids")
        if "card_id" not in entry and isinstance(ids, list) and len(ids) == 1:
            entry["card_id"] = ids[0]
        count = entry.get("card_count", entry.get("count", 1))
        if type(count) is not int or not 0 <= count <= 1000:
            return MISSING
        result.extend([entry] * count)
    return result


def state_contract(endpoint, *, public_state=None, pending=MISSING):
    """Project public quantities plus explicit coverage for every requested field family."""
    values, unavailable, pile_instances, derivations = {}, {}, {}, {}

    def put(name, value, reason="not_exported_or_unknown"):
        """Keep unavailable values separate from ordinary comparable nulls."""
        if value is MISSING:
            unavailable[name] = reason
        else:
            values[name] = value

    def put_power_details(prefix, details):
        """Compare visible counters by power identity, ignoring engine-specific array ordering."""
        if not isinstance(details, list):
            put(prefix, MISSING)
            return
        put(prefix + ".count", len(details))
        for detail in details:
            name = first(detail, ("power_id", "id"))
            if name is MISSING:
                put(prefix + ".identity", MISSING)
                continue
            name = identifier(name)
            if name.endswith("POWER"):
                name = name[:-5]
            for field in ("amount", "display_amount", "is_debuff"):
                value = detail.get(field, MISSING)
                put(f"{prefix}.{name}.{field}", value if value is not None else MISSING)

    player = dict(endpoint.get("player", {}))
    native_history = endpoint.get("public_card_play_history", (public_state or {}).get("combat", {}).get("public_card_play_history", {}))
    if native_history.get("available") is True and native_history.get("complete") is True:
        plays = native_history.get("plays")
        if isinstance(plays, list) and all(type(row.get("this_turn")) is bool and type(row.get("auto_played")) is bool for row in plays):
            player["non_auto_card_plays_this_turn"] = sum(row["this_turn"] and not row["auto_played"] for row in plays)
            derivations["player.non_auto_card_plays_this_turn"] = "complete_native_completed_plays_filtered_this_turn_and_not_auto_played"
    history = endpoint.get("public_history", {})
    if history.get("history_complete") is True:
        for target, source in (("cards_played_this_turn", "total_card_plays_this_turn"),
                               ("attacks_played_this_turn", "total_attack_plays_this_turn"),
                               ("skills_played_this_turn", "total_skill_plays_this_turn"),
                               ("manual_card_plays_this_turn", "manual_card_plays_this_turn")):
            if source in history:
                player[target] = history[source]
    raw_hp = player.get("hp", MISSING)
    put("player.hp", hp_value(raw_hp) if raw_hp is not MISSING else MISSING)
    maximum = (raw_hp.get("max", MISSING) if isinstance(raw_hp, dict) else
               int(raw_hp.split("/")[1]) if isinstance(raw_hp, str) and "/" in raw_hp else
               player.get("max_hp", MISSING))
    put("player.max_hp", maximum if maximum is not None else MISSING)
    for name in ("energy", "stars", "block"):
        value = player.get(name, MISSING)
        put("player." + name, value if type(value) in (int, float) else MISSING)
    put("player.powers", powers(player["powers"]) if "powers" in player else MISSING)
    for name in PLAY_FIELDS:
        count = player.get(name, MISSING)
        if endpoint.get("play_count_scope") == "manual_dispatch_only":
            count = MISSING
        put("player." + name, count if type(count) is int else MISSING)
    # Power display counters can differ from stack amounts and are never inferred from them.
    put_power_details("player.power_details", player.get("power_details", MISSING))

    enemies = endpoint.get("enemies", MISSING)
    put("enemy_count", len(enemies) if enemies is not MISSING else MISSING)
    if enemies is not MISSING:
        for index, enemy in enumerate(enemies):
            prefix = f"enemies[{index}]."
            for field in ("i", "alive", "hittable", "block"):
                put(prefix + field, enemy.get(field, MISSING))
            name = first(enemy, ("enemy_id", "id"))
            put(prefix + "id", identifier(name) if name is not MISSING else MISSING)
            put(prefix + "hp", hp_value(enemy["hp"]) if "hp" in enemy else MISSING)
            put(prefix + "powers", powers(enemy["powers"]) if "powers" in enemy else MISSING)
            put_power_details(prefix + "power_details", enemy.get("power_details", MISSING))

    hand = endpoint.get("hand", MISSING)
    put("hand.count", len(hand) if hand is not MISSING else MISSING)
    if hand is not MISSING:
        for index, card in enumerate(hand):
            for field in CARD_FIELDS:
                put(f"hand[{index}].{field}", card_value(card, field))

    pile_cards = endpoint.get("pile_cards", {})
    for pile in ("draw", "discard", "exhaust"):
        put("piles." + pile + ".count", endpoint.get("piles", {}).get(pile, MISSING))
        rows = pile_cards.get(pile, MISSING)
        if rows is MISSING and public_state is not None:
            rows = public_state.get("combat", {}).get(pile, MISSING)
        rows = expanded_pile(rows) if isinstance(rows, list) else MISSING
        for field in CARD_FIELDS:
            name = f"piles.{pile}.{field}_multiset"
            if rows is MISSING:
                put(name, MISSING)
                continue
            items = [card_value(row, field) for row in rows]
            # Include identity and upgrades in every attribute multiset to keep copy association.
            identities = [(card_value(row, "id"), card_value(row, "upgraded")) for row in rows]
            if any(item is MISSING for item in items) or any(MISSING in identity for identity in identities):
                put(name, MISSING)
            else:
                put(name, sorted(canonical([identity, item]) for identity, item in zip(identities, items)))
        name = f"piles.{pile}.instances_multiset"
        descriptors = ([{field: card_value(row, field) for field in CARD_FIELDS} for row in rows]
                       if rows is not MISSING else MISSING)
        if descriptors is not MISSING:
            pile_instances[pile] = [{field: value for field, value in row.items() if value is not MISSING}
                                    for row in descriptors]
        if descriptors is MISSING or any(value is MISSING for row in descriptors for value in row.values()):
            put(name, MISSING)
        else:
            put(name, sorted(canonical(row) for row in descriptors))

    potions = endpoint.get("potions", MISSING)
    if potions is MISSING and public_state is not None:
        potions = public_state.get("run", {}).get("potions", MISSING)
    put("potions.count", len(potions) if isinstance(potions, list) else MISSING)
    if isinstance(potions, list):
        for index, potion in enumerate(potions):
            prefix = f"potions[{index}]."
            for field in ("i", "occupied", "usable"):
                put(prefix + field, potion.get(field, MISSING))
            name = first(potion, ("potion_id", "id"))
            if potion.get("occupied") is False:
                # An explicitly empty belt slot establishes absence even when null properties are omitted.
                name = None
            put(prefix + "id", identifier(name) if name not in (MISSING, None) else name)

    choice = endpoint.get("pending_choice", pending)
    put("pending_choice.present", choice is not None if choice is not MISSING else MISSING)
    if isinstance(choice, dict):
        for field in ("kind", "min_select", "max_select", "can_skip"):
            value = choice.get(field, MISSING)
            # A generic UI selector does not establish the effect's exhaust/upgrade semantics.
            if field == "kind" and value == "select_card":
                value = MISSING
            put("pending_choice." + field, value)
        options = first(choice, ("options", "candidates"))
        put("pending_choice.option_count", len(options) if isinstance(options, list) else MISSING)
        if isinstance(options, list):
            for index, card in enumerate(options):
                for field in CARD_FIELDS:
                    put(f"pending_choice.options[{index}].{field}", card_value(card, field))
    return {"contract_version": CONTRACT_VERSION, "values": values, "unavailable": unavailable,
            "pile_instances": pile_instances, "derivations": derivations}


def compare_contract(expected, actual):
    """Report differences and missing coverage separately; subset agreement is never full match."""
    left, right = dict(expected["values"]), dict(actual["values"])
    joint_fields = {}
    for pile in ("draw", "discard", "exhaust"):
        expected_rows, actual_rows = expected.get("pile_instances", {}).get(pile), actual.get("pile_instances", {}).get(pile)
        if expected_rows is None or actual_rows is None:
            continue
        known = set(CARD_FIELDS)
        for row in expected_rows + actual_rows:
            known.intersection_update(row)
        # Keep associations between all jointly known attributes even when previews are unavailable.
        if {"id", "upgraded"} <= known:
            name = f"piles.{pile}.known_instances_multiset"
            joint_fields[name] = sorted(known)
            left[name] = sorted(canonical({field: row[field] for field in sorted(known)}) for row in expected_rows)
            right[name] = sorted(canonical({field: row[field] for field in sorted(known)}) for row in actual_rows)
    fields = sorted(set(left) | set(right) | set(expected["unavailable"]) | set(actual["unavailable"]))
    compared = [field for field in fields if field in left and field in right]
    differences = [{"field": field, "expected": left[field], "actual": right[field]}
                   for field in compared if left[field] != right[field]]
    unavailable = [{"field": field, "expected": expected["unavailable"].get(field,
                    "absent" if field not in left else "available"),
                    "actual": actual["unavailable"].get(field, "absent" if field not in right else "available")}
                   for field in fields if field not in compared]
    return {"contract_version": CONTRACT_VERSION,
            "status": "mismatch" if differences else "partial_match" if unavailable else "matched",
            "differences": differences, "compared_fields": compared, "unavailable_fields": unavailable,
            "known_instance_fields": joint_fields,
            "derived_fields": {"expected": expected.get("derivations", {}), "actual": actual.get("derivations", {})},
            "coverage": {"compared": len(compared), "requested": len(fields),
                         "complete": not unavailable}}


def coverage_summary(comparisons):
    """Aggregate field-level evidence without conflating unsupported and matched cases."""
    compared, unavailable = Counter(), Counter()
    for comparison in comparisons:
        compared.update(comparison.get("compared_fields", []))
        unavailable.update(row["field"] for row in comparison.get("unavailable_fields", []))
    return {"compared_fields": dict(compared), "unavailable_fields": dict(unavailable)}

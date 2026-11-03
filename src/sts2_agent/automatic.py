"""Select single public housekeeping actions without spending a model request.

Every returned action still needs the normal observation-token and receipt gate.
Card, event, resource replacement, and other strategic choices remain with the model.
"""

from .public import ActionError, character_id, configured_start, validate_action
from .presentation import display_text, language_from_config


# These relics make an empty potion slot or potion inventory strategically valuable.
POTION_CHOICE_RELICS = frozenset({
    "SOZU", "DELICATEFROND", "PETRIFIEDTOAD", "BELTBUCKLE",
    "添水", "娇嫩蕨草", "石化蟾蜍", "腰带扣",
})
# These buttons only finish an already-made selection or dismiss an information view.
SINGLE_FINISH_ACTIONS = frozenset({
    "confirm_selection", "confirm_bundle", "confirm_unlock",
    "confirm_timeline_overlay", "close_cards_view",
})
# Only known view controls can accompany a forced map transition.
VIEW_ACTIONS = frozenset({"view_deck", "open_deck", "open_map", "close_cards_view"})
# Discarding remains available in the next scene and does not create another map edge.
TRANSITION_AUXILIARY_ACTIONS = VIEW_ACTIONS | {"discard_potion"}


def _normal(value: object) -> str:
    """Compare public IDs and localized relic aliases without separator differences."""
    return "".join(character for character in str(value).upper() if character.isalnum())


def _enabled(options: object) -> list[dict]:
    """Keep explicitly indexed public options that are not disabled."""
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)
            and type(option.get("i")) is int and not option.get("locked")
            and option.get("visible") is not False and option.get("successfully_selected") is not True
            and all(option.get(flag) is not False for flag in ("enabled", "claimable"))]


def _reward_kind(reward: dict) -> str:
    """Prefer the structured type; old public bridges retain the native type in line."""
    kind = reward.get("reward_type") or reward.get("kind")
    if not isinstance(kind, str) or not kind:
        kind = str(reward.get("line", "")).split(":", 1)[0]
    return _normal(kind)


def _potion_collection_is_free(run: dict, reward: dict, *, upstream: bool) -> bool:
    """Honor native procurement gates; upstream drains receiveable potions without a policy choice."""
    if "potion_can_receive" in reward:
        if reward["potion_can_receive"] is not True:
            return False
        if upstream:
            return True
    relics = list(run.get("relic_ids") or []) + list(run.get("relics") or [])
    if any(_normal(relic) in POTION_CHOICE_RELICS for relic in relics):
        return False
    slots = run.get("potions")
    return isinstance(slots, list) and any(
        isinstance(slot, dict) and (slot.get("occupied") is False
                                   or ("potion_id" in slot and slot["potion_id"] is None))
        for slot in slots)


def _forced_turn_end(state: dict, actions: set[str]) -> bool:
    """End a turn only when public cards and resources leave no game decision."""
    if not actions or "end_turn" not in actions or actions - {"end_turn", "play_card"}:
        return False
    combat = state.get("combat", {})
    readiness = combat.get("action_readiness", {})
    if any(readiness.get(key) is not True for key in (
            "can_use_combat_actions", "snapshot_stable", "actions_settled")):
        return False
    # A just-opened turn may briefly expose an empty hand before delayed native draws.
    played = combat.get("player", {}).get("cards_played_this_turn")
    if type(played) is not int or played <= 0:
        return False
    hand = combat.get("hand")
    if not isinstance(hand, list) or any(
            not isinstance(card, dict) or card.get("playable") is not False for card in hand):
        return False
    # A potion can matter even if drinking it is unavailable, for example Belt Buckle.
    slots = state.get("run", {}).get("potions")
    if not isinstance(slots, list) or any(
            not isinstance(slot, dict) or slot.get("occupied") is True or slot.get("potion_id")
            for slot in slots):
        return False
    return True


def automatic_decision(observation: dict, config: dict | None = None, *,
                       scene_actions: tuple[str, ...] | list[str] = (),
                       card_reward_opened: bool = False) -> dict | None:
    """Return one validated housekeeping proposal; scene actions prevent reopening shops."""
    if not isinstance(observation, dict) or (config or {}).get("automatic", {}).get("enabled") is False:
        return None
    state = observation.get("state", {})
    actions = set(observation.get("available_actions", []))
    screen = state.get("screen")
    action = None
    reason = ""
    language = language_from_config(config)

    if screen == "CHARACTER_SELECT":
        try:
            target = configured_start((config or {}).get("policy"))
        except ActionError:
            return None
        selection = state.get("character_select", {})
        if character_id(selection.get("selected")) != target["character"]:
            candidates = [row for row in _enabled(selection.get("characters"))
                          if character_id(row.get("character_id", row.get("line"))) == target["character"]]
            if "select_character" in actions and len(candidates) == 1:
                action = {"type": "select_character", "option_index": candidates[0]["i"]}
                reason = display_text(language, "select_character", character=target["character"])
        elif type(selection.get("ascension")) is int:
            current = selection["ascension"]
            if current != target["ascension"]:
                kind = "increase_ascension" if current < target["ascension"] else "decrease_ascension"
                if kind in actions:
                    action, reason = {"type": kind}, display_text(language, "ascension", ascension=target["ascension"])
            elif "embark" in actions and selection.get("embark") is True:
                action, reason = {"type": "embark"}, display_text(language, "embark")
    elif screen == "REWARD":
        reward = state.get("reward", {})
        # Looking at a card reward is automatic; choosing a card or skipping never is.
        if reward.get("pending_card_choice") or reward.get("cards"):
            return None
        options = _enabled(reward.get("rewards"))
        upstream = (config or {}).get("simulation", {}).get("run_decision_mode") in {"upstream", "assist"}
        # RunManager resolves full-belt swaps before opening the card-reward choice.
        potion_swap_needs_policy = upstream and any(
            _reward_kind(row) == "POTION" and row.get("potion_slot_available") is False
            and row.get("potion_procurement_allowed") is True and bool(row.get("potion_id"))
            and row.get("visible") is not False and row.get("successfully_selected") is not True
            for row in reward.get("rewards", []) if isinstance(row, dict))
        if "claim_reward" in actions:
            for kind, explanation in (
                    ("GOLD", "gold_reward"),
                    ("POTION", "potion_reward"),
                    ("CARD", "card_reward")):
                if kind == "CARD" and (potion_swap_needs_policy or card_reward_opened):
                    # One automatic open per reward screen; a model skip must not reopen the choice.
                    continue
                chosen = next((option for option in options if _reward_kind(option) == kind
                    and (kind != "POTION" or _potion_collection_is_free(
                        state.get("run", {}), option, upstream=upstream))), None)
                if chosen is not None:
                    action = {"type": "claim_reward", "option_index": chosen["i"]}
                    reason = display_text(language, "upstream_potion" if kind == "POTION" and upstream else explanation)
                    break
        if (action is None and not options and not potion_swap_needs_policy
                and "proceed" in actions and reward.get("can_proceed") is True):
            action, reason = {"type": "proceed"}, display_text(language, "rewards_done")
    elif screen == "MAP" and "choose_map_node" in actions and not actions - ({"choose_map_node"} | TRANSITION_AUXILIARY_ACTIONS):
        options = _enabled(state.get("map", {}).get("options"))
        if len(options) == 1:
            action = {"type": "choose_map_node", "option_index": options[0]["i"]}
            reason = display_text(language, "forced_map")
    elif screen == "SHOP":
        if "close_shop_inventory" in scene_actions and "proceed" in actions:
            action, reason = {"type": "proceed"}, display_text(language, "shop_done")
        elif ("open_shop_inventory" in actions and "open_shop_inventory" not in scene_actions
              and not actions - ({"open_shop_inventory", "proceed", "use_potion"} | TRANSITION_AUXILIARY_ACTIONS)):
            action, reason = {"type": "open_shop_inventory"}, display_text(language, "open_shop")
        elif actions - TRANSITION_AUXILIARY_ACTIONS == {"proceed"}:
            action, reason = {"type": "proceed"}, display_text(language, "proceed")
    elif screen == "COMBAT" and _forced_turn_end(state, actions):
        action, reason = {"type": "end_turn"}, display_text(language, "end_turn")
    elif actions - TRANSITION_AUXILIARY_ACTIONS == {"proceed"} and screen not in {
            "COMBAT", "REWARD", "CARD_SELECTION", "EVENT", "MODAL"}:
        action, reason = {"type": "proceed"}, display_text(language, "proceed")
    elif len(actions) == 1:
        kind = next(iter(actions))
        if kind in SINGLE_FINISH_ACTIONS:
            action, reason = {"type": kind}, display_text(language, "finish")

    if action is None:
        return None
    try:
        validate_action(observation, action, (config or {}).get("policy"))
    except (ActionError, KeyError, TypeError):
        return None
    return {"action": action, "reason": reason, "decision_source": "automatic"}

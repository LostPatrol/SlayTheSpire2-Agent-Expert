"""Action schema and semantic checks against reviewed public observations."""

from copy import deepcopy


# NoSL never exposes reload, seed editing, reward macros, debug, or multiplayer controls.
BLOCKED_ACTIONS = frozenset({
    "continue_run", "save_and_quit", "return_to_main_menu",
    "resolve_rewards", "collect_rewards_and_proceed", "abandon_run",
})
# Index namespaces match the upstream native handlers, not labels guessed by the model.
INDEX_SOURCES = {
    "select_character": ("character_select", "characters"),
    "choose_map_node": ("map", "options"),
    "claim_reward": ("reward", "rewards"),
    "choose_reward_card": ("reward", "cards"),
    "select_deck_card": ("selection", "cards"),
    "choose_treasure_relic": ("chest", "relics"),
    "choose_event_option": ("event", "options"),
    "choose_capstone_option": ("capstone", "options"),
    "choose_rest_option": ("rest", "options"),
    "buy_card": ("shop", "cards"),
    "buy_relic": ("shop", "relics"),
    "buy_potion": ("shop", "potions"),
    "use_potion": ("run", "potions"),
    "discard_potion": ("run", "potions"),
}
# These native handlers consume card_index; other listed selections consume option_index.
CARD_INDEX_ACTIONS = frozenset({"play_card"})
# Stable playable IDs and explicit legacy menu labels; never infer a class from option order.
CHARACTER_ALIASES = {
    "IRONCLAD": "IRONCLAD", "THE IRONCLAD": "IRONCLAD", "铁甲战士": "IRONCLAD", "铁甲": "IRONCLAD",
    "SILENT": "SILENT", "THE SILENT": "SILENT", "静默猎手": "SILENT",
    "REGENT": "REGENT", "THE REGENT": "REGENT", "储君": "REGENT",
    "NECROBINDER": "NECROBINDER", "THE NECROBINDER": "NECROBINDER", "亡灵契约师": "NECROBINDER",
    "DEFECT": "DEFECT", "THE DEFECT": "DEFECT", "故障机器人": "DEFECT",
}
# Native character selection permits these user-configurable ascension levels.
MAX_START_ASCENSION = 10


class ActionError(RuntimeError):
    """A policy action failed validation without receiving a substitute."""


def character_id(value: object) -> str | None:
    """Resolve only an explicit ID or known legacy title, without substring matching."""
    return CHARACTER_ALIASES.get(value.strip().upper()) if isinstance(value, str) else None


def configured_start(policy: dict | None = None) -> dict:
    """Read the requested new-run setup independently of an already running game's character."""
    policy = policy or {}
    character = character_id(policy.get("character", "IRONCLAD"))
    ascension = policy.get("ascension", 0)
    if character is None or type(ascension) is not int or not 0 <= ascension <= MAX_START_ASCENSION:
        raise ActionError("Starting policy requires a playable character and ascension 0 through 10.")
    return {"character": character, "ascension": ascension}


def observation(state: dict, run_id: str, episode: str) -> dict:
    """Preserve reviewed bridge facts, including native deck_size and grouped card_count."""
    state = deepcopy(state)
    actions = [name for name in state["available_actions"] if name not in BLOCKED_ACTIONS]
    # Menu difficulty directions remain public; validation permits only progress toward configuration.
    state["available_actions"] = actions
    parameters = {}
    for name in actions:
        if name == "play_card":
            parameters[name] = {"card_index": "combat.hand[].i", "target_index": "selected card.targets[] when nonempty"}
        elif name == "crystal_set_tool":
            parameters[name] = {"tool": "big or small"}
        elif name == "crystal_clear_cell":
            parameters[name] = {"x": "0 <= x < crystal_sphere.grid_width",
                                "y": "0 <= y < crystal_sphere.grid_height", "tool": "optional big or small"}
        elif name in INDEX_SOURCES or name == "choose_bundle":
            field = "card_index" if name in CARD_INDEX_ACTIONS else "option_index"
            parameters[name] = {field: ".".join(INDEX_SOURCES.get(name, ("bundles",))) + "[].i"}
            if name in {"use_potion", "choose_rest_option"}:
                parameters[name]["target_index"] = "selected option's visible target list when required"
        else:
            parameters[name] = {}
    return {"run_id": run_id, "observation_id": episode, "decision_episode_id": episode,
            "state": state, "available_actions": actions, "action_parameters": parameters}


def validate_action(obs: dict, action: dict, policy: dict | None = None) -> None:
    """Check current handles and configured character/difficulty without replacing a choice."""
    policy = policy or {"character": "IRONCLAD", "ascension": 0}
    name = action.get("type")
    if name not in obs["available_actions"]:
        raise ActionError("Action is absent from the public legal list.")
    if name in {"crystal_set_tool", "crystal_clear_cell"}:
        sphere = obs.get("state", {}).get("crystal_sphere", {})
        if obs.get("state", {}).get("screen") != "CRYSTAL_SPHERE" or sphere.get("is_finished") is not False:
            raise ActionError("Crystal sphere is not currently playable.")
        allowed = {"type", "tool"} if name == "crystal_set_tool" else {"type", "tool", "x", "y"}
        if set(action) - allowed:
            raise ActionError("Unexpected crystal sphere parameter.")
        if (name == "crystal_set_tool" or "tool" in action) and action.get("tool") not in {"big", "small"}:
            raise ActionError("Crystal sphere tool must be big or small.")
        if name == "crystal_clear_cell":
            remaining = sphere.get("divinations_left")
            if type(remaining) is not int or remaining <= 0:
                raise ActionError("No public divinations remain.")
            for coordinate, dimension in (("x", "grid_width"), ("y", "grid_height")):
                value, size = action.get(coordinate), sphere.get(dimension)
                if type(value) is not int or type(size) is not int or not 0 <= value < size:
                    raise ActionError("Crystal sphere coordinates must lie inside the current public grid.")
        return
    if any(key not in {"type", "card_index", "option_index", "target_index"} for key in action):
        raise ActionError("Unexpected action parameter.")
    if any(type(value) is not int for key, value in action.items() if key != "type"):
        raise ActionError("Action indices must be integers.")
    state = obs["state"]
    if name in {"embark", "select_character", "increase_ascension", "decrease_ascension"}:
        if state.get("screen") != "CHARACTER_SELECT":
            raise ActionError("Starting settings can only change on character selection before embark.")
        target = configured_start(policy)
        selection = state.get("character_select", {})
        selected = character_id(selection.get("selected"))
        current = selection.get("ascension")
        if name == "embark" and (type(current) is not int or current != target["ascension"]
                                  or selected != target["character"] or selection.get("embark") is False):
            raise ActionError("Embark requires the configured character and ascension.")
        if name in {"increase_ascension", "decrease_ascension"}:
            direction = 1 if name == "increase_ascension" else -1
            if (selected != target["character"] or type(current) is not int
                    or not 0 <= current <= MAX_START_ASCENSION
                    or (target["ascension"] - current) * direction <= 0):
                raise ActionError("Adjust ascension only toward the configured target for the selected character.")
    if name == "play_card":
        choices = state.get("combat", {}).get("hand", [])
    elif name == "choose_bundle":
        choices = state.get("bundles", [])
    elif name in INDEX_SOURCES:
        section, collection = INDEX_SOURCES[name]
        choices = state.get(section, {}).get(collection, [])
    else:
        # Native skip_selection and confirmation are separate, index-free actions.
        if set(action) != {"type"}:
            raise ActionError("This action accepts no indices.")
        return
    field = "card_index" if name in CARD_INDEX_ACTIONS else "option_index"
    chosen = next((item for item in choices if item.get("i") == action.get(field)), None)
    if chosen is None:
        raise ActionError("The selected index is absent from this observation.")
    if chosen.get("locked") or any(chosen.get(flag) is False for flag in (
        "enabled", "playable", "claimable", "affordable", "stocked")):
        raise ActionError("The selected option is disabled.")
    if name == "use_potion" and chosen.get("usable") is not True:
        raise ActionError("The selected potion is not usable.")
    if name == "discard_potion" and chosen.get("discard") is not True:
        raise ActionError("The selected potion cannot be discarded.")
    if name == "select_character":
        if character_id(chosen.get("character_id", chosen.get("line"))) != target["character"]:
            raise ActionError("Select the configured character.")
    targets = chosen.get("targets", chosen.get("valid_target_indices", []))
    if targets and action.get("target_index") not in targets:
        raise ActionError("An explicit current valid target is required.")
    if not targets and "target_index" in action:
        raise ActionError("No target is permitted for this option.")
    if set(action) - {"type", field, "target_index"}:
        raise ActionError("Wrong index namespace for this action.")

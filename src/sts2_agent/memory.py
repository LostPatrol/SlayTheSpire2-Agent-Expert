"""Retain public experience without importing raw state or card-order knowledge."""

from copy import deepcopy
import json
import re


# Only a short recent history is injected; the complete event list remains available.
CONTEXT_EVENT_LIMIT = 12
# Match the provider's short build-plan limit without storing arbitrary long prose.
PLAN_CHARACTER_LIMIT = 1200
# Selection overlays belong to their originating room rather than a new strategy scene.
SCENE_OVERLAYS = frozenset({"CARD_SELECTION", "CARD_PILE", "MODAL"})
# Screens that belong to one combat encounter rather than to the run-level planning phases.
COMBAT_SCREENS = frozenset({"COMBAT"}) | SCENE_OVERLAYS
# Map coordinates are public graph labels, never executable action indices.
MAP_COORDINATE = re.compile(r"^-?\d+,-?\d+$")
# Persisted map records must originate from this controller's reviewed observations.
PUBLIC_MAP_SOURCE = "reviewed_public_observation.state.map"
# Historical map positions cannot substitute for the latest live observation or legal options.
PUBLIC_MAP_USAGE = (
    "Historical public graph from the same act. Live observation.state.map always takes priority. "
    "last_observed_current and node visited flags describe when this graph was observed, not the "
    "current position or current visit status. Only the latest observation's available actions "
    "and map.options authorize an action; this memory contains no executable options. "
    "Unknown nodes remain unknown; do not infer their hidden contents."
)
# Small public snapshots retain important facts without copying arbitrary bridge objects.
PLAYER_FIELDS = ("hp", "block", "energy", "stars", "focus", "cards_played_this_turn")
# Card indexes belong only to the recorded observation and never establish future identity.
HAND_FIELDS = ("i", "card_id", "line", "energy_cost", "star_cost", "playable")
# Public enemy facts help explain the result of an experienced action.
ENEMY_FIELDS = ("i", "enemy_id", "name", "hp", "block", "alive", "hittable")
# Run identity is always controller-owned; game seeds and hidden IDs are excluded.
RUN_FIELDS = ("character", "ascension", "floor", "act_id", "hp", "gold")
# History records only reviewed action parameters, never arbitrary metadata from the caller.
ACTION_FIELDS = (
    "type", "card_index", "target_index", "option_index", "reward_index", "potion_index", "tool",
    "relic_index", "node_index", "bundle_index", "character_index", "index", "x", "y",
)
# Record visible choices so a new reward/event on the same screen is meaningful.
CHOICE_FIELDS = {
    "crystal_sphere": (("divinations_left", "tool", "is_finished", "grid_width", "grid_height"), {}),
    "event": (("id", "title", "finished"), {"options": ("i", "line", "locked", "proceed", "kill")}),
    "reward": (("pending_card_choice", "can_proceed"), {
        "rewards": ("i", "line", "claimable"), "cards": ("i", "card_id", "line", "selected"),
        "alternatives": ("i", "line")}),
    "selection": (("kind", "prompt", "min", "max", "selected", "confirm"), {
        "cards": ("i", "card_id", "line", "selected")}),
    "shop": (("open", "can_open", "can_close"), {
        "cards": ("i", "card_id", "line", "affordable", "on_sale"),
        "relics": ("i", "relic_id", "line", "affordable", "stocked"),
        "potions": ("i", "potion_id", "line", "affordable", "stocked")}),
    "rest": ((), {"options": ("i", "option_id", "line", "enabled", "requires_target")}),
    "chest": (("opened", "claimed"), {"relics": ("i", "relic_id", "line")}),
    "map": (("current", "boss_node", "second_boss_node"), {"options": ("i", "line")}),
    "capstone": ((), {"options": ("i", "line", "title", "description", "enabled", "locked")}),
    "character_select": (("selected", "embark", "ascension"), {"characters": ("i", "line", "locked", "selected")}),
    "modal": (("underlying_screen", "confirm", "dismiss", "confirm_label", "dismiss_label"), {}),
    "game_over": (("victory", "floor", "character", "phase", "can_continue", "can_return"), {}),
    "unlock": (("unlock_type", "can_confirm"), {}),
}


def _scalars(source: object, names: tuple[str, ...]) -> dict:
    """Copy only approved scalar leaves from an expected public object."""
    if not isinstance(source, dict):
        return {}
    return {name: value for name in names if (value := source.get(name)) is not None
            and isinstance(value, (str, int, float, bool))}


def _public_facts(observation: dict) -> dict:
    """Summarize the reviewed public state, ignoring all raw sibling objects."""
    state = observation.get("state", {})
    if not isinstance(state, dict):
        return {}
    facts = _scalars(state, ("screen", "turn"))
    facts["run"] = _scalars(state.get("run"), RUN_FIELDS)
    combat = state.get("combat", {})
    if isinstance(combat, dict):
        facts["player"] = _scalars(combat.get("player"), PLAYER_FIELDS)
        for name, fields in (("hand", HAND_FIELDS), ("enemies", ENEMY_FIELDS)):
            items = combat.get(name, [])
            if isinstance(items, list):
                facts[name] = [_scalars(item, fields) for item in items if isinstance(item, dict)]
    for name, (fields, collections) in CHOICE_FIELDS.items():
        choice = state.get(name)
        if not isinstance(choice, dict):
            continue
        facts[name] = _scalars(choice, fields)
        for collection, item_fields in collections.items():
            items = choice.get(collection)
            if isinstance(items, list):
                facts[name][collection] = [_scalars(item, item_fields) for item in items if isinstance(item, dict)]
    return facts


def _map_coordinate(value: object) -> str | None:
    """Accept only public row/column labels, not arbitrary nested map metadata."""
    return value if isinstance(value, str) and MAP_COORDINATE.fullmatch(value) else None


def _public_map_nodes(value: object) -> list[dict]:
    """Copy the already-reviewed graph's four approved node fields only."""
    if not isinstance(value, list):
        return []
    result = []
    for node in value:
        if not isinstance(node, dict) or _map_coordinate(node.get("coord")) is None:
            continue
        children = node.get("children")
        if not isinstance(node.get("node_type"), str) or not isinstance(children, list):
            continue
        result.append({"coord": node["coord"], "node_type": node["node_type"],
                       "visited": node.get("visited") is True,
                       "children": [child for child in children if _map_coordinate(child) is not None]})
    return result


class PublicMemory:
    """Keep public observations and experienced actions for one controller run."""

    def __init__(self, run_id: str):
        """Bind a fixed controller-generated run identity and empty experience."""
        self.run_id = run_id
        self.events: list[dict] = []
        self._plan = ""
        self._last_signature: str | None = None
        self._public_map: dict | None = None
        self._last_act_id = None
        self.scene_key = None
        self.scene_plan = ""

    def sync_scene(self, observation: dict) -> bool:
        """Expire room tactics at public room boundaries, keeping build hypotheses separate."""
        state = observation.get("state", {})
        run = state.get("run", {})
        if not run or not state.get("screen"):
            return False
        prefix = f"{run.get('act_id')}:{run.get('floor')}:"
        screen = state["screen"]
        if screen in SCENE_OVERLAYS and self.scene_key and self.scene_key.startswith(prefix):
            return False
        key = prefix + screen
        if key == self.scene_key:
            return False
        self.scene_key = key
        self.scene_plan = ""
        return True

    def conversation_scene(self, observation: dict) -> str:
        """Bound the retained conversation only where older observations would mislead.

        Between fights the map, shop and reward observations stay valid, so the same act keeps
        one running conversation. A new combat encounter, or a new act, starts a fresh one.
        """
        state = observation.get("state", {})
        run = state.get("run", {})
        screen = state.get("screen")
        if not run or not screen:
            return ""
        combat = screen in COMBAT_SCREENS
        if not combat:
            return f"{run.get('act_id')}:between"
        history = observation.get("public_history") or {}
        encounter = history.get("encounter_id") or (state.get("combat") or {}).get("encounter_id")
        return f"{run.get('act_id')}:combat:{encounter or run.get('floor')}"

    @staticmethod
    def starts_new_conversation(current: str, previous: str) -> bool:
        """A new act or a new combat encounter starts one; leaving combat keeps the fight's history."""
        current_act, _, current_phase = current.partition(":")
        previous_act, _, _ = previous.partition(":")
        return current_act != previous_act or current_phase.startswith("combat:")

    def set_scene_plan(self, plan: str) -> None:
        """Store an explicitly clearable tactic only for the current public scene."""
        if not isinstance(plan, str):
            raise ValueError("Scene plan must be a string.")
        self.scene_plan = plan[:PLAN_CHARACTER_LIMIT]

    @property
    def public_map(self) -> dict | None:
        """Return an isolated historical graph for persistence without exposing mutable memory."""
        return deepcopy(self._public_map)

    def remember_public_map(self, observation: dict) -> None:
        """Retain only a same-run public graph; observing a new act discards the old graph."""
        observed_run = observation.get("run_id")
        if observed_run is not None and observed_run != self.run_id:
            raise ValueError("Public map memory cannot mix controller runs.")
        state = observation.get("state", {})
        if not isinstance(state, dict):
            return
        run = state.get("run", {})
        run = run if isinstance(run, dict) else {}
        act_id = run.get("act_id")
        if type(act_id) in (str, int):
            if self._public_map is not None and self._public_map.get("act_id") != act_id:
                self._public_map = None
            self._last_act_id = act_id
        graph = state.get("map")
        if not isinstance(graph, dict):
            return
        nodes = _public_map_nodes(graph.get("nodes"))
        if not nodes:
            return
        floor = run.get("floor")
        self._public_map = {"source": PUBLIC_MAP_SOURCE, "run_id": self.run_id,
            "status": "historical_public_map", "act_id": self._last_act_id,
            "observed_floor": floor if type(floor) is int else None,
            "last_observed_current": _map_coordinate(graph.get("current")),
            "boss_node": _map_coordinate(graph.get("boss_node")),
            "second_boss_node": _map_coordinate(graph.get("second_boss_node")),
            "nodes": nodes, "usage": PUBLIC_MAP_USAGE}

    def restore_public_map(self, value: object) -> None:
        """Restore a sanitized same-run map record without appending experience events."""
        if (not isinstance(value, dict) or value.get("run_id") != self.run_id
                or value.get("source") != PUBLIC_MAP_SOURCE):
            return
        self.remember_public_map({"run_id": self.run_id, "state": {
            "run": {"act_id": value.get("act_id"), "floor": value.get("observed_floor")},
            "map": {"current": value.get("last_observed_current"),
                    "boss_node": value.get("boss_node"), "second_boss_node": value.get("second_boss_node"),
                    "nodes": value.get("nodes")}}})

    def observe(self, observation: dict, action: dict | None = None) -> None:
        """Record changed public facts or an experienced action without inferring order."""
        observed_run = observation.get("run_id")
        if observed_run is not None and observed_run != self.run_id:
            raise ValueError("Public memory cannot mix controller runs.")
        self.remember_public_map(observation)
        event = {"source": "public_observation", "facts": _public_facts(observation)}
        if action is not None:
            event["source"] = "experienced_action"
            event["action"] = _scalars(action, ACTION_FIELDS)
        # Changing read tokens or timestamps alone must not grow the fact history.
        signature = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if signature != self._last_signature:
            self.events.append(event)
            self._last_signature = signature

    def set_plan(self, plan: str) -> None:
        """Store a short model-authored build hypothesis separately from facts."""
        if not isinstance(plan, str):
            raise ValueError("Build plan must be a string.")
        self._plan = plan[:PLAN_CHARACTER_LIMIT]

    def context(self) -> dict:
        """Return recent public experience with no inferred card-order knowledge."""
        context = {
            "run_id": self.run_id,
            "events": deepcopy(self.events[-CONTEXT_EVENT_LIMIT:]),
            "plan": self._plan,
            "plan_source": "model_hypothesis",
            "scene_plan": self.scene_plan,
            "scene_scope": self.scene_key,
            "known_order": [],
        }
        if self._public_map is not None:
            context["public_map"] = self.public_map
        return context

"""JSONL service joining the original Mentor mirror to the shared native search library.

Calibration truth stays in this process. Every hypothetical operation receives a clone.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import redirect_stdout
from copy import deepcopy
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys


# Retain recent complete objects for an admitted action's concurrent background audit.
RECENT_ROOTS = 8
# Only commands that change the combat rules state advance the upstream mirror.
COMBAT_ACTIONS = {"play_card": "play", "use_potion": "potion", "end_turn": "end_turn"}
# Choice clicks are UI operations until the game's selection actually resolves.
CHOICE_ACTIONS = frozenset({"select_deck_card", "select_card", "confirm_selection", "skip_selection"})


def state_key(state: dict) -> str:
    """Bind a private state object to the exact public snapshot used by its caller."""
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MentorRuntime:
    """Carry one fight with upstream CombatMirror and serve full production search."""

    def __init__(self, deployment: Path, *, upstream_python: Path | None = None, strict: bool = False):
        """Load one DLL containing both the original Python classes and production wrapper."""
        self.deployment = deployment.resolve()
        sys.path.insert(0, str(upstream_python or self.deployment / "python"))
        sys.path.insert(0, str(self.deployment))
        self.native = importlib.import_module("mentor_native")
        sys.modules["sts2_rs"] = self.native
        from combat_mirror import CombatMirror, MirrorPolicy
        from realgame_adapter import character_display_name, to_headless_combat_play
        from realgame_mcts import prepare_card_select_mirror
        from sts2_hint.adapter import translate
        from sts2_hint.recommender import RecommendConfig

        self.translate = translate
        self.to_headless = to_headless_combat_play
        self.character_name = character_display_name
        self.prepare_choice = prepare_card_select_mirror
        self.config_type = RecommendConfig
        self.mirror = CombatMirror(policy=MirrorPolicy.STRICT if strict else MirrorPolicy.OBSERVE,
                                   label="agent-live", log_path=self.deployment / "mirror-diagnostics.jsonl")
        self.encounter = None
        self.completed = set()
        self.roots = OrderedDict()
        self.last_public = None
        self.choice_open = False
        self.diagnostics = {}

    def _wire(self, board: dict) -> dict:
        """Use both original upstream converters without recreating gameplay fields."""
        wire = self.translate(self.to_headless(board),
                              character=self.character_name((board.get("run_state") or {}).get("character")),
                              ascension=int((board.get("run_state") or {}).get("ascension") or 0), seed=0)
        # The bridge binds public observer memberships to these exact exported pile positions.
        if isinstance(board.get("draw_knowledge"), dict):
            if any(len(wire["deck"][pile]) != len((board.get(pile) or {}).get("cards") or [])
                   for pile in ("draw_pile", "discard_pile")):
                raise ValueError("mirror_draw_knowledge_pile_translation_changed")
            wire["deck"]["draw_knowledge"] = deepcopy(board["draw_knowledge"])
        return wire

    @staticmethod
    def _selection_state(public: dict, board: dict) -> dict | None:
        """Rename our visible selection envelope to the original driver's protocol."""
        selection = public.get("selection")
        if public.get("screen") not in {"CARD_SELECTION", "MODAL"} or not isinstance(selection, dict):
            return None
        return {"type": "card_select", "combat": board, "run_state": board.get("run_state", {}),
                "cards": [{"id": card.get("card_id"), "index": card.get("i", index),
                           "upgraded": card.get("upgraded", False)}
                          for index, card in enumerate(selection.get("cards", []))],
                "min_count": selection.get("min", 0), "max_count": selection.get("max", 1),
                "source": selection.get("source"), "source_model": selection.get("source_model"),
                "prompt_key": selection.get("prompt_key"), "cancelable": selection.get("cancelable")}

    def _advance_completed(self, completion: dict, public: dict) -> str | None:
        """Replay an actual completed command once, including commands chosen by the LLM."""
        action_id = completion.get("action_id")
        if not action_id or action_id in self.completed:
            return None
        if completion.get("receipt_status") != "completed":
            raise ValueError("mirror_action_completion_unproven")
        before = completion.get("before_state") or {}
        action = completion.get("action") or {}
        kind = action.get("type")
        result = None
        if self.mirror.alive and kind in COMBAT_ACTIONS:
            mapped = {"action": COMBAT_ACTIONS[kind]}
            if kind == "play_card":
                mapped["card_index"] = next(i for i, card in enumerate(before["combat"]["hand"])
                                             if card["i"] == action["card_index"])
            elif kind == "use_potion":
                # Original bindings use the compact occupied belt index.
                slots = [p for p in before.get("run", {}).get("potions", []) if p.get("potion_id")]
                mapped["slot"] = next(i for i, p in enumerate(slots) if p["i"] == action["option_index"])
            if "target_index" in action:
                mapped["target_index"] = next(i for i, e in enumerate(before["combat"]["enemies"])
                                               if e["i"] == action["target_index"])
            result = self.mirror.advance(mapped)
            self.choice_open = False
        elif self.mirror.alive and kind in CHOICE_ACTIONS:
            selection = before.get("selection") or {}
            # Toggling a multi-pick UI changes no engine state. Confirm resolves the full set once.
            waiting = (kind in {"select_deck_card", "select_card"}
                       and selection.get("requires_confirmation") is True
                       and public.get("screen") in {"CARD_SELECTION", "MODAL"})
            if not waiting:
                cards = selection.get("cards", [])
                picks = [i for i, card in enumerate(cards) if card.get("selected") is True]
                if kind in {"select_deck_card", "select_card"}:
                    picks = [next(i for i, card in enumerate(cards) if card["i"] == action["option_index"])]
                if kind == "skip_selection":
                    picks = []
                result = self.mirror.advance_through_choice(picks)
                self.choice_open = False
        self.completed.add(action_id)
        return result

    def synchronize(self, request: dict) -> dict:
        """Advance past actual events, then let the original mirror calibrate the next root."""
        public = request.get("public_state") or {}
        private = request.get("internal_state") or {}
        board = private.get("mentor_state")
        encounter = request.get("mirror_encounter")
        if encounter != self.encounter:
            self.mirror.drop("encounter_changed")
            self.encounter = encounter
            self.completed.clear()
            self.choice_open = False
        completion = request.get("completion")
        advanced = self._advance_completed(completion, public) if completion else None
        if not isinstance(board, dict):
            if public.get("screen") not in {"COMBAT", "CARD_SELECTION", "MODAL"}:
                self.mirror.drop("outside_combat")
                self.choice_open = False
                return {"status": "outside_combat"}
            raise ValueError("mentor_state_missing_rebuild_bridge")
        choice = self._selection_state(public, board)
        prior_reseeds = self.mirror.stats.seeds
        diffs = self.mirror.reconcile(self._wire(board))
        if choice is not None and not self.choice_open:
            self.prepare_choice(choice, self.config_type(), self.mirror)
            self.choice_open = True
        if not self.mirror.alive:
            raise ValueError("upstream_mirror_unavailable_after_reconcile")
        self.last_public = deepcopy(public)
        key = state_key(public)
        self.diagnostics = {"status": "synchronized", "differences": len(diffs), "advanced": advanced,
                            "reseeded": self.mirror.stats.seeds > prior_reseeds,
                            "choice_open": self.choice_open, "stats": self.mirror.stats.as_dict()}
        self.roots[key] = (self.mirror.state.clone(), deepcopy(self.diagnostics))
        self.roots.move_to_end(key)
        while len(self.roots) > RECENT_ROOTS:
            self.roots.popitem(last=False)
        return self.diagnostics

    def handle(self, request: dict) -> dict:
        """Keep lifecycle diagnostics public-safe and delegate every search to the native wrapper."""
        request = deepcopy(request)
        request.setdefault("model_dir", str(self.deployment / "models" / "ppo"))
        op = request.get("op")
        if op == "mirror_sync":
            diagnostics = self.synchronize(request)
            response = json.loads(self.native.simulation_handle('{"op":"metadata"}'))
            return {key: response[key] for key in ("ok", "backend", "rule_snapshot")} | {"mirror": diagnostics}
        if op not in {"search", "evaluate", "simulate", "inspect"}:
            return json.loads(self.native.simulation_handle(json.dumps(request)))
        key = state_key(request.get("public_state") or {})
        if key not in self.roots:
            # A new process can seed a complete board, but cannot invent a missing pending tail.
            self.synchronize(request)
        entry = self.roots.get(key)
        if entry is None:
            raise ValueError("upstream_mirror_root_unavailable")
        state, diagnostics = entry
        request.pop("internal_state", None)
        request.pop("completion", None)
        certificate = (request.get("public_state", {}).get("combat") or {}).get("draw_knowledge")
        if (request.get("read_access") != "internal" and isinstance(certificate, dict)
                and certificate.get("available") is False):
            # A parked reconcile preserves its tail; it must not reuse knowledge now declared unavailable.
            response = {"ok": False, "backend": "mentor", "rule_snapshot": "mentor-native-runtime",
                        "reason": "public_draw_knowledge_unavailable",
                        "errors": ["public_draw_knowledge_unavailable"]}
        else:
            response = json.loads(self.native.simulation_handle(json.dumps(request), state))
        response["mirror"] = deepcopy(diagnostics)
        return response


def main() -> int:
    """Serve serialized requests; raw upstream diagnostics go only to local stderr/logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        runtime = MentorRuntime(args.deployment)
    for line in sys.stdin:
        try:
            with redirect_stdout(sys.stderr):
                result = runtime.handle(json.loads(line))
        except Exception as error:
            # Exception strings can contain the truth board; never return them to a model-facing audit.
            print(f"mentor_runtime:{type(error).__name__}", file=sys.stderr, flush=True)
            result = {"ok": False, "backend": "mentor", "rule_snapshot": "mentor-native-runtime",
                      "errors": ["upstream_mirror_request_failed"], "error_kind": type(error).__name__}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

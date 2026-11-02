"""Track proven public combat facts and describe replay-observation gaps.

This never reconstructs hidden state or treats an unseen event as absent.
"""

from copy import deepcopy
import json


# Old bridge snapshots remain readable, with missing capabilities reported explicitly.
HISTORY_SCHEMA_VERSION = 2
# These are the public gates needed before a completed play can establish a history fact.
READY_FIELDS = ("can_use_combat_actions", "actions_settled", "snapshot_stable")
# Prevent incomplete old frames from producing an unbounded diagnostic payload.
MAX_COVERAGE_GAPS = 32
# Card-instance fields needed to distinguish visible modifiers and current costs.
CARD_FIELDS = ("card_type", "dynamic_values", "energy_cost", "star_cost", "costs_x",
               "star_costs_x", "effective_keywords", "keyword_state_complete", "affliction", "enchantment")
# Every public surface can contain cards later used by the simulator or the replay.
CARD_PATHS = (("combat", "hand"), ("combat", "draw"), ("combat", "discard"), ("combat", "exhaust"),
              ("run", "deck"), ("run", "piles", "draw"), ("run", "piles", "discard"),
              ("run", "piles", "exhaust"), ("selection", "cards"), ("shop", "cards"),
              ("reward", "cards"))


def observation_coverage(state: dict) -> dict:
    """Report absent replay inputs without changing or rejecting the public snapshot."""
    gaps = []
    count = 0
    omitted = 0

    def gap(path: str, reason: str) -> None:
        """Keep a bounded diagnostic while retaining the full number of missing facts."""
        nonlocal omitted
        if len(gaps) < MAX_COVERAGE_GAPS:
            gaps.append({"path": path, "reason": reason})
        else:
            omitted += 1

    version = state.get("observation_schema_version")
    if type(version) is not int:
        gap("observation_schema_version", "not_recorded")
    if state.get("screen") == "COMBAT":
        history = (state.get("combat") or {}).get("public_card_play_history") or {}
        if (history.get("available") is not True or history.get("complete") is not True
                or history.get("scope") != "current_combat"):
            gap("combat.public_card_play_history", "complete_current_combat_play_history_not_recorded")
    rows = []
    for segments in CARD_PATHS:
        value = state
        for segment in segments:
            value = value.get(segment) if isinstance(value, dict) else None
        if isinstance(value, list):
            rows.extend((".".join(segments) + f"[{index}]", row) for index, row in enumerate(value))
    for index, bundle in enumerate(state.get("bundles") or []):
        if isinstance(bundle, dict):
            rows.extend((f"bundles[{index}].cards[{card_index}]", row)
                        for card_index, row in enumerate(bundle.get("cards") or []))
    for path, card in rows:
        if not isinstance(card, dict):
            gap(path, "card_record_not_object")
            continue
        count += 1
        if not card.get("card_id") and not card.get("card_ids"):
            gap(path, "card_identity_missing")
        for field in CARD_FIELDS:
            if field not in card:
                gap(path + "." + field, "not_recorded")
        if card.get("keyword_state_complete") is False:
            gap(path + ".keyword_state_complete", "incomplete_public_keyword_state")
        modifiers = card.get("mods") or []
        if not isinstance(modifiers, list):
            modifiers = []
        if "Enchantment" in modifiers and not isinstance(card.get("enchantment"), dict):
            gap(path + ".enchantment", "visible_enchantment_without_structured_record")
        if "Affliction" in modifiers and not isinstance(card.get("affliction"), dict):
            gap(path + ".affliction", "visible_affliction_without_structured_record")
    return {"schema_version": 1, "observation_schema_version": version,
            "status": "partial" if gaps or omitted else "complete_for_declared_public_fields",
            "card_records": count, "gaps": gaps, "additional_gap_count": omitted,
            "scope": "public_observation_fields_not_hidden_state_or_complete_engine_replay"}


class PublicCombatHistory:
    """Remember confirmed controller actions without claiming complete native play history."""

    def __init__(self, saved: dict | None = None):
        """Restore only this version's derived public facts from the controller ledger."""
        self.data = deepcopy(saved) if isinstance(saved, dict) and saved.get("schema_version") == HISTORY_SCHEMA_VERSION else {}

    def observe(self, state: dict, encounter_id: str | None, identity: tuple) -> dict:
        """Reset facts across encounters/processes and retain unknown native-event history."""
        identity = list(identity)
        if (self.data.get("encounter_id") != encounter_id or self.data.get("bridge_identity") != identity
                or not self.data):
            self.data = {"schema_version": HISTORY_SCHEMA_VERSION, "encounter_id": encounter_id,
                         "bridge_identity": identity, "history_complete": False,
                         "complete_from_start": False, "permafrost_first_power_used": None,
                         "observed_manual_power_plays": 0, "completed_action_ids": [],
                         "source": "confirmed_controller_actions",
                         "reason": "complete_native_card_play_history_not_observed"}
        origin = self.data.get("combat_selection_origin")
        if isinstance(origin, dict) and not self._selection_continues(state, origin):
            self.data.pop("combat_selection_origin", None)
        self.data["last_action_revision"] = state.get("action_revision")
        self.data["turn"] = state.get("turn")
        self.data.update(history_complete=False, complete_from_start=False,
                         source="confirmed_controller_actions",
                         reason="complete_native_card_play_history_not_observed",
                         manual_card_plays_this_turn=None, manual_attack_plays_this_turn=None,
                         manual_skill_plays_this_turn=None, total_card_plays_this_turn=None,
                         total_attack_plays_this_turn=None, total_skill_plays_this_turn=None,
                         non_auto_card_plays_this_turn=None)
        if self.data.get("permafrost_first_power_used") is False:
            self.data["permafrost_first_power_used"] = None
        if state.get("screen") == "COMBAT" and type(state.get("turn")) is int:
            # A settled telegraph is witnessed information, including fixed cycles and repeat limits.
            combat = state.get("combat") or {}
            turn = state["turn"]
            turns = self.data.setdefault("enemy_turns", [])
            entry = {"turn": turn, "player": deepcopy(combat.get("player", {})),
                     "enemies": deepcopy(combat.get("enemies", []))}
            previous = next((index for index, row in enumerate(turns) if row.get("turn") == turn), None)
            # Keep the latest same-turn telegraph, e.g. after a publicly visible stun or phase change.
            if previous is None:
                turns.append(entry)
            else:
                turns[previous] = entry
            self.data["enemy_history_complete_from_start"] = bool(turns and turns[0].get("turn") == 1
                and [row.get("turn") for row in turns] == list(range(1, turn + 1)))
        native = (state.get("combat") or {}).get("public_card_play_history") or {}
        if native.get("available") is True and native.get("scope") == "current_combat" and isinstance(native.get("plays"), list):
            plays = native["plays"]
            power_plays = [row for row in plays if isinstance(row, dict)
                           and str(row.get("card_type", "")).upper() == "POWER"]
            known_types = all(isinstance(row, dict) and row.get("card_type") in {
                "Attack", "Skill", "Power", "Status", "Curse", "Quest"} for row in plays)
            complete = native.get("complete") is True and known_types
            self.data.update(history_complete=complete, complete_from_start=complete,
                             source="native_completed_local_card_plays",
                             observed_manual_power_plays=sum(row.get("auto_played") is False for row in power_plays),
                             reason=("completed_native_local_card_play_history" if complete
                                     else "native_card_play_history_incomplete"))
            series_known = complete and all(type(row.get(field)) is bool for row in plays
                                           for field in ("auto_played", "first_in_series", "this_turn"))
            if complete and all(type(row.get("this_turn")) is bool for row in plays):
                finished = [row for row in plays if row["this_turn"]]
                self.data.update(total_card_plays_this_turn=len(finished),
                                 total_attack_plays_this_turn=sum(row["card_type"] == "Attack" for row in finished),
                                 total_skill_plays_this_turn=sum(row["card_type"] == "Skill" for row in finished))
                if all(type(row.get("auto_played")) is bool for row in finished):
                    # Scarf counts non-auto completions, including replays within one manual series.
                    self.data["non_auto_card_plays_this_turn"] = sum(not row["auto_played"] for row in finished)
            if series_known:
                manual = [row for row in plays if row["this_turn"] and not row["auto_played"]
                          and row["first_in_series"]]
                self.data.update(manual_card_plays_this_turn=len(manual),
                                 manual_attack_plays_this_turn=sum(row["card_type"] == "Attack" for row in manual),
                                 manual_skill_plays_this_turn=sum(row["card_type"] == "Skill" for row in manual))
            if "PERMAFROST" in state.get("run", {}).get("relic_ids", []):
                if power_plays:
                    self.data["permafrost_first_power_used"] = True
                elif complete:
                    self.data["permafrost_first_power_used"] = False
        return self.snapshot()

    def completed(self, command: dict, receipt: dict, successor: dict) -> dict:
        """Use one exact-revision settled play as positive evidence, never as evidence of absence."""
        before = command.get("observation", {}).get("state", {})
        after = successor.get("state", {})
        self._record_selection_origin(command, receipt, after)
        action_id = command.get("action_id")
        revision = before.get("action_revision")
        ready = after.get("combat", {}).get("action_readiness", {})
        if (not action_id or action_id in self.data.get("completed_action_ids", [])
                or receipt.get("action_id") != action_id or receipt.get("status") not in {"pending", "completed"}
                or before.get("screen") != "COMBAT" or after.get("screen") != "COMBAT"
                or type(revision) is not int or after.get("action_revision") != revision + 1
                or not all(ready.get(field) is True for field in READY_FIELDS)):
            return self.snapshot()
        self.data.setdefault("completed_action_ids", []).append(action_id)
        action = command.get("action", {})
        card = next((row for row in before.get("combat", {}).get("hand", [])
                     if row.get("i") == action.get("card_index")), {}) if action.get("type") == "play_card" else {}
        if str(card.get("card_type", "")).upper() == "POWER":
            if self.data.get("source") != "native_completed_local_card_plays":
                self.data["observed_manual_power_plays"] = self.data.get("observed_manual_power_plays", 0) + 1
            if "PERMAFROST" in before.get("run", {}).get("relic_ids", []):
                self.data["permafrost_first_power_used"] = True
                self.data["source_action_id"] = action_id
        return self.snapshot()

    @staticmethod
    def _selection_continues(state: dict, origin: dict) -> bool:
        """Keep a witnessed combat choice only on its current public room's selection surface."""
        if state.get("screen") not in {"CARD_SELECTION", "MODAL"}:
            return False
        selection = state.get("selection", {})
        if (not isinstance(selection, dict) or not isinstance(selection.get("cards"), list)
                or type(selection.get("min")) is not int or type(selection.get("max")) is not int):
            return False
        underlying = [selection.get("underlying_screen"), state.get("modal", {}).get("underlying_screen")]
        if any(screen and screen != "COMBAT" for screen in underlying):
            return False
        before_run, after_run = origin.get("before", {}).get("run", {}), state.get("run", {})
        return all(before_run.get(key) == after_run.get(key) for key in ("act_id", "floor"))

    def _record_selection_origin(self, command: dict, receipt: dict, after: dict) -> None:
        """Keep only combat routing evidence; the original mirror owns pending continuations."""
        before = command.get("observation", {}).get("state", {})
        if (receipt.get("action_id") != command.get("action_id")
                or receipt.get("status") not in {"completed", "pending"}
                or type(before.get("action_revision")) is not int
                or after.get("action_revision") != before["action_revision"] + 1):
            return
        origin = self.data.get("combat_selection_origin")
        if before.get("screen") == "COMBAT":
            origin = {"encounter_id": self.data.get("encounter_id"),
                      "before": {"run": {key: before.get("run", {}).get(key) for key in ("act_id", "floor")}},
                      "source_action_id": command.get("action_id")}
        if origin and self._selection_continues(after, origin):
            self.data["combat_selection_origin"] = origin
        else:
            self.data.pop("combat_selection_origin", None)

    def snapshot(self) -> dict:
        """Expose compact public history, excluding internal deduplication metadata."""
        return {key: deepcopy(value) for key, value in self.data.items()
                if key not in {"completed_action_ids", "bridge_identity"}}

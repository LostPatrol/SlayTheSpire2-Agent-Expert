"""Verify witnessed choices, instance lifetimes, and public draw-knowledge contracts."""

from copy import deepcopy

import pytest

from sts2_agent.public_history import PublicCombatHistory

from test_public_adapter import client, frame, public_card
from test_search_adapter import internal_state, search_frame

# Match FairDecisionProtocol's selection.cards[] whitelist, excluding hand-only controls.
CHOICE_CARD_KEYS = frozenset({"i", "instance_id", "source_pile", "source_index", "selectable", "card_id", "line",
    "selected", "keywords", "mods", "effective_keywords", "keyword_state_complete", "upgraded", "energy_cost",
    "star_cost", "costs_x", "star_costs_x", "card_type", "rarity", "dynamic_values", "affliction", "enchantment", "fetch_played_this_turn"})
# These two public spellings resolve to the same pinned catalogue CardId.
CARD_ID_ALIASES = {"NOXIOUS_FUMES_CARD": "NOXIOUS_FUMES"}
# These controlled roots explicitly declare zero history; live unknown aggregates remain null.
CONTROLLED_HISTORY_ZERO = {
    "energy_spent_this_turn": 0, "hp_loss_events_this_turn": 0, "hp_loss_events_this_combat": 0,
    "doom_applied_this_turn": False, "cards_exhausted_this_turn": 0, "cards_discarded_this_turn": 0,
    "cards_drawn_by_effect_this_turn": 0, "status_cards_drawn_this_turn": 0, "cards_drawn_this_combat": 0,
    "lightning_orbs_channeled_this_combat": 0, "stars_gained_this_turn": 0, "osty_attacks_this_turn": 0,
    "powered_hits_by_target_this_turn": [], "ally_powered_hits_by_target_this_turn": [],
    "generated_cards_this_combat": None, "energy_wasted_this_combat": None, "unspent_energy_at_turn_end_sum": None,
}


def history_frame(cards, *, character="IRONCLAD", plays=(), **statistics):
    """Declare a controlled public history without inferring counters from current resources or piles."""
    state = search_frame(cards, character=character)
    state["combat"].update(deepcopy(CONTROLLED_HISTORY_ZERO))
    state["combat"].update(statistics)
    state["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_history_fixture", "plays": deepcopy(list(plays))}
    return state


def finished_play(index, name, *, kind="Attack", current=True, instance=None, auto=False,
                  first=True, ethereal=False, spent=0, value=0):
    """Model separate finished resources and Ethereal facts with no current-card keyword inference."""
    return {"sequence": index, "card_id": name, "card_type": kind, "this_turn": current,
            "known_instance_id": instance, "auto_played": auto, "first_in_series": first,
            "was_ethereal": ethereal, "energy_spent": spent, "energy_value": value, "stars_spent": 0}


def choice_card(row, index):
    """Use actual compact choice-card fields without carrying hand play/target controls into a choice."""
    return {**{key: deepcopy(value) for key, value in row.items() if key in CHOICE_CARD_KEYS},
            "i": index, "selected": False, "selectable": None}


def rollout_prefix(client, state, actions, *, seed=41):
    """Read the literal prefix snapshot without using its continuation statistics as native truth."""
    result = client.request({"op": "evaluate", "rollout": True, "public_state": state,
        "samples": 1, "simulations": 1, "max_rollout_depth": 1, "scenario_seed": seed,
        "candidate_plans": [{"id": "literal", "actions": actions}], "timeout_ms": 5000})
    assert result.get("ok"), result
    sample = result["candidates"][0]["samples"][0]
    assert sample["status"] == "completed", result
    return sample["state"]


def visible_successor(before, snapshot):
    """Construct a synthetic bridge-shaped observation from actual core prefix quantities."""
    state = deepcopy(before)
    combat = state["combat"]
    player = snapshot["player"]
    combat["player"].update(hp=player["hp"], block=player["block"], energy=player["energy"],
                            stars=player["stars"], powers=player["powers"])
    for key in CONTROLLED_HISTORY_ZERO:
        if key in snapshot.get("history_statistics", {}):
            combat[key] = deepcopy(snapshot["history_statistics"][key])
    for key in ("orb_capacity", "orb_details"):
        if key in player:
            combat["player"][key] = deepcopy(player[key])
    state["turn"] = snapshot["round"]
    originals = [row for pile in ("hand", "draw", "discard", "exhaust") for row in before["combat"][pile]]

    def card_row(card, index, pile):
        """Retain the real fixture card's public modifiers while changing its visible container."""
        matching = [row for row in originals if row.get("card_id", row.get("card_ids", [None])[0]) == card["card_id"]]
        if not matching:
            assert card["card_id"] == "DAZED", "Only the original Boost Away's base Status birth is handled here"
            matching = [public_card("DAZED", cost=0)]
        old = next((row for row in matching if card.get("card_index") is not None and row.get("i") == card["card_index"]), matching[0])
        row = deepcopy(old)
        row.update(card_id=card["card_id"], card_count=1, upgraded=card["upgraded"],
                   energy_cost=card["cost"] if card["cost"] is not None else old["energy_cost"],
                   effective_keywords=card["effective_keywords"], keyword_state_complete=True)
        if card.get("star_cost") is not None:
            row["star_cost"] = card["star_cost"]
        if card.get("fetch_played_this_turn") is not None:
            row["fetch_played_this_turn"] = card["fetch_played_this_turn"]
        if pile == "hand":
            row["i"] = index
        else:
            row.pop("i", None)
            row["card_ids"] = [card["card_id"]]
            row["line"] = f"{card['card_id']} [{row['energy_cost']}]"
        return row

    combat["hand"] = [card_row(row, index, "hand") for index, row in enumerate(snapshot["hand"])]
    for pile in ("draw", "discard", "exhaust"):
        combat[pile] = [card_row(row, index, pile) for index, row in enumerate(snapshot["pile_cards"][pile])]
    for native, simulated in zip(combat["enemies"], snapshot["enemies"]):
        native.update(hp=simulated["hp"], block=simulated["block"], powers=simulated["powers"])
    return state


def open_choice(client, before, opening, *, manual=False, snapshot=None):
    """Keep the witnessed opening action and expose sparse native indices in reversed UI order."""
    if snapshot is None:
        snapshot = rollout_prefix(client, before, [opening])
    choice = snapshot["pending_choice"]
    assert choice is not None, snapshot
    state = visible_successor(before, snapshot)
    candidates = []
    for index, option in enumerate(choice["options"]):
        row = next(deepcopy(row) for pile in ("hand", "draw", "discard", "exhaust")
                   for row in state["combat"][pile]
                   if CARD_ID_ALIASES.get(row.get("card_id"), row.get("card_id")) == option["card_id"]
                   and not any(used.get("instance_id") is not None and used.get("instance_id") == row.get("instance_id") for used in candidates))
        candidates.append(choice_card(row, 11 + index * 3))
    state.update(screen="CARD_SELECTION", selection={"kind": "combat_hand_select", "underlying_screen": "COMBAT",
        "min": choice["min_select"], "max": choice["max_select"],
        "selected": 0, "requires_confirmation": manual,
        "confirm": choice["min_select"] == 0, "cards": list(reversed(candidates))})
    state["combat"]["action_readiness"] = {"can_use_combat_actions": False, "snapshot_stable": True}
    state["available_actions"] = ["select_deck_card"] + (["confirm_selection"] if choice["min_select"] == 0 else [])
    state["public_history"] = {"combat_selection_origin": {
        "schema_version": 1, "before": deepcopy(before), "actions": [deepcopy(opening)], "action_id": "witnessed"}}
    if opening["type"] == "use_potion":
        state["run"]["potions"] = []
    return state


@pytest.mark.parametrize("source,block", [("BURNING_PACT", 0), ("SURVIVOR", 8)])
def test_card_choice_replays_deferred_card_tail_and_maps_native_indices(client, source, block):
    """Exhaust/discard selects retain the source card's actual post-choice draw/disposal hooks."""
    before = frame([public_card(source), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    if source == "SURVIVOR":
        before["run"]["character_id"] = "SILENT"
    before["combat"]["draw"] = [{**public_card("ANGER", cost=0), "card_ids": ["ANGER"], "line": "ANGER*2 [0]"}]
    opening = {"type": "play_card", "card_index": 0}
    current = open_choice(client, before, opening)
    result = client.request({"op": "search", "public_state": current, "simulations": 32,
                             "max_rollout_depth": 8, "scenario_seed": 41, "timeout_ms": 5000})
    assert result.get("ok"), result
    action = result["best"]["actions"][0]
    assert action["type"] == "select_deck_card" and action["option_index"] in (11, 14), result
    after = rollout_prefix(client, current, [{"type": "select_deck_card", "option_index": 11}])
    assert after["pending_choice"] is None and after["pending_play"] is False
    assert after["player"]["cards_played_this_turn"] == 1
    assert after["player"]["block"] == block
    assert any(row["card_id"] == source for row in after["pile_cards"]["discard"])
    if source == "BURNING_PACT":
        assert any(row["card_id"] == "STRIKE_IRONCLAD" for row in after["pile_cards"]["exhaust"])
        assert sum(row["card_id"] == "ANGER" for row in after["hand"]) == 2
    else:
        assert any(row["card_id"] == "STRIKE_IRONCLAD" for row in after["pile_cards"]["discard"])


def test_gamblers_brew_selected_subset_resumes_only_after_native_confirmation(client):
    """A witnessed native click is accumulated without discarding or drawing until confirmation."""
    before = frame([public_card("STRIKE_IRONCLAD"), public_card("DEFEND_IRONCLAD", index=1)])
    before["combat"]["draw"] = [{**public_card("ANGER", cost=0), "card_ids": ["ANGER"], "line": "ANGER*2 [0]"}]
    before["run"]["potions"] = [{"i": 2, "potion_id": "GAMBLERS_BREW", "occupied": True,
                                    "usable": True, "target": None, "targets": []}]
    current = open_choice(client, before, {"type": "use_potion", "option_index": 2}, manual=True)
    prior_selection = deepcopy(current["selection"])
    current["selection"]["cards"][0]["selected"] = True
    current["selection"]["selected"] = 1
    current["public_history"]["combat_selection_origin"]["actions"].append({
        "action": {"type": "select_deck_card", "option_index": 14}, "selection": prior_selection})
    after = rollout_prefix(client, current, [{"type": "confirm_selection"}])
    assert after["pending_choice"] is None
    assert any(row["card_id"] == "DEFEND_IRONCLAD" for row in after["pile_cards"]["discard"])
    assert sum(row["card_id"] == "ANGER" for row in after["hand"]) == 1


def test_single_selected_manual_choice_exposes_only_real_confirmation(client):
    """A native one-card selection awaiting confirm is a forced continuation, not another click."""
    before = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    current = open_choice(client, before, {"type": "play_card", "card_index": 0}, manual=True)
    previous = deepcopy(current["selection"])
    current["selection"]["cards"][1]["selected"] = True
    current["selection"].update(selected=1, confirm=True)
    current["available_actions"].append("confirm_selection")
    current["public_history"]["combat_selection_origin"]["actions"].append({
        "action": {"type": "select_deck_card", "option_index": 11}, "selection": previous})
    result = client.request({"op": "search", "public_state": current, "simulations": 8,
                             "max_rollout_depth": 8, "scenario_seed": 41, "timeout_ms": 5000})
    assert result.get("ok"), result
    assert result["best"]["actions"] == [{"type": "confirm_selection"}], result


def test_raw_selection_payload_names_remain_an_explicit_compatibility_path(client):
    """Legacy raw callers may use payload names while normal tests exercise the real compact bridge."""
    before = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    for compact, raw in (("min", "min_select"), ("max", "max_select"),
                         ("selected", "selected_count"), ("confirm", "can_confirm")):
        current["selection"][raw] = current["selection"].pop(compact)
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok") and result["best"]["actions"][0]["type"] == "select_deck_card", result


def test_choice_suffix_uses_current_native_hand_indices_after_source_play(client):
    """Opening a choice removes its source card, so suffix indices refer to the current native hand."""
    before = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    before["combat"]["draw"] = [{**public_card("ANGER", cost=0), "card_ids": ["ANGER"], "line": "ANGER*2 [0]"}]
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    defend = next(row for row in current["combat"]["hand"] if row["card_id"] == "DEFEND_IRONCLAD")
    assert defend["i"] == 1  # The source occupied index zero in the witnessed opening frame.
    after = rollout_prefix(client, current, [{"type": "select_deck_card", "option_index": 11},
        {"type": "play_card", "card_index": defend["i"], "card_id": "DEFEND_IRONCLAD"}])
    assert after["pending_choice"] is None and after["pending_play"] is False
    assert after["player"]["cards_played_this_turn"] == 2 and after["player"]["block"] == 5
    assert any(row["card_id"] == "DEFEND_IRONCLAD" for row in after["pile_cards"]["discard"])


def test_choice_witness_rejects_a_changed_public_enemy_telegraph(client):
    """Matching HP and piles cannot excuse a current intent that disagrees with the replayed AI."""
    before = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    current["combat"]["enemies"][0]["intents"] = [{"i": 0, "intent_type": "Attack", "damage": 999, "hits": 1}]
    result = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not result.get("ok") and result["best"] is None, result
    assert "combat_selection_witness_enemy_telegraph_mismatch" in result["support"]["blocking_reasons"], result


def open_generated_choice(client, potion="COLORLESS_POTION"):
    """Expose an actual generated menu using the native independent skip capability."""
    before = frame()
    before["run"]["potions"] = [{"i": 2, "potion_id": potion, "occupied": True,
                                    "usable": True, "target": None, "targets": []}]
    opening = {"type": "use_potion", "option_index": 2}
    snapshot = rollout_prefix(client, before, [opening])
    choice = snapshot["pending_choice"]
    assert choice["kind"] == "AddGeneratedFreeToHand", snapshot
    preview = client.request({"op": "inspect", "public_state": frame([
        public_card(option["card_id"], index=index, upgraded=option["upgraded"])
        for index, option in enumerate(choice["options"])])})
    assert preview.get("ok"), preview
    current = visible_successor(before, snapshot)
    current.update(screen="CARD_SELECTION", selection={"kind": "choose_card_select", "underlying_screen": "COMBAT",
        "min": choice["min_select"], "max": choice["max_select"], "selected": 0,
        "requires_confirmation": False, "confirm": False, "can_skip": True,
        "cards": [choice_card({**public_card(option["card_id"], upgraded=option["upgraded"]),
                    "effective_keywords": preview["state"]["hand"][index]["effective_keywords"], "keyword_state_complete": True}, 11 + index * 3)
                  for index, option in enumerate(choice["options"])]})
    current["run"]["potions"] = []
    current["combat"]["action_readiness"] = {"can_use_combat_actions": False, "snapshot_stable": True}
    current["available_actions"] = ["select_deck_card", "skip_selection"]
    current["public_history"] = {"combat_selection_origin": {
        "schema_version": 1, "before": deepcopy(before), "actions": [opening], "action_id": "witnessed"}}
    return before, current


@pytest.mark.parametrize("potion", ["ATTACK_POTION", "COLORLESS_POTION"])
def test_generated_choice_uses_base_preview_and_rejects_explicit_unknown_modifications(client, potion):
    """A generated card's ID cannot hide a changed visible base value from the source resolver."""
    _, current = open_generated_choice(client, potion)
    # The observed menu was produced with seed 41; this different prior must condition on the visible outcome.
    request = {"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 1729}
    baseline = client.request(request)
    assert baseline.get("ok") and baseline["best"]["actions"][0]["type"] in {"select_deck_card", "skip_selection"}, baseline
    current["selection"]["cards"][0]["dynamic_values"] = [{"name": "Damage", "base_value": 999}]
    changed = client.request(request)
    assert not changed.get("ok") and changed["best"] is None, changed
    assert "observed_generated_palette_not_in_original_generator_support" in changed["support"]["blocking_reasons"], changed
    if potion == "COLORLESS_POTION":
        current["selection"]["cards"][0].update(card_id="STRIKE_IRONCLAD", dynamic_values=[],
                                                    effective_keywords=[], keyword_state_complete=True)
        impossible = client.request(request)
        assert not impossible.get("ok") and impossible["best"] is None, impossible
        assert "observed_generated_palette_not_in_original_generator_support" in impossible["support"]["blocking_reasons"], impossible


def test_generated_choice_skip_uses_real_native_skip_and_original_rule_continuation(client):
    """An allowed native skip resolves the original potion choice without obtaining a card or confirming."""
    _, current = open_generated_choice(client)
    result = client.request({"op": "search", "public_state": current, "simulations": 64,
                             "max_rollout_depth": 2, "scenario_seed": 1729})
    assert result.get("ok"), result
    actions = [result["best"]["actions"][0], *(row["actions"][0] for row in result["alternatives"])]
    assert {"type": "skip_selection"} in actions, result
    assert {"type": "confirm_selection"} not in actions, result
    after = rollout_prefix(client, current, [{"type": "skip_selection"}])
    assert after["pending_choice"] is None and after["pending_play"] is False, after
    assert after["hand"] == [] and after["potions"] == [], after


def test_generated_choice_skip_requires_both_native_capability_and_available_action(client):
    """The upstream can_skip flag alone never authorizes a missing native control."""
    _, current = open_generated_choice(client)
    for missing in ("capability", "action"):
        blocked = deepcopy(current)
        if missing == "capability":
            blocked["selection"]["can_skip"] = False
        else:
            blocked["available_actions"].remove("skip_selection")
        result = client.request({"op": "evaluate", "rollout": True, "public_state": blocked,
            "samples": 1, "simulations": 1, "max_rollout_depth": 1, "scenario_seed": 41,
            "candidate_plans": [{"id": "forbidden_native_skip", "actions": [{"type": "skip_selection"}]}]})
        sample = result["candidates"][0]["samples"][0]
        assert sample["status"] == "needs_decision" and sample["stop_reason"] == "native_choice_skip_not_available", result


def test_card_id_alias_and_duplicate_physical_copies_keep_native_choice_mapping(client):
    """NoxiousFumesCard's published alias and two distinct Defends reproduce the real Silent choice root."""
    before = frame([public_card("NOXIOUS_FUMES_CARD"), public_card("SURVIVOR", index=1),
                    public_card("DEFEND_SILENT", index=2), public_card("DEFEND_SILENT", index=3),
                    public_card("STRIKE_SILENT", index=4)])
    before["run"]["character_id"] = "SILENT"
    for row in before["combat"]["hand"]:
        row["instance_id"] = f"fixture-{row['i']}"
    current = open_choice(client, before, {"type": "play_card", "card_index": 1})
    assert len({row["instance_id"] for row in current["selection"]["cards"]}) == 4
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "max_rollout_depth": 2})
    assert result.get("ok") and result["best"]["actions"][0]["type"] == "select_deck_card", result


def test_draw_before_choice_is_conditioned_on_the_public_new_hand_card(client):
    """PhotonCut's already observed draw constrains an unknown order before replaying its original tail."""
    before = frame([public_card("PHOTON_CUT", target="enemies"), public_card("VENERATE", index=1)])
    before["run"]["character_id"] = "REGENT"
    before["combat"]["draw"] = [{**public_card(name), "card_ids": [name], "line": f"{name} [1]"}
                                 for name in ("STRIKE_REGENT", "DEFEND_REGENT")]
    current = open_choice(client, before, {"type": "play_card", "card_index": 0, "target_index": 0})
    result = client.request({"op": "search", "public_state": current, "simulations": 8,
                             "max_rollout_depth": 2, "scenario_seed": 1})
    assert result.get("ok"), result
    after = rollout_prefix(client, current, [result["best"]["actions"][0]])
    assert after["pending_choice"] is None and after["pending_play"] is False, after
    impossible = deepcopy(current)
    newcomer = next(row for row in impossible["combat"]["hand"] if row["card_id"] != "VENERATE")
    newcomer["card_id"] = "ANGER"
    rejected = client.request({"op": "search", "public_state": impossible, "simulations": 8, "scenario_seed": 1})
    assert not rejected.get("ok") and rejected["best"] is None, rejected
    assert "combat_selection_witness_card_pile_mismatch" in rejected["support"]["blocking_reasons"], rejected


def test_draw_before_choice_keeps_previously_seen_duplicate_card_identities(client):
    """A public draw can reveal either known duplicate first, independently of the hypothetical shuffle seed."""
    before = frame([public_card("PHOTON_CUT", target="enemies", upgraded=True), public_card("VENERATE", index=1)])
    before["run"]["character_id"] = "REGENT"
    before["combat"]["hand"][1]["instance_id"] = "known-venerate"
    before["combat"]["draw"] = [{**public_card("STRIKE_REGENT"), "card_ids": ["STRIKE_REGENT"],
        "line": "STRIKE_REGENT*1 [1]", "known_instance_ids": [identity]} for identity in ("known-a", "known-b")]
    before["combat"]["draw"].append({**public_card("FALLING_STAR", cost=0, upgraded=True),
        "card_ids": ["FALLING_STAR"], "line": "FALLING_STAR+*1 [0]", "star_cost": 2,
        "known_instance_ids": ["known-star"]})
    current = open_choice(client, before, {"type": "play_card", "card_index": 0, "target_index": 0})
    assert sorted(row["card_id"] for row in current["combat"]["hand"]) == ["FALLING_STAR", "STRIKE_REGENT", "VENERATE"]
    # Both duplicate outcomes are legal; this controlled observation reveals B and leaves A undrawn.
    for row in current["combat"]["hand"]:
        row["instance_id"] = {"VENERATE":"known-venerate", "STRIKE_REGENT":"known-b", "FALLING_STAR":"known-star"}[row["card_id"]]
        row.pop("known_instance_ids", None)
    current["combat"]["draw"][0]["known_instance_ids"] = ["known-a"]
    current["selection"]["cards"] = [choice_card(row, 11 + index * 3)
        for index, row in reversed(list(enumerate(current["combat"]["hand"])))]
    for seed in (1, 2, 3, 41, 1729):
        result = client.request({"op": "search", "public_state": current, "simulations": 8,
                                 "max_rollout_depth": 2, "scenario_seed": seed})
        assert result.get("ok"), result
        assert result["best"]["actions"][0]["type"] == "select_deck_card", result


@pytest.mark.parametrize("card", ["DAZED", "CLUMSY"])
def test_unplayable_zero_energy_quote_does_not_make_the_card_playable(client, card):
    """Native cost0 and the independent Unplayable keyword remain separate facts."""
    state = frame([public_card(card, cost=0), public_card("DEFEND_IRONCLAD", index=1)])
    state["combat"]["hand"][0].update(playable=False, effective_keywords=["Unplayable", "Ethereal"],
                                      keyword_state_complete=True)
    inspected = client.request({"op": "inspect", "public_state": state})
    assert inspected.get("ok") and inspected["state"]["hand"][0]["cost"] == 0, inspected
    result = client.request({"op": "search", "public_state": state, "simulations": 8, "max_rollout_depth": 2})
    assert result.get("ok"), result
    assert result["best"]["actions"][0].get("card_index") != 0, result
    attempted = client.request({"op": "simulate", "public_state": state, "actions": [{"type": "play_card", "card_index": 0}]})
    assert not attempted.get("ok"), attempted


@pytest.mark.parametrize("monster,enemy_powers,player_powers,damage,buff", [
    ("ShrinkerBeetle", [("WEAK_POWER", 1)], [], 5, False),
    ("SnappingJaxfruit", [("STRENGTH_POWER", 2)], [], 5, True),
    ("Nibbit", [("STRENGTH_POWER", 2)], [], 14, False),
    ("ShrinkerBeetle", [("WEAK_POWER", 1), ("STRENGTH_POWER", 2), ("VULNERABLE_POWER", 1)],
        [("VULNERABLE_POWER", 1)], 10, False),
])
def test_public_telegraphs_restore_canonical_powers_once(client, monster, enemy_powers, player_powers, damage, buff):
    """The first three observations reproduce v2 pilot failures; the fourth checks combined modifier ownership."""
    state = frame([public_card("DEFEND_IRONCLAD")])
    state["turn"] = 2
    target = state["combat"]["enemies"][0]
    target.update(enemy_id=monster, power_details=[{"power_id": key, "amount": amount} for key, amount in enemy_powers],
                  intents=[{"i": 0, "intent_type": "Attack", "damage": damage, "hits": 1}])
    if buff:
        target["intents"].append({"i": 1, "intent_type": "Buff", "damage": None, "hits": 1})
    player = state["combat"]["player"]
    player["power_details"] = [{"power_id": key, "amount": amount} for key, amount in player_powers]
    for shape in ("structured", "native_both", "localized_legacy"):
        target["powers"] = [] if shape == "structured" else [
            f"{key if shape == 'native_both' else '可见效果'} {amount}" for key, amount in enemy_powers]
        player["powers"] = [] if shape == "structured" else [
            f"{key if shape == 'native_both' else '可见效果'} {amount}" for key, amount in player_powers]
        imported = client.request({"op": "inspect", "public_state": state})
        assert imported.get("ok"), (shape, imported)
        observed = sorted(tuple(row.split()[:2]) for row in imported["state"]["enemies"][0]["powers"])
        assert observed == sorted((key, str(amount)) for key, amount in enemy_powers), (shape, imported)
        result = client.request({"op": "search", "public_state": state, "simulations": 8, "max_rollout_depth": 2})
        assert result.get("ok"), (shape, result)
        assert not any("public_intent_move" in reason for reason in result["support"]["blocking_reasons"]), result


@pytest.mark.parametrize("legacy", ["STRENGTH_POWER 1", "WEAK_POWER 2"])
def test_conflicting_power_identity_or_amount_does_not_silently_override_a_public_fact(client, legacy):
    """Canonical details win decoding but a contradictory native line keeps the conflict visible."""
    state = frame([public_card("DEFEND_IRONCLAD")])
    state["combat"]["enemies"][0].update(enemy_id="ShrinkerBeetle", powers=[legacy],
        power_details=[{"power_id": "WEAK_POWER", "amount": 1}],
        intents=[{"i": 0, "intent_type": "Attack", "damage": 5, "hits": 1}])
    result = client.request({"op": "search", "public_state": state, "simulations": 8})
    assert not result.get("ok") and result["best"] is None, result
    assert "public_power_line_detail_conflict:WEAK_POWER" in result["support"]["blocking_reasons"], result


@pytest.mark.parametrize("mutation,reason", [
    ("missing_origin", "combat_selection_origin_not_witnessed"),
    ("resource", "combat_selection_witness_resource_mismatch"),
    ("option", "combat_choice_options_do_not_match_witnessed_effect"),
    ("bounds", "combat_choice_bounds_do_not_match_witnessed_effect"),
])
def test_unwitnessed_or_mismatched_choices_are_explicitly_unsupported(client, mutation, reason):
    """Neither localized prompts nor a surviving subset of random worlds can fabricate a choice."""
    before = frame([public_card("BURNING_PACT"), public_card("STRIKE_IRONCLAD", index=1),
                    public_card("DEFEND_IRONCLAD", index=2)])
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    if mutation == "missing_origin":
        current.pop("public_history")
    elif mutation == "resource":
        current["combat"]["player"]["energy"] += 1
    elif mutation == "option":
        current["selection"]["cards"][0]["card_id"] = "ANGER"
    else:
        current["selection"]["max"] = 2
    result = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not result.get("ok") and result["best"] is None, result
    assert reason in result["support"]["blocking_reasons"], result


def completed_public_step(tracker, before, after, action, action_id):
    """Feed a synthetic receipt and actual core observations through the production history collector."""
    scope = ("cost-fixture", "bridge")
    observed = tracker.observe(before, "cost-fixture-encounter", scope)
    command = {"action_id": action_id, "action": deepcopy(action),
               "observation": {"state": deepcopy(before), "public_history": observed}}
    # Match session ordering: observing COMBAT retires the open choice before receipt settlement.
    tracker.observe(after, "cost-fixture-encounter", scope)
    tracker.completed(command, {"action_id": action_id, "status": "completed"}, {"state": after})
    after["public_history"] = tracker.observe(after, "cost-fixture-encounter", scope)


def closed_touch_fixture(client, *, single=False, cards=None, character="IRONCLAD", tracker=None):
    """Record the real original potion effect without constructing a core pending choice or cost flags."""
    before = frame(cards if cards is not None else [public_card("STRIKE_IRONCLAD", target="enemies"),
                    *([] if single else [public_card("DEFEND_IRONCLAD", index=1)])])
    before["run"]["character_id"] = character
    for row in before["combat"]["hand"]:
        row["instance_id"] = f"seen-card-{row['i']}"
    before["action_revision"] = 0
    before["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    before["run"]["potions"] = [{"i": 2, "potion_id": "TOUCH_OF_INSANITY", "occupied": True,
                                    "usable": True, "target": None, "targets": []}]
    opening = {"type": "use_potion", "option_index": 2}
    tracker = tracker or PublicCombatHistory()
    if single:
        snapshot = rollout_prefix(client, before, [opening])
        after = visible_successor(before, snapshot)
        after["run"]["potions"] = []
        after["action_revision"] = 1
        completed_public_step(tracker, before, after, opening, "touch-auto")
    else:
        choice = open_choice(client, before, opening)
        choice["action_revision"] = 1
        completed_public_step(tracker, before, choice, opening, "touch-open")
        selected = {"type": "select_deck_card", "option_index": 11}
        snapshot = rollout_prefix(client, choice, [selected])
        after = visible_successor(choice, snapshot)
        after.update(screen="COMBAT", action_revision=2)
        after.pop("selection", None)
        after["combat"]["action_readiness"] = deepcopy(before["combat"]["action_readiness"])
        completed_public_step(tracker, choice, after, selected, "touch-close")
    assert len(after["public_history"]["combat_cost_origins"]) == 1
    return after


def closed_heirloom_fixture(client, manual):
    """Observe the original live-source clone and record its newly public opaque identity through the collector."""
    tracker = PublicCombatHistory()
    cards = [public_card("CALAMITY", cost=3), public_card("HEIRLOOM_HAMMER", index=1, cost=2, target="enemies"),
             public_card("DEFEND_REGENT", index=2)]
    if manual:
        cards.append(public_card("FINESSE", index=3, cost=0))
    before = closed_touch_fixture(client, cards=cards, character="REGENT", tracker=tracker)
    opening = {"type": "play_card", "card_index": 1, "target_index": 0}
    if manual:
        choice = open_choice(client, before, opening)
        choice["action_revision"] = before["action_revision"] + 1
        completed_public_step(tracker, before, choice, opening, "heirloom-open")
        closing = {"type": "select_deck_card", "option_index": 11}
        snapshot = rollout_prefix(client, choice, [closing])
        source = choice
    else:
        snapshot = rollout_prefix(client, before, [opening])
        closing = opening
        source = before
    assert snapshot["pending_choice"] is None and snapshot["pending_play"] is False, snapshot
    after = visible_successor(before, snapshot)
    after.update(screen="COMBAT", action_revision=source["action_revision"] + 1)
    after.pop("selection", None)
    after["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    # This controlled public successor assigns a fresh label only to the original rule's newly visible clone.
    for row, actual in zip(after["combat"]["hand"], snapshot["hand"]):
        if actual.get("card_index") is None:
            row["instance_id"] = "seen-heirloom-copy"
    completed_public_step(tracker, source, after, closing, "heirloom-close")
    assert len(after["public_history"]["combat_cost_origins"]) == 2, after["public_history"]
    return after


@pytest.mark.parametrize("manual", [False, True])
def test_heirloom_copy_inherits_proven_permanent_cost_through_a_new_public_instance(client, manual):
    """Automatic and selected live-template clones retain actual permanent modifiers across a later action."""
    current = closed_heirloom_fixture(client, manual)
    copy = next(row for row in current["combat"]["hand"] if row["instance_id"] == "seen-heirloom-copy")
    inspected = client.request({"op": "inspect", "public_state": current})
    assert inspected.get("ok"), inspected
    imported = next(row for row in inspected["state"]["hand"] if row["card_index"] == copy["i"])
    assert imported["cost"] == 0 and imported["cost_observation_only"] is False, inspected
    defend = next(row for row in current["combat"]["hand"] if row["card_id"] == "DEFEND_REGENT")
    after = rollout_prefix(client, current, [{"type": "play_card", "card_index": defend["i"]}])
    clone = next(row for row in after["hand"] if row["card_index"] == copy["i"])
    assert clone["cost"] == 0 and clone["cost_observation_only"] is False, after


def test_heirloom_copy_does_not_assign_a_permanent_modifier_without_a_new_public_instance(client):
    """A cost-bearing live clone cannot obtain an invented physical identity from a duplicate card ID."""
    current = closed_heirloom_fixture(client, False)
    next(row for row in current["combat"]["hand"] if row["instance_id"] == "seen-heirloom-copy").pop("instance_id")
    # Remove the source of the proof too; dropping only the current label leaves a legitimately observed origin ID.
    closed = current["public_history"]["combat_cost_origins"][-1]["after"]
    next(row for row in closed["combat"]["hand"] if row["instance_id"] == "seen-heirloom-copy").pop("instance_id")
    result = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not result.get("ok") and "combat_cost_copy_new_instance_not_publicly_identifiable" in str(result), result


@pytest.mark.parametrize("single", [False, True])
def test_completed_touch_keeps_free_modifier_after_the_card_enters_a_hidden_pile(client, single):
    """A multi-option selection and the one-option automatic branch retain their original combat lifetime."""
    current = closed_touch_fixture(client, single=single)
    imported = client.request({"op": "inspect", "public_state": current})
    assert imported.get("ok"), imported
    touched = next(row for row in imported["state"]["hand"] if row["card_id"] == "STRIKE_IRONCLAD")
    assert touched["cost"] == 0 and touched["cost_observation_only"] is False, imported
    snapshot = rollout_prefix(client, current, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    after = visible_successor(current, snapshot)
    # The real producer publishes only previously seen identities, unordered within each pile group.
    for row in after["combat"]["discard"]:
        row["known_instance_ids"] = [row.pop("instance_id")]
    result = client.request({"op": "search", "public_state": after, "simulations": 8, "max_rollout_depth": 2})
    assert result.get("ok"), result
    assert not result["support"]["blocking_reasons"], result
    inspected = client.request({"op": "inspect", "public_state": after})
    freed = next(row for row in inspected["state"]["pile_cards"]["discard"] if row["card_id"] == "STRIKE_IRONCLAD")
    assert freed["cost"] == 0 and freed["supported"] is True and freed["cost_observation_only"] is False, inspected
    missing = deepcopy(after)
    missing["combat"]["discard"][0].pop("known_instance_ids")
    rejected = client.request({"op": "search", "public_state": missing, "simulations": 8})
    assert not rejected.get("ok") and "card_energy_cost_not_reconstructible" in str(rejected), rejected


@pytest.mark.parametrize("mutation,reason", [
    ("closed_outcome", "combat_selection_witness_resource_mismatch"),
    ("current_quote", "combat_cost_origin_current_quote_contradicts_rule"),
    ("encounter", "combat_cost_origin_not_witnessed"),
    ("duplicate", "duplicate_public_card_instance_id"),
])
def test_completed_cost_witness_rejects_contradictory_outcomes_and_instance_bindings(client, mutation, reason):
    """Closed receipts do not excuse a changed observed outcome, current quote, encounter, or physical identity."""
    current = closed_touch_fixture(client)
    if mutation == "closed_outcome":
        current["public_history"]["combat_cost_origins"][0]["after"]["combat"]["player"]["energy"] += 1
    elif mutation == "current_quote":
        current["combat"]["hand"][0]["energy_cost"] = 1
    elif mutation == "encounter":
        current["public_history"]["encounter_id"] = "different-encounter"
    else:
        current["combat"]["discard"] = [{**public_card("STRIKE_IRONCLAD", cost=0),
            "card_ids": ["STRIKE_IRONCLAD"], "line": "STRIKE_IRONCLAD [0]", "known_instance_ids": ["seen-card-0"]}]
    result = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not result.get("ok") and result["best"] is None and reason in str(result), result


def test_touch_filters_local_cost_during_a_global_free_window(client):
    """Native Touch excludes global discounts from its filter, so Scarf cannot hide a local-cost target."""
    before = frame([public_card("STRIKE_IRONCLAD", cost=0, target="enemies"),
                    public_card("DEFEND_IRONCLAD", index=1, cost=0)])
    for row in before["combat"]["hand"]:
        row["instance_id"] = f"scarf-card-{row['i']}"
    before["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4], potions=[{
        "i": 2, "potion_id": "TOUCH_OF_INSANITY", "occupied": True, "usable": True, "target": None, "targets": []}])
    before["combat"]["player"].update(cards_played_this_turn=4, attacks_played_this_turn=4, skills_played_this_turn=0)
    before["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    before["action_revision"] = 0
    opening = {"type": "use_potion", "option_index": 2}
    choice = open_choice(client, before, opening)
    choice["action_revision"] = 1
    tracker = PublicCombatHistory()
    completed_public_step(tracker, before, choice, opening, "touch-scarf-open")
    selected = {"type": "select_deck_card", "option_index": 11}
    snapshot = rollout_prefix(client, choice, [selected])
    after = visible_successor(choice, snapshot)
    after.update(screen="COMBAT", action_revision=2)
    after.pop("selection", None)
    after["combat"]["action_readiness"] = deepcopy(before["combat"]["action_readiness"])
    completed_public_step(tracker, choice, after, selected, "touch-scarf-close")
    final = rollout_prefix(client, after, [{"type": "play_card", "card_index": 1}])
    assert final["player"]["energy"] == before["combat"]["player"]["energy"], final
    assert final["hand"][0]["card_id"] == "STRIKE_IRONCLAD" and final["hand"][0]["cost"] == 0, final


def test_automatic_touch_does_not_prove_permanence_when_a_global_discount_hides_local_cost(client):
    """Native temporary-free/no-op and local-cost/permanent outcomes can have identical Scarf ALL quotes."""
    before = frame([public_card("DEFEND_IRONCLAD", cost=0)])
    before["combat"]["hand"][0]["instance_id"] = "seen-masked-defend"
    before["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4], potions=[{
        "i":2,"potion_id":"TOUCH_OF_INSANITY","occupied":True,"usable":True,"target":None,"targets":[]}])
    before["combat"]["player"].update(cards_played_this_turn=4, attacks_played_this_turn=4, skills_played_this_turn=0)
    before["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    before["action_revision"] = 0
    # Both original-DLL counterfactuals have this public successor; no local modifier flag is supplied.
    after = deepcopy(before)
    after["run"]["potions"] = []
    after["action_revision"] = 1
    completed_public_step(PublicCombatHistory(), before, after,
                          {"type":"use_potion","option_index":2}, "masked-touch")
    result = client.request({"op":"search","public_state":after,"simulations":8})
    assert not result.get("ok") and "combat_cost_origin_automatic_local_cost_hidden_by_global_modifier" in str(result), result


def knowledge_card(name, *, perfect=False, cost=1, identity=None):
    """Build a controlled complete pile face with the actual certificate row schema."""
    row = public_card(name, cost=cost)
    row.pop("i")
    row.update(card_ids=[name], card_count=1, line=f"{name}*1 [{cost}]", dynamic_values=[],
               known_instance_ids=[identity] if identity else [], effective_keywords=[], keyword_state_complete=True)
    if perfect:
        row.update(enchantment={"enchantment_id":"PERFECT_FIT","amount":1}, mods=["Enchantment","PERFECT_FIT"])
    return row


def attach_draw_knowledge(state, *, bands=(), inserted=(), discard_bands=(), kind="combat_reshuffle"):
    """Attach an explicitly synthetic public event certificate using production field names and face rows."""
    state["combat"]["draw_knowledge"] = {"schema_version":1,"available":True,"reason":None,
        "source":"controlled_public_event_fixture","kind":kind,
        "bands":[{"cards":deepcopy(cards)} for cards in bands], "inserted":deepcopy(list(inserted)),
        "discard_bands":[{"cards":deepcopy(cards)} for cards in discard_bands]}


def knowledge_draw_worlds(client, state, *, samples=8):
    """Apply one original draw effect in fixed common worlds, without interpreting rollout wins as native evidence."""
    result = client.request({"op":"evaluate","rollout":True,"public_state":state,
        "candidate_plans":[{"id":"draw_one","actions":[{"type":"play_card","card_index":0}]}],
        "samples":samples,"simulations":samples,"max_rollout_depth":1,"scenario_seed":41,"timeout_ms":5000})
    assert result.get("ok"), result
    reports = result["candidates"][0]["samples"]
    assert len(reports) == samples and all(report["status"] == "completed" for report in reports), result
    return [report["state"] for report in reports]


@pytest.mark.parametrize("ordinary_top", [False, True])
def test_public_draw_bands_preserve_priority_and_known_ordinary_top_in_eight_worlds(client, ordinary_top):
    """Explicit Top can precede PF; a marked card is not an unconditional magnet for every draw."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    perfect = knowledge_card("DEFEND_IRONCLAD", perfect=True, identity="seen-perfect")
    ordinary = knowledge_card("STRIKE_IRONCLAD", identity="seen-ordinary")
    state["combat"]["draw"] = [perfect, ordinary]
    groups = [[ordinary],[perfect]] if ordinary_top else [[perfect]]
    attach_draw_knowledge(state, bands=groups)
    expected = "STRIKE_IRONCLAD" if ordinary_top else "DEFEND_IRONCLAD"
    assert all(world["hand"][0]["card_id"] == expected for world in knowledge_draw_worlds(client,state))


def test_initial_shuffle_certificate_does_not_activate_perfect_fit(client):
    """An observed initial shuffle leaves both ordinary and PF cards in the unknown middle."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [knowledge_card("DEFEND_IRONCLAD",perfect=True),knowledge_card("STRIKE_IRONCLAD")]
    attach_draw_knowledge(state,kind="initial_shuffle")
    worlds = knowledge_draw_worlds(client,state,samples=32)
    assert {world["hand"][0]["card_id"] for world in worlds} == {"DEFEND_IRONCLAD","STRIKE_IRONCLAD"}


def test_future_reshuffle_uses_witnessed_discard_chronology_in_eight_worlds(client):
    """Native PF listener order reverses the old discard receiver, before the first reshuffle draw."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    bash = knowledge_card("BASH",perfect=True,cost=2)
    defend = knowledge_card("DEFEND_IRONCLAD")
    strike = knowledge_card("STRIKE_IRONCLAD",perfect=True)
    state["combat"]["discard"] = [strike,defend,bash]
    attach_draw_knowledge(state,kind="initial_shuffle",discard_bands=[[bash],[defend],[strike]])
    assert all(world["hand"][0]["card_id"] == "STRIKE_IRONCLAD" for world in knowledge_draw_worlds(client,state))


def test_missing_nonclosed_or_incomplete_draw_certificates_remain_explicitly_unsupported(client):
    """A current pile enchantment and canonical row order cannot replace a witnessed shuffle history."""
    for mutation in ("missing","nonclosed","discard_omitted"):
        state = search_frame([public_card("SHRUG_IT_OFF")])
        perfect = knowledge_card("DEFEND_IRONCLAD",perfect=True)
        if mutation == "discard_omitted":
            state["combat"]["discard"] = [perfect]
            attach_draw_knowledge(state)
        else:
            state["combat"]["draw"] = [perfect]
            if mutation == "nonclosed":
                attach_draw_knowledge(state)
                state["combat"]["draw_knowledge"].update(available=False,reason="removal_after_random_insertion_requires_posterior")
        result = client.request({"op":"search","public_state":state,"simulations":8})
        assert not result.get("ok") and "public_draw_knowledge_unavailable:" in str(result), (mutation,result)


def test_private_pf_order_cannot_override_public_bands_or_invent_discard_order(client):
    """Authorized RNG/draw order must still respect public constraints and the omitted native discard chronology."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    perfect = knowledge_card("DEFEND_IRONCLAD",perfect=True)
    ordinary = knowledge_card("STRIKE_IRONCLAD")
    state["combat"]["draw"] = [perfect,ordinary]
    attach_draw_knowledge(state,bands=[[perfect]])
    private = internal_state(state,draw=[ordinary,perfect])
    result = client.request({"op":"search","read_access":"internal","public_state":state,"internal_state":private,"simulations":8})
    assert not result.get("ok") and "internal_draw_order_contradicts_public_draw_bands" in str(result), result
    state["combat"]["discard"] = [knowledge_card("BASH",perfect=True,cost=2),knowledge_card("STRIKE_IRONCLAD",perfect=True)]
    attach_draw_knowledge(state,bands=[[perfect]],discard_bands=[state["combat"]["discard"]])
    private = internal_state(state,draw=[perfect,ordinary])
    result = client.request({"op":"search","read_access":"internal","public_state":state,"internal_state":private,"simulations":8})
    assert not result.get("ok") and "internal_perfect_fit_requires_observed_discard_order" in str(result), result


def test_nontrivial_public_order_and_historical_information_gaps_survive_without_current_pf(client):
    """Ordinary Top placements and later nonclosed posteriors remain relevant when no PF card is present."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    defend = knowledge_card("DEFEND_IRONCLAD")
    strike = knowledge_card("STRIKE_IRONCLAD")
    state["combat"]["draw"] = [defend,strike]
    attach_draw_knowledge(state,bands=[[strike]],kind="public_rule_state")
    assert all(world["hand"][0]["card_id"] == "STRIKE_IRONCLAD" for world in knowledge_draw_worlds(client,state))
    state["combat"]["draw_knowledge"].update(available=False,
        reason="removal_after_random_insertion_requires_posterior",bands=[])
    result = client.request({"op":"search","public_state":state,"simulations":8})
    assert not result.get("ok") and "public_draw_knowledge_unavailable:" in str(result), result


def test_structured_pile_multiplicity_binds_the_full_certificate_group(client):
    """The production numeric count takes precedence over a legacy display line's single-card text."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    perfect = knowledge_card("DEFEND_IRONCLAD",perfect=True)
    perfect.update(card_count=2,known_instance_ids=["seen-pf-a","seen-pf-b"])
    state["combat"]["draw"] = [perfect]
    attach_draw_knowledge(state,bands=[[perfect]])
    worlds = knowledge_draw_worlds(client,state)
    assert all(world["hand"][0]["card_id"] == "DEFEND_IRONCLAD"
        and len(world["pile_cards"]["draw"]) == 1 for world in worlds)


def surrounded_frame():
    """Use the actual Crusher12/Rocket4 public telegraphs that jointly require the player to face Left."""
    state = search_frame()
    state["combat"]["player"]["powers"] = ["SURROUNDED_POWER 1"]
    state["combat"]["enemies"] = [
        {"i": 0, "enemy_id": "Crusher", "instance_id": "crusher-visible", "hp": "208/209",
         "block": 0, "alive": True, "hittable": True,
         "powers": ["BACK_ATTACK_LEFT_POWER 1", "CRAB_RAGE_POWER 1"],
         "intents": [{"intent_type": "Attack", "damage": 12, "hits": 1}]},
        {"i": 1, "enemy_id": "Rocket", "instance_id": "rocket-visible", "hp": "198/199",
         "block": 0, "alive": True, "hittable": True,
         "powers": ["BACK_ATTACK_RIGHT_POWER 1", "CRAB_RAGE_POWER 1"],
         "intents": [{"intent_type": "Attack", "damage": 4, "hits": 1}]}]
    return state


@pytest.mark.parametrize("direct", [False, True])
def test_surrounded_facing_uses_its_public_property_or_joint_original_intent_constraints(client, direct):
    """Both direct and uniquely inferred Facing drive the actual enemy damage pipeline, without double-counting."""
    state = surrounded_frame()
    if direct:
        state["combat"]["player"]["power_details"] = [{
            "index": 0, "power_id": "SURROUNDED_POWER", "amount": 1, "facing": "Left"}]
    after = rollout_prefix(client, state, [{"type": "end_turn"}])
    assert after["player"]["hp"]["current"] == 64, after
    if direct:
        state["combat"]["player"]["power_details"][0]["facing"] = "Right"
        mismatch = client.request({"op": "search", "public_state": state, "simulations": 8})
        assert not mismatch.get("ok") and "public_intent_move_not_matched" in str(mismatch), mismatch


def test_surrounded_nonattack_intents_do_not_prove_a_hidden_facing(client):
    """A public property remains necessary when both possible Facing values fit every visible Buff intent."""
    state = surrounded_frame()
    for row in state["combat"]["enemies"]:
        row["intents"] = [{"intent_type": "Buff"}]
    unknown = client.request({"op": "search", "public_state": state, "simulations": 8})
    assert not unknown.get("ok") and "surrounded_facing_not_reconstructible_from_public_intents" in str(unknown), unknown
    state["combat"]["player"]["power_details"] = [{
        "index": 0, "power_id": "SURROUNDED_POWER", "amount": 1, "facing": "Left"}]
    known = client.request({"op": "search", "public_state": state, "simulations": 8, "max_rollout_depth": 1})
    assert known.get("ok") and not known["support"]["blocking_reasons"], known


def test_choice_after_ordinary_enemy_death_rebinds_the_real_dense_target_index(client):
    """A proven ordinary death can remove its row; subsequent native target zero still addresses the surviving UID."""
    before = search_frame([public_card("PHOTON_CUT", target="enemies"),
                           public_card("STRIKE_REGENT", index=1, target="enemies"),
                           public_card("DEFEND_REGENT", index=2)], character="REGENT")
    first = deepcopy(before["combat"]["enemies"][0])
    first.update(i=0, hp="1/100", instance_id="ordinary-first")
    second = deepcopy(first)
    second.update(i=1, hp="100/100", instance_id="ordinary-second")
    before["combat"]["enemies"] = [first, second]
    before["combat"]["draw"] = [{**public_card("ANGER", cost=0), "card_ids": ["ANGER"], "card_count": 1}]
    current = open_choice(client, before, {"type": "play_card", "card_index": 0, "target_index": 0})
    survivor = current["combat"]["enemies"][1]
    survivor["i"] = 0
    current["combat"]["enemies"] = [survivor]
    returned = next(row["i"] for row in current["selection"]["cards"] if row["card_id"] == "ANGER")
    result = rollout_prefix(client, current, [{"type": "select_deck_card", "option_index": returned},
                                            {"type": "play_card", "card_index": 0, "target_index": 0}])
    assert len(result["enemies"]) == 1 and result["enemies"][0]["i"] == 0, result
    assert result["enemies"][0]["hp"]["current"] == 94, result
    impostor = deepcopy(current)
    impostor["combat"]["enemies"][0]["instance_id"] = "different-physical-enemy"
    rejected = client.request({"op": "search", "public_state": impostor, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_enemy_roster_mismatch" in str(rejected), rejected


def test_dead_reattach_segment_keeps_its_empty_move_and_becomes_targetable_after_real_revive(client):
    """The native corpse remains in the roster: empty DEAD, then Heal, then 25 HP at the same target slot."""
    strike = public_card("STRIKE_IRONCLAD", target="enemies")
    strike.update(effective_keywords=["Retain"], keyword_state_complete=True)
    state = search_frame([strike])
    state["combat"]["enemies"] = [
        {"i": 0, "enemy_id": "DECIMILLIPEDE_SEGMENT_FRONT", "instance_id": "segment-front",
         "hp": "0/44", "alive": False, "hittable": False, "block": 0, "powers": ["REATTACH_POWER 25"], "intents": []},
        {"i": 1, "enemy_id": "DECIMILLIPEDE_SEGMENT_MIDDLE", "instance_id": "segment-middle",
         "hp": "40/40", "alive": True, "hittable": True, "block": 0, "powers": ["REATTACH_POWER 25"],
         "intents": [{"intent_type": "Attack", "damage": 6, "hits": 1}, {"intent_type": "Buff"}]},
        {"i": 2, "enemy_id": "DECIMILLIPEDE_SEGMENT_BACK", "instance_id": "segment-back",
         "hp": "42/42", "alive": True, "hittable": True, "block": 0, "powers": ["REATTACH_POWER 25"],
         "intents": [{"intent_type": "Attack", "damage": 8, "hits": 1}, {"intent_type": "Debuff"}]}]
    turns = [{"type": "end_turn"}, {"type": "end_turn"}]
    revived = rollout_prefix(client, state, turns)
    front = next(row for row in revived["enemies"] if row["i"] == 0)
    assert front["hp"]["current"] == 25 and front["hittable"] is True, revived
    attacked = rollout_prefix(client, state, turns + [{"type": "play_card", "card_index": 0, "target_index": 0}])
    front = next(row for row in attacked["enemies"] if row["i"] == 0)
    assert 0 < front["hp"]["current"] < 25, attacked


def closed_boost_away_fixture(client, *, old_anonymous_status=False, hive_attack=False, unplayed_echo=False, tracker=None):
    """Collect a controlled actual-core Boost prefix using the real Python receipt path and native log schema."""
    before = search_frame([public_card("BOOST_AWAY", cost=0), public_card("DEFEND_DEFECT", index=1)], character="DEFECT")
    before["combat"]["hand"][0]["instance_id"] = "seen-boost"
    before["combat"]["hand"][1]["instance_id"] = "seen-defend"
    if unplayed_echo:
        before["combat"]["hand"].append({**public_card("ECHO_FORM_CARD", index=2, cost=3),
                                        "instance_id": "seen-unplayed-echo"})
    if hive_attack:
        before["combat"]["hand"].append({**public_card("GO_FOR_THE_EYES", index=2, cost=0, target="enemies"),
                                        "instance_id": "seen-eyes"})
        before["combat"]["enemies"][0]["powers"] = ["PERSONAL_HIVE_POWER 2"]
    before["combat"]["discard"] = [{**knowledge_card("ROCKET_PUNCH", cost=2, identity="seen-rocket")}]
    if old_anonymous_status:
        old = knowledge_card("DAZED", cost=0)
        old["effective_keywords"] = ["Ethereal", "Unplayable"]
        before["combat"]["discard"].append(old)
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    before["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    before["action_revision"] = 0
    opening = {"type": "play_card", "card_index": 0}
    after = visible_successor(before, rollout_prefix(client, before, [opening]))
    after["action_revision"] = 1
    after["combat"]["public_card_play_history"]["plays"] = [{"sequence": 0,
        "card_id": "BOOST_AWAY", "card_type": "Skill", "auto_played": False,
        "first_in_series": True, "this_turn": True, "known_instance_id": "seen-boost"}]
    for pile in ("draw", "discard", "exhaust"):
        for row in after["combat"][pile]:
            if identity := row.pop("instance_id", None):
                row["known_instance_ids"] = [identity]
    completed_public_step(tracker or PublicCombatHistory(), before, after, opening, "actual-boost-prefix")
    recorded = after["public_history"]["combat_cost_origins"][0]["actions"]
    assert len(recorded) == 1 and {key: value for key, value in recorded[0].items() if key != "before"} == opening
    assert recorded[0]["before"]["combat"] == before["combat"]
    return after


def test_boost_away_recovers_rocket_free_until_played_without_making_it_permanently_free(client):
    """The original status-generation reaction survives an unrelated action and turn, then expires on play."""
    current = closed_boost_away_fixture(client)
    after = rollout_prefix(client, current, [{"type": "play_card", "card_index": 0}, {"type": "end_turn"}])
    rocket = next(row for row in after["hand"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and rocket["cost_observation_only"] is False, after
    played = rollout_prefix(client, current, [{"type": "end_turn"},
        {"type": "play_card", "card_id": "ROCKET_PUNCH", "target_index": 0}])
    rocket = next(row for row in played["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2 and rocket["cost_observation_only"] is False, played


def test_completed_same_rocket_identity_expires_its_witnessed_modifier_and_unknown_identity_does_not_guess(client):
    """A later real-schema completed play of the same seen card proves expiry; a null same-face identity does not."""
    current = closed_boost_away_fixture(client)
    snapshot = rollout_prefix(client, current, [{"type": "end_turn"},
        {"type": "play_card", "card_id": "ROCKET_PUNCH", "target_index": 0}])
    after = visible_successor(current, snapshot)
    after["action_revision"] += 2
    after["public_history"] = deepcopy(current["public_history"])
    plays = after["combat"]["public_card_play_history"]["plays"]
    plays[0]["this_turn"] = False
    plays.append({"sequence": 1, "card_id": "ROCKET_PUNCH", "card_type": "Attack",
        "auto_played": False, "first_in_series": True, "this_turn": True, "known_instance_id": "seen-rocket"})
    for pile in ("draw", "discard", "exhaust"):
        for row in after["combat"][pile]:
            if identity := row.pop("instance_id", None):
                row["known_instance_ids"] = [identity]
    inspected = client.request({"op": "inspect", "public_state": after})
    assert inspected.get("ok"), inspected
    rocket = next(row for row in inspected["state"]["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2 and rocket["cost_observation_only"] is False, inspected
    for unknown_first in (False, True):
        variant = deepcopy(after)
        log = variant["combat"]["public_card_play_history"]["plays"]
        extra = {**log[-1], "known_instance_id": None}
        log[1:] = [extra, log[-1]] if unknown_first else [log[-1], extra]
        for sequence, row in enumerate(log):
            row["sequence"] = sequence
        expired = client.request({"op": "inspect", "public_state": variant})
        assert expired.get("ok"), expired
        same = next(row for row in expired["state"]["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
        assert same["cost"] == 2, expired
    plays[-1]["known_instance_id"] = None
    unknown = client.request({"op": "search", "public_state": after, "simulations": 8})
    assert not unknown.get("ok") and "combat_cost_origin_until_played_identity_not_witnessed" in str(unknown), unknown


def test_generated_status_birth_does_not_stamp_an_arbitrary_matching_old_anonymous_card(client):
    """A new Generated=1 status cannot identify itself among an unproved same-face old anonymous member."""
    current = closed_boost_away_fixture(client, old_anonymous_status=True)
    result = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not result.get("ok") and "combat_cost_generated_birth_lineage_ambiguous" in str(result), result


def test_completed_observed_status_increase_preserves_the_whole_proven_birth_cohort(client):
    """Original PersonalHive births extend the Boost cohort only after their own completed public action."""
    tracker = PublicCombatHistory()
    current = closed_boost_away_fixture(client, hive_attack=True, tracker=tracker)
    action = {"type": "play_card", "card_index": 1, "target_index": 0}
    snapshot = rollout_prefix(client, current, [action])
    after = visible_successor(current, snapshot)
    after["action_revision"] += 1
    after["combat"]["public_card_play_history"]["plays"].append({"sequence": 1,
        "card_id": "GO_FOR_THE_EYES", "card_type": "Attack", "auto_played": False,
        "first_in_series": True, "this_turn": True, "known_instance_id": "seen-eyes"})
    for pile in ("draw", "discard", "exhaust"):
        for row in after["combat"][pile]:
            if identity := row.pop("instance_id", None):
                row["known_instance_ids"] = [identity]
    completed_public_step(tracker, current, after, action, "hive-status-completed")
    assert len(after["public_history"]["combat_cost_origins"]) == 2, after["public_history"]
    inspected = client.request({"op": "inspect", "public_state": after})
    assert inspected.get("ok"), inspected
    dazed = [row for pile in inspected["state"]["pile_cards"].values() for row in pile if row["card_id"] == "DAZED"]
    assert len(dazed) == 3 and all("generated_instance_restored_from_completed_public_rule" in row["import_notes"]
                                  for row in dazed), inspected
    missing_last_action = deepcopy(after)
    missing_last_action["public_history"]["combat_cost_origins"].pop()
    rejected = client.request({"op": "search", "public_state": missing_last_action, "simulations": 8})
    assert not rejected.get("ok") and "combat_cost_generated_birth_lineage_ambiguous" in str(rejected), rejected


def test_living_original_empty_move_accepts_an_empty_public_telegraph(client):
    """BattleFriend's real NOTHING_MOVE is empty; a live Nibbit cannot use that as a missing-intent fallback."""
    state = search_frame()
    state["combat"]["enemies"][0].update(enemy_id="BattleFriendV1", hp="75/75", intents=[],
        powers=["BATTLEWORN_DUMMY_TIME_LIMIT_POWER 3"])
    accepted = client.request({"op": "search", "public_state": state,
        "simulations": 8, "max_rollout_depth": 1, "scenario_seed": 41})
    assert accepted.get("ok") and not accepted["support"]["blocking_reasons"], accepted
    state["combat"]["enemies"][0].update(enemy_id="Nibbit", powers=[])
    missing = client.request({"op": "search", "public_state": state, "simulations": 8})
    assert not missing.get("ok") and "public_intent_move_not_matched" in str(missing), missing


def test_native_choice_order_survives_remapping_and_witnessed_picks(client):
    """UI order defines the eligible enumeration while actual earlier clicks retain their physical meaning."""
    before = frame([public_card("STRIKE_IRONCLAD"), public_card("DEFEND_IRONCLAD", index=1),
                    public_card("BASH", index=2, cost=2)])
    for index, row in enumerate(before["combat"]["hand"]):
        row["instance_id"] = f"choice-card-{index}"
    before["combat"]["draw"] = [{**knowledge_card("ANGER", cost=0), "card_count": 2, "line": "ANGER*2 [0]"}]
    before["run"]["potions"] = [{"i": 2, "potion_id": "GAMBLERS_BREW", "occupied": True,
        "usable": True, "target": None, "targets": []}]
    current = open_choice(client, before, {"type": "use_potion", "option_index": 2}, manual=True)
    native_indices = {"STRIKE_IRONCLAD": 9, "DEFEND_IRONCLAD": 3, "BASH": 7}
    for row in current["selection"]["cards"]:
        row["i"] = native_indices[row["card_id"]]
    imported = rollout_prefix(client, current, [])
    assert [row["card_id"] for row in imported["pending_choice"]["options"]] == [
        "DEFEND_IRONCLAD", "BASH", "STRIKE_IRONCLAD"], imported
    for picked in (9, 3):
        previous = deepcopy(current["selection"])
        next(row for row in current["selection"]["cards"] if row["i"] == picked)["selected"] = True
        current["selection"]["selected"] += 1
        current["public_history"]["combat_selection_origin"]["actions"].append({
            "action": {"type": "select_deck_card", "option_index": picked}, "selection": previous})
    resolved = rollout_prefix(client, current, [{"type": "confirm_selection"}])
    assert [row["card_id"] for row in resolved["hand"]] == ["BASH", "ANGER", "ANGER"], resolved
    assert {row["card_id"] for row in resolved["pile_cards"]["discard"]} == {
        "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"}, resolved


def test_complete_witnessed_turn_draws_cross_a_real_refill_before_tyranny_choice(client):
    """A different predeclared observation world fixes the whole six-card draw, including its refill tail."""
    before = search_frame()
    before["combat"]["player"]["powers"] = ["TYRANNY_POWER 1"]
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    draw = [knowledge_card(name, cost=cost, identity=f"draw-{name}") for name, cost in (
        ("TWIN_STRIKE", 1), ("DEFEND_IRONCLAD", 1), ("ANGER", 0), ("BASH", 2))]
    perfect = knowledge_card("STRIKE_IRONCLAD", perfect=True, identity="discard-perfect")
    discard = [perfect] + [knowledge_card(name, identity=f"discard-{name}")
        for name in ("SHRUG_IT_OFF", "IRON_WAVE", "TRUE_GRIT")]
    before["combat"].update(draw=draw, discard=discard)
    attach_draw_knowledge(before, kind="initial_shuffle", discard_bands=[[perfect]])
    opening = {"type": "end_turn"}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    assert len(snapshot["hand"]) == 6 and snapshot["pending_choice"] is not None, snapshot
    current = visible_successor(before, snapshot)
    known = {row["card_ids"][0]: row["known_instance_ids"][0] for row in draw + discard}
    for row in current["combat"]["hand"]:
        row["instance_id"] = known[row["card_id"]]
        row.pop("known_instance_ids", None)
    current.update(screen="CARD_SELECTION", action_revision=1,
        selection={"kind": "hand", "underlying_screen": "COMBAT", "min": 1, "max": 1,
            "selected": 0, "requires_confirmation": False, "confirm": False,
            "cards": [choice_card(row, row["i"]) for row in current["combat"]["hand"]]},
        available_actions=["select_deck_card"])
    # The original HISS grants Strength2, then advances to the literal base12 BUTT move.
    current["combat"]["enemies"][0]["intents"] = [
        {"intent_type": "Attack", "damage": 14, "hits": 1}]
    current["combat"]["action_readiness"] = {"snapshot_stable": True, "can_use_combat_actions": False}
    current["public_history"] = {"combat_selection_origin": {"schema_version": 1,
        "before": before, "actions": [opening], "action_id": "controlled-refill-end-turn"}}
    attach_draw_knowledge(current)
    restored = rollout_prefix(client, current, [])
    assert [row["card_id"] for row in restored["hand"]] == [row["card_id"] for row in snapshot["hand"]], restored
    assert restored["hand"][4]["card_id"] == "STRIKE_IRONCLAD", restored
    without_complete_draw_log = deepcopy(current)
    without_complete_draw_log["combat"]["public_card_play_history"]["complete"] = False
    rejected = client.request({"op": "search", "public_state": without_complete_draw_log, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_refill_draw_chronology_not_completely_observed" in str(rejected), rejected


def test_repeated_boost_replaces_an_expired_witness_before_validating_the_new_quote(client):
    """A later completed Boost can set the same Rocket free again; its resulting quote must still match."""
    tracker = PublicCombatHistory()
    current = closed_boost_away_fixture(client, tracker=tracker)
    played = rollout_prefix(client, current, [{"type": "end_turn"},
        {"type": "play_card", "card_id": "ROCKET_PUNCH", "target_index": 0}])
    before = visible_successor(current, played)
    before["action_revision"] += 2
    plays = before["combat"]["public_card_play_history"]["plays"]
    plays[0]["this_turn"] = False
    plays.append({"sequence": 1, "card_id": "ROCKET_PUNCH", "card_type": "Attack", "auto_played": False,
        "first_in_series": True, "this_turn": True, "known_instance_id": "seen-rocket"})
    before["combat"]["enemies"][0]["intents"] = [{"intent_type": "Attack", "damage": 14, "hits": 1}]
    for row in before["combat"]["hand"]:
        if len(row.get("known_instance_ids", [])) == 1:
            row["instance_id"] = row.pop("known_instance_ids")[0]
    rocket = next(row for row in before["combat"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["energy_cost"] == 2
    boost = next(row["i"] for row in before["combat"]["hand"] if row["card_id"] == "BOOST_AWAY")
    opening = {"type": "play_card", "card_index": boost}
    after = visible_successor(before, rollout_prefix(client, before, [opening]))
    after["action_revision"] += 1
    after["combat"]["public_card_play_history"]["plays"].append({"sequence": 2,
        "card_id": "BOOST_AWAY", "card_type": "Skill", "auto_played": False,
        "first_in_series": True, "this_turn": True, "known_instance_id": "seen-boost"})
    for pile in ("draw", "discard", "exhaust"):
        for row in after["combat"][pile]:
            if identity := row.pop("instance_id", None):
                row["known_instance_ids"] = [identity]
    completed_public_step(tracker, before, after, opening, "repeated-boost-after-expiry")
    assert len(after["public_history"]["combat_cost_origins"]) == 2
    result = client.request({"op": "inspect", "public_state": after})
    assert result.get("ok"), result
    rocket = next(row for row in result["state"]["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and rocket["cost_observation_only"] is False, result
    wrong = deepcopy(after)
    next(row for row in wrong["combat"]["discard"] if row["card_id"] == "ROCKET_PUNCH")["energy_cost"] = 2
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8})
    assert not rejected.get("ok") and "combat_cost_origin_current_quote_contradicts_rule" in str(rejected), rejected


def test_complete_prepared_draws_cross_refill_while_the_original_source_remains_in_play(client):
    """Prepared+ draws two before its discard choice and finishes neither its play nor AfterImage early."""
    before = search_frame([public_card("PREPARED", cost=0, upgraded=True),
                           public_card("DEFEND_SILENT", index=1)], character="SILENT")
    for index, row in enumerate(before["combat"]["hand"]):
        row.update(instance_id=f"prepared-old-{index}", effective_keywords=[], keyword_state_complete=True)
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    before["combat"]["player"]["powers"] = ["AFTERIMAGE_POWER 2"]
    draw = knowledge_card("ANGER", cost=0, identity="prepared-anger")
    discard = [knowledge_card("STRIKE_SILENT", identity="prepared-strike"),
               knowledge_card("BASH", cost=2, identity="prepared-bash")]
    before["combat"].update(draw=[draw], discard=discard)
    opening = {"type": "play_card", "card_index": 0}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    assert snapshot["pending_play"] is True and snapshot["player"]["block"] == 0, snapshot
    current = open_choice(client, before, opening, snapshot=snapshot)
    known = {row["card_ids"][0]: row["known_instance_ids"][0] for row in [draw] + discard}
    for row in current["combat"]["hand"]:
        if row["card_id"] in known:
            row["instance_id"] = known[row["card_id"]]
            row.pop("known_instance_ids", None)
    current["combat"]["play"] = [{**deepcopy(before["combat"]["hand"][0]), "i": 0, "source_pile": "Play"}]
    current["selection"]["cards"] = [choice_card(row, row["i"]) for row in current["combat"]["hand"]]
    for scenario in (41, 1771, 104770):
        restored = rollout_prefix(client, current, [], seed=scenario)
        assert [row["card_id"] for row in restored["hand"]] == [row["card_id"] for row in snapshot["hand"]], restored
        assert restored["pending_play"] is True and restored["player"]["cards_played_this_turn"] == 0, restored
        assert restored["player"]["block"] == 0, restored
    current["combat"]["public_card_play_history"]["complete"] = False
    rejected = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_refill_draw_chronology_not_completely_observed" in str(rejected), rejected


def test_newly_seen_duplicate_draws_bind_only_within_the_same_anonymous_unordered_class(client):
    """Two identical unreferenced Defends share a representative; differing public bands remain distinct."""
    before = search_frame()
    before["combat"]["player"]["powers"] = ["TYRANNY_POWER 1"]
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    anonymous = {**knowledge_card("DEFEND_IRONCLAD"), "card_count": 2, "line": "DEFEND_IRONCLAD*2 [1]"}
    perfect = knowledge_card("STRIKE_IRONCLAD", perfect=True, identity="duplicate-perfect")
    others = [knowledge_card(name, cost=cost, identity=f"duplicate-{name}")
        for name, cost in (("ANGER", 0), ("BASH", 2), ("TWIN_STRIKE", 1))]
    before["combat"]["draw"] = [anonymous, perfect] + others
    attach_draw_knowledge(before, bands=[before["combat"]["draw"]], kind="initial_shuffle")
    opening = {"type": "end_turn"}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    current = open_choice(client, before, opening, snapshot=snapshot)
    known = {row["card_ids"][0]: row["known_instance_ids"][0] for row in [perfect] + others}
    for index, row in enumerate(current["combat"]["hand"]):
        row["instance_id"] = known.get(row["card_id"], f"newly-seen-defend-{index}")
        row.pop("known_instance_ids", None)
    current["selection"]["cards"] = [choice_card(row, row["i"]) for row in current["combat"]["hand"]]
    current["combat"]["enemies"][0]["intents"] = [{"intent_type": "Attack", "damage": 14, "hits": 1}]
    attach_draw_knowledge(current)
    restored = rollout_prefix(client, current, [])
    assert [row["card_id"] for row in restored["hand"]] == [row["card_id"] for row in snapshot["hand"]], restored
    different_roles = deepcopy(current)
    origin = different_roles["public_history"]["combat_selection_origin"]["before"]
    single = {**anonymous, "card_count": 1, "line": "DEFEND_IRONCLAD*1 [1]"}
    attach_draw_knowledge(origin, bands=[[single], [single], [perfect] + others])
    rejected = client.request({"op": "search", "public_state": different_roles, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_draw_identity_not_uniquely_recoverable" in str(rejected), rejected


def test_unexhausted_draw_does_not_mix_identical_anonymous_discard_candidates(client):
    """A visibly certified top Defend is unique in Draw even when identical anonymous copies sit in Discard."""
    before = search_frame([public_card("PREPARED", cost=0, upgraded=True),
                           public_card("STRIKE_SILENT", index=1)], character="SILENT")
    for index, row in enumerate(before["combat"]["hand"]):
        row.update(instance_id=f"draw-only-old-{index}", effective_keywords=[], keyword_state_complete=True)
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    defend = knowledge_card("DEFEND_SILENT")
    others = [knowledge_card("ANGER", cost=0, identity="draw-only-anger"), knowledge_card("BASH", cost=2, identity="draw-only-bash")]
    before["combat"].update(draw=[defend] + others, discard=[{**defend, "card_count": 2, "line": "DEFEND_SILENT*2 [1]"}])
    attach_draw_knowledge(before, bands=[[defend], others])
    opening = {"type": "play_card", "card_index": 0}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    current = open_choice(client, before, opening, snapshot=snapshot)
    for row in current["combat"]["hand"]:
        if row["card_id"] == "DEFEND_SILENT":
            row["instance_id"] = "new-draw-only-defend"
        elif row.get("known_instance_ids"):
            row["instance_id"] = row["known_instance_ids"][0]
    current["combat"]["play"] = [{**deepcopy(before["combat"]["hand"][0]), "source_pile": "Play"}]
    current["selection"]["cards"] = [choice_card(row, row["i"]) for row in current["combat"]["hand"]]
    attach_draw_knowledge(current)
    restored = rollout_prefix(client, current, [])
    assert restored["hand"][1]["card_id"] == "DEFEND_SILENT", restored
    assert len(restored["pile_cards"]["discard"]) == 2, restored


def test_observed_hatch_hp_is_conditioned_at_the_original_sampler_and_checks_its_support(client):
    """Past hatch rolls may differ from the replay seed; a max-HP outside the actual version's support cannot pass."""
    before = search_frame()
    before["combat"]["player"]["powers"] = ["TYRANNY_POWER 1"]
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    before["combat"]["draw"] = [knowledge_card(name, cost=cost, identity=f"hatch-{name}") for name, cost in (
        ("ANGER", 0), ("BASH", 2), ("DEFEND_IRONCLAD", 1), ("STRIKE_IRONCLAD", 1), ("TWIN_STRIKE", 1), ("SHRUG_IT_OFF", 1))]
    before["combat"]["enemies"] = [{"i": index, "enemy_id": "ToughEgg", "instance_id": f"hatch-egg-{index}",
        "hp": f"{16+index}/{16+index}", "block": 0, "alive": True, "hittable": True,
        "powers": ["HATCH_POWER 1", "MINION_POWER 1"], "intents": [{"intent_type": "Summon"}]}
        for index in range(3)]
    opening = {"type": "end_turn"}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    current = open_choice(client, before, opening, snapshot=snapshot)
    known = {row["card_ids"][0]: row["known_instance_ids"][0] for row in before["combat"]["draw"]}
    for row in current["combat"]["hand"]:
        row["instance_id"] = known[row["card_id"]]
    for row, actual in zip(current["combat"]["enemies"], snapshot["enemies"]):
        row.update(power_details=deepcopy(actual["power_details"]), intents=[{"intent_type": "Attack", "damage": 4, "hits": 1}])
    current["selection"]["cards"] = [choice_card(row, row["i"]) for row in current["combat"]["hand"]]
    restored = rollout_prefix(client, current, [])
    assert [row["hp"] for row in restored["enemies"]] == [row["hp"] for row in snapshot["enemies"]], restored
    impossible = deepcopy(current)
    impossible["combat"]["enemies"][0]["hp"] = "22/22"
    rejected = client.request({"op": "search", "public_state": impossible, "simulations": 8})
    assert not rejected.get("ok") and "observed_hatch_hp" in str(rejected) and "support" in str(rejected), rejected


@pytest.mark.parametrize("old_orb,enemy_count", [
    ("Lightning", 1), ("Glass", 2), ("Frost", 2), ("Plasma", 2), ("Lightning", 2)])
def test_complete_visible_random_orb_suffix_preserves_original_overflow_effects(client, old_orb, enemy_count):
    """Visible new kinds and original overflow targets are independently proved against the full successor."""
    before = search_frame([public_card("BOOST_AWAY", cost=0), public_card("DEFEND_DEFECT", index=1)], character="DEFECT")
    before["combat"]["hand"][0]["instance_id"] = "orb-boost"
    before["combat"]["hand"][1]["instance_id"] = "orb-defend"
    before["combat"]["discard"] = [knowledge_card("ROCKET_PUNCH", cost=2, identity="orb-rocket")]
    if enemy_count == 2:
        second = deepcopy(before["combat"]["enemies"][0])
        second.update(i=1, instance_id="fixture-enemy-2")
        before["combat"]["enemies"].append(second)
    passive, evoke = {"Lightning": (4, 9), "Glass": (5, 10), "Frost": (3, 6), "Plasma": (1, 2)}[old_orb]
    before["combat"]["player"].update(powers=["FOCUS_POWER 1", "ECHO_FORM_POWER 1", "TRASH_TO_TREASURE_POWER 1"],
        orb_capacity=2, orb_details=[{"orb_id": old_orb, "passive_value": passive, "evoke_value": evoke}] * 2)
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    before["combat"]["action_readiness"] = dict.fromkeys(("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    opening = {"type": "play_card", "card_index": 0}
    after = visible_successor(before, rollout_prefix(client, before, [opening], seed=101))
    # Both new orbs remain visible. Their legal kinds cannot change preceding old-orb evocations.
    after["combat"]["player"]["orb_details"] = [{"orb_id": "Plasma", "passive_value": 1, "evoke_value": 2},
                                                 {"orb_id": "Glass", "passive_value": 5, "evoke_value": 10}]
    after["combat"]["public_card_play_history"]["plays"] = [{"sequence": index, "card_id": "BOOST_AWAY",
        "card_type": "Skill", "auto_played": False, "first_in_series": index == 0,
        "this_turn": True, "known_instance_id": "orb-boost"} for index in range(2)]
    if old_orb == "Lightning" and enemy_count == 2:
        # Both old Lightning rolls selecting the first enemy has positive original support, regardless of seed.
        after["combat"]["enemies"][0]["hp"] = {"current": 82, "max": 100}
        after["combat"]["enemies"][1]["hp"] = {"current": 100, "max": 100}
    after["action_revision"] += 1
    for pile in ("draw", "discard", "exhaust"):
        for row in after["combat"][pile]:
            if identity := row.pop("instance_id", None):
                row["known_instance_ids"] = [identity]
    completed_public_step(PublicCombatHistory(), before, after, opening, "controlled-visible-orb-outcomes")
    result = client.request({"op": "inspect", "public_state": after})
    assert result.get("ok"), result
    assert result["state"]["player"]["orb_details"] == after["combat"]["player"]["orb_details"], result
    expected_hp = [82] + [100] * (enemy_count - 1) if old_orb == "Lightning" else [80 if old_orb == "Glass" else 100] * enemy_count
    assert [row["hp"]["current"] for row in result["state"]["enemies"]] == expected_hp, result
    assert result["state"]["player"]["block"] == (24 if old_orb == "Frost" else 12), result
    if old_orb == "Plasma":
        assert result["state"]["player"]["energy"] == before["combat"]["player"]["energy"] + 4, result
    hidden_old_rolls = deepcopy(after)
    origin = hidden_old_rolls["public_history"]["combat_cost_origins"][0]
    for state in (origin["before"], origin["after"], hidden_old_rolls):
        state["combat"]["player"]["powers"] = ["FOCUS_POWER 1", "ECHO_FORM_POWER 1", "TRASH_TO_TREASURE_POWER 2"]
    rejected = client.request({"op": "search", "public_state": hidden_old_rolls, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_random_orb_sequence_not_completely_visible" in str(rejected), rejected
    impossible = deepcopy(after)
    for state in (impossible, impossible["public_history"]["combat_cost_origins"][0]["after"]):
        state["combat"]["enemies"][0]["hp"]["current"] -= 1
    rejected = client.request({"op": "search", "public_state": impossible, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_enemy_state_mismatch" in str(rejected), rejected


@pytest.mark.parametrize("hp,status,strength,new_hp,new_strength", [
    ("60/160", "Normal", 0, 59, 3), ("59/80", "Active", 3, 58, 0), ("38/80", "Active", 4, 37, 4)])
def test_public_red_skull_status_preserves_max_hp_only_history_and_brand_order(client, hp, status, strength, new_hp, new_strength):
    """Native-proven Status, rather than the current HP ratio, controls the original next HP-change callback."""
    state = search_frame([public_card("BRAND", cost=0), public_card("DEFEND_IRONCLAD", index=1),
                          public_card("STRIKE_IRONCLAD", index=2)])
    state["combat"]["player"].update(hp=hp, powers=[f"STRENGTH_POWER {strength}"] if strength else [])
    state["run"].update(relic_ids=["RED_SKULL"], relic_stacks=[None], relic_statuses=[status], relic_is_melted=[False])
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0}])
    actual = next((int(power.split()[1]) for power in after["player"]["powers"] if power.startswith("STRENGTH_POWER ")), 0)
    assert after["player"]["hp"]["current"] == new_hp and actual == new_strength, after
    assert after["pending_play"] is True, after


def test_legacy_red_skull_uses_a_unique_complete_prefix_hypothesis_without_guessing_the_hp_ratio(client):
    """Brand distinguishes the two flags; an action with no HP effect leaves the old schema honestly ambiguous."""
    before = search_frame([public_card("BRAND", cost=0), public_card("DEFEND_IRONCLAD", index=1),
                           public_card("STRIKE_IRONCLAD", index=2)])
    before["combat"]["player"].update(hp="38/80", powers=["STRENGTH_POWER 4"])
    before["run"].update(relic_ids=["RED_SKULL"], relic_stacks=[None], relic_statuses=["Active"], relic_is_melted=[False])
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    current["run"].pop("relic_statuses")
    current["public_history"]["combat_selection_origin"]["before"]["run"].pop("relic_statuses")
    restored = rollout_prefix(client, current, [])
    assert "STRENGTH_POWER 4" in restored["player"]["powers"], restored
    before["combat"]["hand"][0] = public_card("SURVIVOR")
    before["run"]["character_id"] = "SILENT"
    before["combat"]["players"][0]["character_id"] = "SILENT"
    ambiguous = open_choice(client, before, {"type": "play_card", "card_index": 0})
    ambiguous["run"].pop("relic_statuses")
    ambiguous["public_history"]["combat_selection_origin"]["before"]["run"].pop("relic_statuses")
    rejected = client.request({"op": "search", "public_state": ambiguous, "simulations": 8})
    assert not rejected.get("ok") and "red_skull_activation_not_uniquely_observed" in str(rejected), rejected


def test_cost_witness_uses_the_same_full_upstream_support_for_an_unplayed_echo_card(client):
    """An unrelated known Echo card is not an unknown mechanic; an actually unexplained quote still is."""
    current = closed_boost_away_fixture(client, unplayed_echo=True)
    inspected = client.request({"op": "inspect", "public_state": current})
    assert inspected.get("ok"), inspected
    rocket = next(row for row in inspected["state"]["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and rocket["cost_observation_only"] is False, inspected
    played = rollout_prefix(client, current, [{"type": "play_card", "card_id": "ECHO_FORM_CARD"}])
    assert "ECHO_FORM_POWER 1" in played["player"]["powers"], played
    unexplained = deepcopy(current)
    origin = unexplained["public_history"]["combat_cost_origins"][0]
    for state in (origin["before"], origin["after"], unexplained):
        next(row for row in state["combat"]["hand"] if row["card_id"] == "ECHO_FORM_CARD")["energy_cost"] = 0
    rejected = client.request({"op": "search", "public_state": unexplained, "simulations": 8})
    assert not rejected.get("ok") and "combat_cost_origin_hand_mechanics_not_reconstructible" in str(rejected), rejected


def test_photon_cut_putback_uses_complete_observed_draws_across_refill(client):
    """Original PhotonCut+ damage and two draws reach PutBack without relaxing its complete hand chronology."""
    before = search_frame([public_card("PHOTON_CUT", target="enemies", upgraded=True),
                           public_card("DEFEND_REGENT", index=1), public_card("STRIKE_REGENT", index=2)], character="REGENT")
    for index, row in enumerate(before["combat"]["hand"]):
        row.update(instance_id=f"photon-refill-old-{index}", effective_keywords=[], keyword_state_complete=True)
    before["combat"]["play"] = []
    before["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "source": "controlled_public_completed_actions", "plays": []}
    discard = [knowledge_card(name, cost=cost, identity=f"photon-refill-{name}") for name, cost in (
        ("ANGER", 0), ("BASH", 2), ("SHRUG_IT_OFF", 1), ("TWIN_STRIKE", 1))]
    before["combat"]["discard"] = discard
    opening = {"type": "play_card", "card_index": 0, "target_index": 0}
    snapshot = rollout_prefix(client, before, [opening], seed=101)
    assert snapshot["pending_choice"]["kind"] == "PutBack" and snapshot["pending_play"] is True, snapshot
    current = open_choice(client, before, opening, snapshot=snapshot)
    known = {row["card_ids"][0]: row["known_instance_ids"][0] for row in discard}
    for row in current["combat"]["hand"]:
        if row["card_id"] in known:
            row["instance_id"] = known[row["card_id"]]
            row.pop("known_instance_ids", None)
    current["combat"]["play"] = [{**deepcopy(before["combat"]["hand"][0]), "i": 0, "source_pile": "Play"}]
    current["selection"]["cards"] = [choice_card(row, row["i"]) for row in current["combat"]["hand"]]
    restored = rollout_prefix(client, current, [])
    assert [row["card_id"] for row in restored["hand"]] == [row["card_id"] for row in snapshot["hand"]], restored
    assert restored["enemies"][0]["hp"]["current"] == 87 and restored["pending_play"] is True, restored
    missing = deepcopy(current)
    missing["combat"]["public_card_play_history"]["complete"] = False
    rejected = client.request({"op": "search", "public_state": missing, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_refill_draw_chronology_not_completely_observed" in str(rejected), rejected
    reordered = deepcopy(current)
    reordered["combat"]["hand"][:2] = list(reversed(reordered["combat"]["hand"][:2]))
    for index, row in enumerate(reordered["combat"]["hand"]):
        row["i"] = index
    reordered["selection"]["cards"] = [choice_card(row, row["i"]) for row in reordered["combat"]["hand"]]
    rejected = client.request({"op": "search", "public_state": reordered, "simulations": 8})
    assert not rejected.get("ok") and "witnessed_draw_old_hand_prefix_not_observed" in str(rejected), rejected


@pytest.mark.parametrize("card,cost,key,amount,damage", [
    ("HELIX_DRILL", 0, "energy_spent_this_turn", 3, 9),
    ("SPITE", 0, "hp_loss_events_this_turn", 1, 10),
    ("TEAR_ASUNDER", 2, "hp_loss_events_this_combat", 2, 15),
    ("DEATH_MARCH", 1, "cards_drawn_by_effect_this_turn", 2, 16),
    ("MEMENTO_MORI", 1, "cards_discarded_this_turn", 2, 17),
    ("MURDER", 3, "cards_drawn_this_combat", 7, 8),
])
def test_history_aggregates_drive_original_damage_handlers(client, card, cost, key, amount, damage):
    """Real rule consumers must use historical quantities instead of a silent fresh-root zero."""
    before = history_frame([public_card(card, cost=cost, target="enemies")], **{key: amount})
    after = rollout_prefix(client, before, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert after["enemies"][0]["hp"]["current"] == 100 - damage, after
    assert after["history_statistics"][key] == amount, after


@pytest.mark.parametrize("card,key,value,block,energy", [
    ("DEATHS_DOOR", "doom_applied_this_turn", True, 18, 2),
    ("EVIL_EYE", "cards_exhausted_this_turn", 1, 16, 2),
    ("FORGOTTEN_RITUAL", "cards_exhausted_this_turn", 1, 0, 5),
])
def test_history_predicates_restore_block_and_resource_effects(client, card, key, value, block, energy):
    """Exhaust and Doom event predicates survive a fresh import even after their visible source has disappeared."""
    state = history_frame([public_card(card)], **{key: value})
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0}])
    assert after["player"]["block"] == block and after["player"]["energy"] == energy, after


def test_missing_history_is_distinct_from_zero_and_unrelated_legacy_actions_remain_usable(client):
    """A missing Helix tally cannot mean zero; missing unrelated valuation inputs are explicit partial support."""
    unknown = history_frame([public_card("HELIX_DRILL", cost=0, target="enemies")], energy_spent_this_turn=None)
    rejected = client.request({"op": "search", "public_state": unknown, "simulations": 8})
    assert not rejected.get("ok") and "public_history_statistic_not_observed:energy_spent_this_turn" in str(rejected), rejected
    zero = deepcopy(unknown)
    zero["combat"]["energy_spent_this_turn"] = 0
    after = rollout_prefix(client, zero, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert after["enemies"][0]["hp"]["current"] == 100, after
    legacy = rollout_prefix(client, search_frame([public_card("DEFEND_IRONCLAD")]),
                            [{"type": "play_card", "card_index": 0}])
    assert legacy["player"]["block"] == 5, legacy
    assert legacy["history_statistics"]["energy_wasted_this_combat"] is None, legacy
    assert "unspent_energy_at_turn_end_sum" in legacy["history_support"]["missing_value_features"], legacy


def test_finished_shivs_include_autoplay_and_replay_without_reusing_the_first_shiv_bonus(client):
    """Phantom Blades counts every completed Shiv, independent of the power's acquisition time."""
    plays = [finished_play(0, "SHIV", auto=True), finished_play(1, "SHIV", first=False)]
    state = history_frame([public_card("SHIV", cost=0, target="enemies")], character="SILENT", plays=plays)
    state["combat"]["player"]["powers"] = ["PHANTOM_BLADES_POWER 9"]
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert after["enemies"][0]["hp"]["current"] == 96, after
    assert after["history_statistics"]["shivs_played_this_turn"] == 3, after


@pytest.mark.parametrize("value,spent,returns", [(0, 0, False), (1, 0, True)])
def test_feral_uses_finished_resource_value_instead_of_energy_paid(client, value, spent, returns):
    """The original Feral budget reads EnergyValue, which is a separate public resource property."""
    state = history_frame([public_card("CLAW", cost=0, target="enemies")], character="DEFECT",
        plays=[finished_play(0, "STRIKE_DEFECT", value=value, spent=spent, auto=True)])
    state["combat"]["player"]["powers"] = ["FERAL_POWER 1"]
    state["combat"]["player"]["power_details"] = [{"index": 0, "power_id": "FERAL_POWER",
        "amount": 1, "display_amount": int(returns)}]
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert bool(after["hand"]) is returns, after
    assert after["history_statistics"]["zero_cost_attacks_played_this_turn"] == (2 if value == 0 else 1), after


def test_ethereal_finished_history_is_combat_wide_and_not_a_current_keyword_guess(client):
    """Pull From Below includes old-turn Ethereal plays and auto/replays using their finish-time flag."""
    state = history_frame([public_card("PULL_FROM_BELOW", target="enemies")], character="NECROBINDER", plays=[
        finished_play(0, "DEFEND_NECROBINDER", kind="Skill", current=False, ethereal=True),
        finished_play(1, "STRIKE_NECROBINDER", auto=True, ethereal=True),
        finished_play(2, "STRIKE_NECROBINDER", first=False, ethereal=False)])
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert after["enemies"][0]["hp"]["current"] == 90, after
    assert after["history_statistics"]["ethereal_cards_played_this_combat"] == 2, after
    state["combat"]["public_card_play_history"]["plays"][1]["was_ethereal"] = None
    rejected = client.request({"op": "search", "public_state": state, "simulations": 8})
    assert not rejected.get("ok") and "public_history_statistic_not_observed:ethereal_cards_played_this_combat" in str(rejected), rejected


def test_fetch_history_binds_the_played_physical_copy_and_rejects_an_unknown_copy(client):
    """A completed Fetch suppresses its own next draw without suppressing an unplayed identical card."""
    state = history_frame([public_card("FETCH", cost=0, target="enemies"),
        public_card("FETCH", index=1, cost=0, target="enemies")], character="NECROBINDER",
        plays=[finished_play(0, "FETCH", instance="fetch-a")])
    for row, identity in zip(state["combat"]["hand"], ("fetch-a", "fetch-b")):
        row["instance_id"] = identity
    state["combat"]["player"]["pet_details"] = [{"pet_id": "OSTY", "current_hp": 10, "max_hp": 10,
        "block": 0, "power_details": []}]
    state["combat"]["draw"] = [knowledge_card("DEFEND_NECROBINDER", identity=f"fetch-draw-{index}") for index in range(2)]
    first = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert len(first["hand"]) == 1 and first["piles"]["draw"] == 2, first
    both = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0},
        {"type": "play_card", "card_index": 1, "target_index": 0}])
    assert len(both["hand"]) == 1 and both["piles"]["draw"] == 1, both
    state["combat"]["public_card_play_history"]["plays"][0]["known_instance_id"] = None
    rejected = client.request({"op": "search", "public_state": state, "simulations": 8})
    assert not rejected.get("ok") and "public_history_statistic_not_observed:fetch_played_this_turn" in str(rejected), rejected


def test_target_histories_do_not_transfer_hits_between_enemies(client):
    """Gang Up reads the current target's allied hits, not total attacks or a renumbered native slot."""
    state = history_frame([public_card("GANG_UP", target="enemies")])
    other = deepcopy(state["combat"]["enemies"][0])
    other.update(i=1, instance_id="fixture-enemy-2")
    state["combat"]["enemies"].append(other)
    state["combat"]["hand"][0]["targets"] = [0, 1]
    state["combat"]["ally_powered_hits_by_target_this_turn"] = [{"instance_id": "fixture-enemy-2", "hits": 3}]
    state["combat"]["powered_hits_by_target_this_turn"] = [
        {"instance_id": "departed-nibbit", "hits": 4}, {"instance_id": "fixture-enemy-2", "hits": 2}]
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert [row["hp"]["current"] for row in after["enemies"]] == [95, 100], after
    assert after["history_statistics"]["powered_hits_this_turn"] == 7, after
    assert all(row["instance_id"] != "departed-nibbit" for row in after["history_statistics"]["powered_hits_by_target_this_turn"]), after
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 1}])
    assert [row["hp"]["current"] for row in after["enemies"]] == [100, 80], after


def test_original_photon_defeat_keeps_complete_departed_target_evidence(client):
    """A killed receiver remains a strict historical identity without occupying a current target slot."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-photon-dead-hit-v18.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok") and result["best"]["actions"][0]["type"] == "select_deck_card", result
    restored = rollout_prefix(client, current, [])
    assert restored["history_statistics"]["powered_hits_this_turn"] == 1, restored
    assert restored["history_statistics"]["powered_hits_by_target_this_turn"] == [], restored
    assert [row["hp"]["current"] for row in restored["enemies"]] == [33, 15], restored
    renamed = deepcopy(current)
    renamed["combat"]["powered_hits_by_target_this_turn"][0]["instance_id"] = "a-different-departed-creature"
    rejected = client.request({"op": "search", "public_state": renamed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_history_mismatch:powered_hits_by_target_this_turn" in str(rejected), rejected
    changed = deepcopy(current)
    changed["combat"]["ally_powered_hits_by_target_this_turn"] = [{"instance_id": "visible-enemy-2", "hits": 1}]
    rejected = client.request({"op": "search", "public_state": changed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_history_mismatch:ally_powered_hits_by_target_this_turn" in str(rejected), rejected


def test_original_random_target_turn_restores_only_rule_supported_enemy_consequences(client):
    """Past random orb targets are constrained by the full public successor, not by the simulator's seed."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-random-target-turn-v22.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    for seed in (0, 41, 719):
        restored = rollout_prefix(client, current, [], seed=seed)
        assert [row["hp"]["current"] for row in restored["enemies"]] == [194, 155], restored
        rocket = next(row for row in restored["hand"] if row["card_id"] == "ROCKET_PUNCH")
        assert rocket["cost"] == 0 and not rocket["cost_observation_only"], restored
    impossible = deepcopy(current)
    for state in (impossible, impossible["public_history"]["combat_cost_origins"][-1]["after"]):
        state["combat"]["enemies"][0]["hp"] = "193/209"
    rejected = client.request({"op": "search", "public_state": impossible, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_enemy_state_mismatch" in str(rejected), rejected


def test_original_homogeneous_birth_chain_replays_after_rocket_identity_binds(client):
    """The collector's next original action remains a continuation while homogeneous births lack public IDs."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-homogeneous-birth-continuation-v23.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    rocket = next(row for row in restored["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2 and not rocket["cost_observation_only"], restored
    assert [row["card_id"] for row in restored["hand"]][-2:] == ["DEFEND_DEFECT", "WHITE_NOISE"], restored
    dazed = [row for rows in restored["pile_cards"].values() for row in rows if row["card_id"] == "DAZED"]
    assert dazed and all(row["generated_in_combat"] is True for row in dazed), restored
    wrong = deepcopy(current)
    for state in (wrong, wrong["public_history"]["combat_cost_origins"][-1]["after"]):
        state["combat"]["enemies"][0]["hp"] = "178/209"
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_enemy_state_mismatch" in str(rejected), rejected
    gap = deepcopy(current)
    gap["public_history"]["combat_cost_origins"][-1]["before"]["action_revision"] -= 1
    rejected = client.request({"op": "search", "public_state": gap, "simulations": 8})
    assert not rejected.get("ok"), rejected
    changed = deepcopy(current)
    before = changed["public_history"]["combat_cost_origins"][-1]["before"]
    next(row for row in before["combat"]["hand"] if row["card_id"] == "ROCKET_PUNCH")["instance_id"] = "unproved-rocket"
    rejected = client.request({"op": "search", "public_state": changed, "simulations": 8})
    assert not rejected.get("ok"), rejected


def test_completed_witness_uses_full_original_automatic_card_continuations(client):
    """A completed original Echo Form play must reach its proved Power without the legacy prefix-only veto."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-completed-automatic-path-v24.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    assert "ECHO_FORM_POWER 1" in restored["player"]["powers"], restored
    treasure = next(row for row in restored["hand"] if row["card_id"] == "TRASH_TO_TREASURE")
    assert treasure["cost"] == 0 and treasure["generated_in_combat"] is True, restored
    wrong = deepcopy(current)
    for state in (wrong, wrong["public_history"]["combat_cost_origins"][-1]["after"]):
        for row in state["combat"]["player"]["power_details"]:
            if row["power_id"] == "ECHO_FORM_POWER":
                row.update(amount=2, display_amount=2)
        state["combat"]["player"]["powers"] = ["ECHO_FORM_POWER 2" if value == "ECHO_FORM_POWER 1" else value
                                                   for value in state["combat"]["player"]["powers"]]
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_player_power_mismatch" in str(rejected), rejected
    unexplained = deepcopy(current)
    before = unexplained["public_history"]["combat_cost_origins"][-1]["before"]
    next(row for row in before["combat"]["hand"] if row["card_id"] == "ECHO_FORM_CARD")["energy_cost"] = 2
    rejected = client.request({"op": "search", "public_state": unexplained, "simulations": 8})
    assert not rejected.get("ok"), rejected


def test_original_echo_zero_attack_counts_both_completed_iterations(client):
    """The literal V25 frontier must replay both zero-cost Echo iterations and reject a false quote."""
    import hashlib
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-echo-zero-attack-v25.json"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == "d795d75b3b7fc53f3b5f90dfa567db7bdf4585ff7c52c2406bb9dd3fda144a47"
    current = json.loads(fixture.read_text(encoding="utf-8"))["last_decision"]["projection"]["public_state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    assert restored["history_statistics"]["zero_cost_attacks_played_this_turn"] == 2, restored
    assert restored["player"]["hp"]["current"] == 64, restored
    wrong = deepcopy(current)
    for state in (wrong, wrong["public_history"]["combat_cost_origins"][-1]["after"]):
        play = state["combat"]["public_card_play_history"]["plays"][-1]
        assert play["card_id"] == "GO_FOR_THE_EYES" and play["first_in_series"] is False, play
        play["energy_value"] = 1
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_history_mismatch:zero_cost_attacks_played_this_turn" in str(rejected), rejected


def test_original_glass_overflow_keeps_complete_visible_ttt_roll_with_two_enemies(client):
    """The unchanged V27 frontier proves Glass AoE and a fresh Plasma without a single-enemy assumption."""
    import hashlib
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-glass-overflow-v27.json"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == "2e73cce1820b8861aa4fd229e1975790e68be3908a332b29ec07dcfcf558b309"
    current = json.loads(fixture.read_text(encoding="utf-8"))["last_decision"]["projection"]["public_state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    assert [row["hp"]["current"] for row in restored["enemies"]] == [72, 65], restored
    assert restored["player"]["orb_details"] == current["combat"]["player"]["orb_details"], restored
    assert restored["player"]["hp"]["current"] == 64, restored
    wrong = deepcopy(current)
    for state in (wrong, wrong["public_history"]["combat_cost_origins"][-1]["after"]):
        state["combat"]["enemies"][1]["hp"] = "66/199"
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_enemy_state_mismatch" in str(rejected), rejected


def opening_rocket_v28():
    """Read the unchanged first public frame after Whispering Earring's four automatic finishes."""
    import hashlib
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-opening-rocket-v28.json"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == "f2f1bf30be5a8641079266e10f284fdbfdd7898da03e19e7a047ef0e493142b2"
    return json.loads(fixture.read_text(encoding="utf-8"))["last_decision"]["projection"]["public_state"]


def test_original_opening_rocket_recovers_only_the_proven_status_reaction(client):
    """Closed opening history proves UntilPlayed without duplicating the generating card or any counters."""
    current = opening_rocket_v28()
    assert current["public_history"]["combat_cost_origins"] == []
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    rocket = next(row for row in restored["pile_cards"]["draw"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and not rocket["cost_observation_only"], restored
    assert restored["piles"] == {"draw": 19, "discard": 5, "exhaust": 0}, restored
    assert len(restored["hand"]) == 1 and restored["hand"][0]["card_id"] == "ECHO_FORM_CARD", restored
    assert restored["player"]["hp"] == {"current": 75, "max": 75}, restored
    assert (restored["player"]["energy"], restored["player"]["block"]) == (2, 12), restored
    assert restored["player"]["orb_details"] == current["combat"]["player"]["orb_details"], restored
    assert restored["enemies"][0]["hp"]["current"] == 279, restored
    for key in ("generated_cards_this_combat", "cards_drawn_this_combat", "energy_spent_this_turn"):
        assert restored["history_statistics"][key] == current["combat"][key], restored


@pytest.mark.parametrize("missing_proof", ["incomplete", "missing_boost", "later_rocket", "extra_rocket", "extra_birth", "wrong_type"])
def test_opening_rocket_requires_complete_ordered_source_and_closed_cohort(client, missing_proof):
    """Incomplete order, consumed riders and unattributed same-ID births retain the original cost gap."""
    current = opening_rocket_v28()
    combat = current["combat"]
    history = combat["public_card_play_history"]
    if missing_proof == "incomplete":
        history["complete"] = False
    elif missing_proof == "missing_boost":
        history["plays"][-1]["card_id"] = "LIGHTNING_ROD"
    elif missing_proof == "later_rocket":
        history["plays"].append(finished_play(4, "ROCKET_PUNCH", auto=True))
        rocket = next(row for row in combat["draw"] if row["card_id"] == "ROCKET_PUNCH")
        combat["draw"].remove(rocket)
        combat["discard"].append(rocket)
        combat["cards_drawn_this_combat"] += 1
        combat["enemies"][0]["hp"] = "265/300"
        for band in combat["draw_knowledge"]["bands"]:
            band["cards"] = [row for row in band["cards"] if row["card_id"] != "ROCKET_PUNCH"]
        combat["draw_knowledge"]["discard_bands"].append({"cards": [deepcopy(rocket)]})
    elif missing_proof == "extra_rocket":
        combat["draw"].append(deepcopy(next(row for row in combat["draw"] if row["card_id"] == "ROCKET_PUNCH")))
    elif missing_proof == "extra_birth":
        combat["generated_cards_this_combat"] += 1
    else:
        history["plays"][-1]["card_type"] = "Attack"
    rejected = client.request({"op": "search", "public_state": current, "simulations": 8})
    assert not rejected.get("ok") and "card_energy_cost_not_reconstructible" in str(rejected), rejected


def test_opening_rocket_last_generation_restores_an_earlier_consumed_rider(client):
    """A controlled earlier Rocket finish permits a fresh rider after Boost; reversing it is rejected above."""
    current = opening_rocket_v28()
    combat = current["combat"]
    rocket = next(row for row in combat["draw"] if row["card_id"] == "ROCKET_PUNCH")
    combat["draw"].remove(rocket)
    combat["discard"].append(rocket)
    combat["cards_drawn_this_combat"] += 1
    combat["energy_spent_this_turn"] += 2
    combat["player"]["energy"] -= 2
    combat["enemies"][0]["hp"] = "265/300"
    for band in combat["draw_knowledge"]["bands"]:
        band["cards"] = [row for row in band["cards"] if row["card_id"] != "ROCKET_PUNCH"]
    combat["draw_knowledge"]["discard_bands"].append({"cards": [deepcopy(rocket)]})
    plays = combat["public_card_play_history"]["plays"]
    plays.insert(0, finished_play(0, "ROCKET_PUNCH", auto=True, value=2))
    for index, play in enumerate(plays):
        play["sequence"] = index
    restored = rollout_prefix(client, current, [])
    rocket = next(row for row in restored["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and not rocket["cost_observation_only"], restored


def controlled_opening_rocket():
    """Build a minimal declared post-autoplay root for isolated continuation checks."""
    current = history_frame([], character="DEFECT", generated_cards_this_combat=1,
        plays=[finished_play(0, "BOOST_AWAY", kind="Skill", auto=True)])
    current["run"].update(relic_ids=["WHISPERING_EARRING"], relic_stacks=[None], deck_size=2,
        deck=[{"card_id": name, "upgraded": False, "card_count": 1} for name in ("BOOST_AWAY", "ROCKET_PUNCH")])
    current["combat"]["draw"] = [knowledge_card("ROCKET_PUNCH", cost=0)]
    dazed = knowledge_card("DAZED", cost=0)
    dazed["effective_keywords"] = ["Ethereal", "Unplayable"]
    current["combat"]["discard"] = [knowledge_card("BOOST_AWAY", cost=0), dazed]
    current["action_revision"] = 0
    current["combat"]["action_readiness"] = dict.fromkeys(
        ("can_use_combat_actions", "actions_settled", "snapshot_stable"), True)
    return current


def test_opening_rocket_rider_survives_turn_then_expires_on_actual_play(client):
    """The opening recovery installs the original UntilPlayed flag, not a permanent or turn-only discount."""
    current = controlled_opening_rocket()
    after = rollout_prefix(client, current, [{"type": "end_turn"}])
    rocket = next(row for row in after["hand"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and not rocket["cost_observation_only"], after
    played = rollout_prefix(client, current, [{"type": "end_turn"},
        {"type": "play_card", "card_id": "ROCKET_PUNCH", "target_index": 0}])
    rocket = next(row for row in played["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2 and not rocket["cost_observation_only"], played


def test_opening_rocket_cost_survives_the_first_completed_manual_command(client):
    """The actual receipt collector must keep the opening proof when an unrelated hand card completes."""
    before = controlled_opening_rocket()
    before["combat"]["hand"] = [{**public_card("DEFEND_DEFECT"), "instance_id": "opening-defend"}]
    before["run"]["deck"].append({"card_id": "DEFEND_DEFECT", "upgraded": False, "card_count": 1})
    before["run"]["deck_size"] += 1
    action = {"type": "play_card", "card_index": 0}
    after = visible_successor(before, rollout_prefix(client, before, [action]))
    after["action_revision"] = 1
    after["combat"]["public_card_play_history"]["plays"].append(
        finished_play(1, "DEFEND_DEFECT", kind="Skill", instance="opening-defend", spent=1, value=1))
    for row in after["combat"]["discard"]:
        if identity := row.pop("instance_id", None):
            row["known_instance_ids"] = [identity]
    completed_public_step(PublicCombatHistory(), before, after, action, "opening-first-manual")
    origins = after["public_history"]["combat_cost_origins"]
    assert len(origins) == 1 and origins[0]["before"]["combat"] == before["combat"]
    assert {key: value for key, value in origins[0]["actions"][0].items() if key != "before"} == action
    assert after["public_history"]["combat_cost_chain"]["status"] == "collecting"
    restored = rollout_prefix(client, after, [])
    rocket = next(row for row in restored["pile_cards"]["draw"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and not rocket["cost_observation_only"], restored
    unproved = deepcopy(after)
    unproved["public_history"]["combat_cost_origins"][0]["before"]["combat"]["public_card_play_history"]["complete"] = False
    rejected = client.request({"op": "search", "public_state": unproved, "simulations": 8})
    assert not rejected.get("ok") and "combat_cost_origin_source_not_supported" in str(rejected), rejected


def test_original_wither_upgrade_matches_old_draws_and_keeps_the_new_discard_birth(client):
    """An original enemy upgrades old Withers before a no-refill draw and creates a separate discard birth."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-wither-upgrade-draw-v22.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok") and result["best"]["actions"][0]["type"] == "select_deck_card", result
    restored = rollout_prefix(client, current, [])
    assert restored["piles"] == {"draw": 8, "discard": 16, "exhaust": 5}, restored
    withers = [row for row in restored["hand"] if row["card_id"] == "WITHER"]
    assert len(withers) == 2 and all(row["dynamic_values"][0]["base_value"] == 9 for row in withers), restored
    stale = deepcopy(current)
    for container in (stale["combat"]["hand"], stale["selection"]["cards"]):
        for card in container:
            if card["card_id"] == "WITHER":
                for variable in card["dynamic_values"]:
                    variable.update(base_value=6, current_value=6, enchanted_value=6)
    rejected = client.request({"op": "search", "public_state": stale, "simulations": 8})
    assert not rejected.get("ok"), rejected
    mixed = deepcopy(current)
    before = mixed["public_history"]["combat_selection_origin"]["before"]
    for state in (before, mixed):
        for row in state["combat"]["discard"]:
            if row.get("card_id") == "WITHER" and row.get("known_instance_ids") == ["visible-34"]:
                row["known_instance_ids"] = []
    rejected = client.request({"op": "search", "public_state": mixed, "simulations": 8})
    assert not rejected.get("ok") and "combat_selection_witness_discard_identity_mismatch" in str(rejected), rejected


def test_original_white_noise_birth_uses_its_pool_and_original_turn_cost_rider(client):
    """Condition the observed new Power at its original sampler and retain its proved free-turn lifetime."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-white-noise-birth-v18.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok") and "character_card_generation_replayed_from_complete_public_birth" in result["support"]["reasons"], result
    defended = rollout_prefix(client, current, [{"type": "play_card", "card_index": 0}])
    buffer = next(row for row in defended["hand"] if row["card_id"] == "BUFFER_CARD")
    assert buffer["cost"] == 0 and buffer["generated_in_combat"] is True, defended
    ended = rollout_prefix(client, current, [{"type": "end_turn"}])
    buffers = [row for pile in ended["pile_cards"].values() for row in pile if row["card_id"] == "BUFFER_CARD"]
    assert len(buffers) == 1 and buffers[0]["cost"] == 2, ended
    # Refeed the three actual closed observations through the production collector, then observe one controlled remainder.
    tracker = PublicCombatHistory()
    for index, origin in enumerate(current["public_history"]["combat_cost_origins"]):
        tracked = deepcopy(origin["after"])
        action = origin["actions"][0].get("action", origin["actions"][0])
        completed_public_step(tracker, deepcopy(origin["before"]), tracked, action, f"actual-v18-source-{index}")
    assert tracked["combat"] == current["combat"], "The controlled receipt transport must preserve the original public mechanics"
    played = rollout_prefix(client, current, [{"type": "play_card", "card_index": 5}])
    after = deepcopy(tracked)
    after["action_revision"] += 1
    after["combat"]["hand"] = after["combat"]["hand"][:-1]
    for key in ("block", "energy", "stars", "powers", "power_details", "orb_capacity", "orb_details"):
        after["combat"]["player"][key] = deepcopy(played["player"][key])
    # Native formats both arrays from the same ordered power list; the host's display strings are independently sorted.
    after["combat"]["player"]["powers"] = [f"{row['power_id']} {row['amount']}"
        for row in after["combat"]["player"]["power_details"]]
    for key in tuple(after["combat"]):
        if key in played["history_statistics"]:
            after["combat"][key] = deepcopy(played["history_statistics"][key])
    plays = after["combat"]["public_card_play_history"]["plays"]
    plays.append(finished_play(len(plays), "BUFFER_CARD", kind="Power", instance="visible-25"))
    completed_public_step(tracker, tracked, after, {"type": "play_card", "card_index": 5, "card_id": "BUFFER_CARD"}, "controlled-buffer-play")
    assert len(after["public_history"]["combat_cost_origins"]) == 4, after["public_history"]
    result = client.request({"op": "search", "public_state": after, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    assert any("BUFFER" in row["power_id"].upper() and row["amount"] == 1 for row in after["combat"]["player"]["power_details"])
    missing = deepcopy(after)
    missing["public_history"]["combat_cost_origins"].pop()
    rejected = client.request({"op": "search", "public_state": missing, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_cost_generated_birth_lineage_not_witnessed" in str(rejected), rejected
    wrong = deepcopy(after)
    for state in (wrong, wrong["public_history"]["combat_cost_origins"][-1]["after"]):
        for row in state["combat"]["player"]["power_details"]:
            if "BUFFER" in row["power_id"].upper():
                row["amount"] = 2
                row["display_amount"] = 2
        state["combat"]["player"]["powers"] = [line.rsplit(" ", 1)[0] + " 2" if "BUFFER" in line.upper() else line
            for line in state["combat"]["player"]["powers"]]
    rejected = client.request({"op": "search", "public_state": wrong, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_player_power_mismatch" in str(rejected), rejected
    changed = deepcopy(current)
    for state in (changed, changed["public_history"]["combat_cost_origins"][-1]["after"]):
        state["combat"]["hand"][-1]["energy_cost"] = 1
    rejected = client.request({"op": "search", "public_state": changed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_cost_generated_birth_not_reconstructible" in str(rejected), rejected
    outside = deepcopy(current)
    for state in (outside, outside["public_history"]["combat_cost_origins"][-1]["after"]):
        state["combat"]["hand"][-1].update(card_id="DEMON_FORM", card_ids=["DEMON_FORM"], line="DEMON_FORM*1")
    rejected = client.request({"op": "search", "public_state": outside, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "observed_generated_card_outside_positive_support" in str(rejected), rejected


def test_original_named_rocket_expiry_does_not_create_a_second_modifier(client):
    """A known UntilPlayed fact can expire during a later status-birth witness after its anonymous chain bound."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-named-rocket-expiry-v19.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    rocket = next(row for row in restored["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2, restored
    changed = deepcopy(current)
    for state in (changed, changed["public_history"]["combat_cost_origins"][-1]["after"]):
        next(row for row in state["combat"]["discard"] if row["card_id"] == "ROCKET_PUNCH")["energy_cost"] = 0
    rejected = client.request({"op": "search", "public_state": changed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_cost_origin_current_quote_contradicts_rule" in str(rejected), rejected


def test_seen_homogeneous_birth_cohort_survives_an_unrelated_completed_action(client):
    """A still-anonymous earlier birth needs the actual next command even after new births have visible IDs."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-seen-birth-cohort-v20.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    original = deepcopy(current)
    rejected = client.request({"op":"search","public_state":current,"simulations":8,"scenario_seed":41})
    assert not rejected.get("ok"), "The unchanged old root lacks its final completed Lightning Rod witness"
    # Recollect only the fixture's actual completed before/actions/after, preserving all public mechanics.
    tracker = PublicCombatHistory()
    encounter = current["public_history"]["encounter_id"]

    def record_actual(before, after, action, action_id):
        """Feed the production successor-first collector with this original public command boundary."""
        tracker.observe(before, encounter, (1, "offline-public-fixture"))
        command = {"action_id":action_id,"action":action,
            "observation":{"state":before,"public_history":tracker.snapshot()}}
        tracker.observe(after, encounter, (1, "offline-public-fixture"))
        return tracker.completed(command,{"action_id":action_id,"status":"completed"},{"state":after})

    witnesses = original["public_history"]["combat_cost_origins"]
    for witness in witnesses:
        assert len(witness["actions"]) == 1, "This original fixture contains nine single-command boundaries"
        action = {key:value for key,value in witness["actions"][0].items() if key != "before"}
        record_actual(witness["before"],witness["after"],action,witness["action_id"])
    assert tracker.snapshot()["combat_cost_chain"]["status"] == "collecting"
    assert tracker.snapshot()["combat_cost_chain"]["last_revision"] == 245
    before = witnesses[-1]["after"]
    assert before["action_revision"] == 245 and current["action_revision"] == 246
    assert before["combat"]["hand"][3]["card_id"] == "LIGHTNING_ROD"
    # The captured original 245→246 action was Lightning Rod; its successor is the unchanged original root.
    actual_action = {"card_handle":"h3","card_id":"LIGHTNING_ROD","card_index":3,"type":"play_card"}
    current["public_history"] = record_actual(before,original,actual_action,"offline-completed-step-245")
    assert {key:value for key,value in current.items() if key != "public_history"} == {
        key:value for key,value in original.items() if key != "public_history"}
    assert len(current["public_history"]["combat_cost_origins"]) == 10
    result = client.request({"op":"search","public_state":current,"simulations":8,"scenario_seed":41})
    assert result.get("ok"), result
    restored = rollout_prefix(client,current,[])
    proved = [row for row in restored["hand"] if row["card_index"] in (3,4)]
    assert len(proved) == 2 and all(row["card_id"] == "DAZED" and row["generated_in_combat"] is True for row in proved), restored
    changed = deepcopy(current)
    next(row for row in changed["combat"]["hand"] if row["instance_id"] == "visible-26")["instance_id"] = "an-unproved-replacement"
    rejected = client.request({"op":"search","public_state":changed,"simulations":8,"scenario_seed":41})
    assert not rejected.get("ok") and "combat_cost_generated_birth_not_reconstructible" in str(rejected), rejected
    missing = deepcopy(current)
    missing["combat"]["hand"] = [row for row in missing["combat"]["hand"] if row["instance_id"] != "visible-26"]
    rejected = client.request({"op":"search","public_state":missing,"simulations":8,"scenario_seed":41})
    assert not rejected.get("ok") and "combat_cost_generated_birth_not_reconstructible" in str(rejected), rejected


def test_original_non_hand_birth_class_keeps_all_slimed_generation_flags(client):
    """A whole anonymous class of this prefix's births can bind without mixing old generation states."""
    import json
    from pathlib import Path

    current = json.loads((Path(__file__).with_name("fixtures") / "public-slimed-cohort-v19.json").read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok"), result
    restored = rollout_prefix(client, current, [])
    slimed = [row for row in restored["pile_cards"]["discard"] if row["card_id"] == "SLIMED"]
    assert len(slimed) == 10 and all(row["generated_in_combat"] is True for row in slimed), restored
    disclosed = deepcopy(current)
    next(row for row in disclosed["combat"]["discard"] if row["card_id"] == "SLIMED")["known_instance_ids"] = ["unbound-slimed-instance"]
    rejected = client.request({"op": "search", "public_state": disclosed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_discard_identity_mismatch" in str(rejected), rejected
    mixed = deepcopy(current)
    old = deepcopy(next(row for row in current["combat"]["discard"] if row["card_id"] == "SLIMED"))
    mixed["public_history"]["combat_selection_origin"]["before"]["combat"]["discard"].append(deepcopy(old))
    mixed["combat"]["discard"].append(old)
    rejected = client.request({"op": "search", "public_state": mixed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_discard_identity_mismatch" in str(rejected), rejected


def test_fetch_public_instance_predicates_survive_play_and_reset_at_the_actual_turn(client):
    """A real per-card predicate resolves a prior unseen autoplay without guessing a historical ID."""
    state = history_frame([public_card("FETCH", cost=0, target="enemies"), public_card("FETCH", index=1, cost=0, target="enemies")],
        character="NECROBINDER", plays=[finished_play(0,"FETCH",auto=True)])
    for row, played in zip(state["combat"]["hand"], (True,False)):
        row.update(instance_id=f"newly-visible-fetch-{row['i']}",fetch_played_this_turn=played)
    state["combat"]["player"]["pet_details"] = [{"pet_id":"OSTY","current_hp":10,"max_hp":10,"block":0,"power_details":[]}]
    state["combat"]["draw"] = [knowledge_card("DEFEND_NECROBINDER", identity=f"fetch-predicate-draw-{index}") for index in range(2)]
    after = rollout_prefix(client,state,[{"type":"play_card","card_index":0,"target_index":0},
        {"type":"play_card","card_index":1,"target_index":0}])
    assert len(after["hand"]) == 1 and after["piles"]["draw"] == 1, after
    assert all(row["fetch_played_this_turn"] is True for row in after["pile_cards"]["discard"] if row["card_id"] == "FETCH"), after
    ended = rollout_prefix(client,state,[{"type":"end_turn"}])
    fetches = [row for row in ended["hand"] + [row for rows in ended["pile_cards"].values() for row in rows] if row["card_id"] == "FETCH"]
    assert len(fetches) == 2 and all(row["fetch_played_this_turn"] is False for row in fetches), ended
    missing = deepcopy(state)
    missing["combat"]["hand"][1]["fetch_played_this_turn"] = None
    rejected = client.request({"op":"search","public_state":missing,"simulations":8})
    assert not rejected.get("ok") and "public_history_statistic_not_observed:fetch_played_this_turn" in str(rejected), rejected
    conflict = deepcopy(state)
    conflict["combat"]["public_card_play_history"]["plays"][0]["known_instance_id"] = "newly-visible-fetch-1"
    rejected = client.request({"op":"search","public_state":conflict,"simulations":8})
    assert not rejected.get("ok") and "public_fetch_played_state_conflicts_with_finished_history" in str(rejected), rejected


def test_anonymous_fetch_predicates_are_part_of_the_draw_knowledge_face(client):
    """Distinct anonymous predicates survive a real draw, newly seen IDs, choice rebinding and another play."""
    state = history_frame([public_card("PHOTON_CUT",cost=1,target="enemies",upgraded=True)],character="NECROBINDER",
        plays=[finished_play(index,"FETCH",auto=True) for index in range(2)])
    state["combat"]["hand"][0]["instance_id"] = "fetch-draw-photon"
    state["combat"]["player"]["pet_details"] = [{"pet_id":"OSTY","current_hp":10,"max_hp":10,"block":0,"power_details":[]}]
    unseen = {**knowledge_card("FETCH",cost=0),"card_count":2,"line":"FETCH*2","fetch_played_this_turn":False}
    played = {**knowledge_card("FETCH",cost=0),"card_count":2,"line":"FETCH*2","fetch_played_this_turn":True}
    state["combat"]["draw"] = [deepcopy(unseen),deepcopy(played)]
    attach_draw_knowledge(state,bands=[[deepcopy(played)],[deepcopy(unseen)]])
    action = {"type":"play_card","card_index":0,"target_index":0}
    snapshot = rollout_prefix(client,state,[action])
    current = open_choice(client,state,action,snapshot=snapshot)
    current["combat"]["play"] = [{**deepcopy(state["combat"]["hand"][0]),"source_pile":"Play","source_index":0}]
    attach_draw_knowledge(current,bands=[[deepcopy(unseen)]])
    for index,row in enumerate(current["combat"]["hand"]):
        row.update(instance_id=f"first-seen-played-fetch-{index}",fetch_played_this_turn=True)
    current["selection"]["cards"] = [{**choice_card(row,11+3*index),"source_pile":"Hand","source_index":index}
        for index,row in enumerate(current["combat"]["hand"])]
    restored = rollout_prefix(client,current,[])
    assert len(restored["hand"]) == 2 and all(row["fetch_played_this_turn"] is True for row in restored["hand"]), restored
    after = rollout_prefix(client,current,[{"type":"select_deck_card","option_index":11},
        {"type":"play_card","card_index":1,"target_index":0}])
    assert after["hand"] == [] and after["piles"]["draw"] == 3, after


def test_historical_channels_and_star_gains_are_not_replaced_by_the_current_belt_or_stars(client):
    """Voltaic remembers already-evoked Lightning, and Radiate remembers stars that were subsequently spent."""
    state = history_frame([public_card("VOLTAIC", cost=3)], character="DEFECT", lightning_orbs_channeled_this_combat=3)
    state["combat"]["player"].update(orb_capacity=3, orb_details=[])
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0}])
    assert [orb["orb_id"] for orb in after["player"]["orb_details"]] == ["Lightning"] * 3, after
    assert after["history_statistics"]["lightning_orbs_channeled_this_combat"] == 6, after
    state = history_frame([public_card("RADIATE", cost=0)], character="REGENT", stars_gained_this_turn=3)
    state["combat"]["player"]["stars"] = 0
    after = rollout_prefix(client, state, [{"type": "play_card", "card_index": 0}])
    assert after["enemies"][0]["hp"]["current"] == 91, after


def test_original_chomper_births_remain_hidden_during_complete_observed_refill(client):
    """Preserve the actual V16 failure while conditioning only old drawn UIDs, never pre-inserting new Dazed."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name("fixtures") / "public-chomper-refill-v16.json"
    current = json.loads(fixture.read_text(encoding="utf-8"))["state"]
    result = client.request({"op": "search", "public_state": current, "simulations": 8, "scenario_seed": 41})
    assert result.get("ok") and result["best"]["actions"][0]["type"] == "select_deck_card", result
    assert "non_hand_births_replayed_before_complete_observed_old_instance_draws" in result["support"]["reasons"], result
    restored = rollout_prefix(client, current, [])
    assert [row["card_id"] for row in restored["hand"]] == [row["card_id"] for row in current["combat"]["hand"]], restored
    statuses = [row for row in restored["pile_cards"]["draw"] if row["card_id"] == "DAZED"]
    assert len(statuses) == 6 and sum(row["generated_in_combat"] is True for row in statuses) == 3, restored
    assert sum(row["generated_in_combat"] is None for row in statuses) == 3, restored
    changed = deepcopy(current)
    # The published pile has singleton rows; remove one rather than making an invalid zero-count row.
    dazed_index = next(index for index, row in enumerate(changed["combat"]["draw"]) if row["card_ids"] == ["DAZED"])
    del changed["combat"]["draw"][dazed_index]
    for band in changed["combat"]["draw_knowledge"].get("bands", []):
        for row in band["cards"]:
            if row["card_ids"] == ["DAZED"]:
                row["card_count"] -= 1
    rejected = client.request({"op": "search", "public_state": changed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_card_pile_mismatch" in str(rejected), rejected
    # An anonymous mixed class cannot be labelled as one particular newly disclosed physical instance.
    disclosed = deepcopy(current)
    dazed = next(row for row in disclosed["combat"]["draw"] if row["card_ids"] == ["DAZED"])
    dazed["known_instance_ids"] = ["unbound-status-instance"]
    for band in disclosed["combat"]["draw_knowledge"].get("bands", []):
        for row in band["cards"]:
            if row["card_ids"] == ["DAZED"]:
                row["known_instance_ids"] = ["unbound-status-instance"]
    rejected = client.request({"op": "search", "public_state": disclosed, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_draw_identity_mismatch" in str(rejected), rejected
    ordered = deepcopy(current)
    rows = [row for band in ordered["combat"]["draw_knowledge"]["bands"] for row in band["cards"]]
    statuses = [row for row in rows if row["card_ids"] == ["DAZED"]]
    remainder = [row for row in rows if row["card_ids"] != ["DAZED"]]
    ordered["combat"]["draw_knowledge"]["bands"] = [{"cards": statuses}, {"cards": remainder}]
    rejected = client.request({"op": "search", "public_state": ordered, "simulations": 8, "scenario_seed": 41})
    assert not rejected.get("ok") and "combat_selection_witness_draw_identity_mismatch" in str(rejected), rejected


def test_anonymous_rocket_uses_a_continuous_original_chain_until_real_identity_and_play_expiry(client):
    """Follow an anonymous Draw card through actual public commands, then bind its first seen ID without fabricating one."""
    tracker = PublicCombatHistory()
    before = history_frame([public_card("BOOST_AWAY", cost=0), public_card("DEFEND_DEFECT", index=1)], character="DEFECT")
    before["combat"]["hand"][0]["instance_id"] = "anonymous-source-boost"
    before["combat"]["hand"][1]["instance_id"] = "anonymous-source-defend"
    before["combat"].update(draw=[knowledge_card("ROCKET_PUNCH", cost=2)], play=[],
        action_readiness=dict.fromkeys(("can_use_combat_actions", "actions_settled", "snapshot_stable"), True))
    before["action_revision"] = 0

    def publish(previous, actions, action_id, *, card=None, kind="Skill", paid=0):
        """Transport original prefix quantities through the production collector with explicit controlled native IDs."""
        snapshot = rollout_prefix(client, previous, actions)
        current = visible_successor(previous, snapshot)
        current["action_revision"] = previous["action_revision"] + 1
        if actions[0]["type"] == "end_turn":
            for play in current["combat"]["public_card_play_history"]["plays"]:
                play["this_turn"] = False
            current["combat"]["enemies"][0]["intents"] = [{"intent_type": "Attack", "damage": 14, "hits": 1}]
        if card:
            source = next(row for row in previous["combat"]["hand"] if row["i"] == actions[0]["card_index"])
            plays = current["combat"]["public_card_play_history"]["plays"]
            plays.append(finished_play(len(plays), card, kind=kind, instance=source["instance_id"], spent=paid, value=paid))
        for pile in ("draw", "discard", "exhaust"):
            for row in current["combat"][pile]:
                if identity := row.pop("instance_id", None):
                    row["known_instance_ids"] = [identity]
        for row in current["combat"]["hand"]:
            if len(row.get("known_instance_ids", [])) == 1:
                row["instance_id"] = row.pop("known_instance_ids")[0]
            elif not row.get("instance_id"):
                row["instance_id"] = f"first-real-visible-{row['card_id']}"
                row.pop("known_instance_ids", None)
        completed_public_step(tracker, previous, current, actions[0], action_id)
        return current

    boosted = publish(before, [{"type": "play_card", "card_index": 0}], "anonymous-boost", card="BOOST_AWAY")
    assert boosted["public_history"]["combat_cost_chain"]["status"] == "collecting", boosted
    inspected = rollout_prefix(client, boosted, [])
    rocket = next(row for row in inspected["pile_cards"]["draw"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 0 and rocket["cost_observation_only"] is False, inspected
    defended = publish(boosted, [{"type": "play_card", "card_index": 0}], "anonymous-defend", card="DEFEND_DEFECT", paid=1)
    drawn = publish(defended, [{"type": "end_turn"}], "anonymous-end-turn")
    assert drawn["public_history"]["combat_cost_chain"]["status"] == "bound", drawn
    rocket = next(row for row in drawn["combat"]["hand"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["instance_id"] == "first-real-visible-ROCKET_PUNCH" and rocket["energy_cost"] == 0, drawn
    played = publish(drawn, [{"type": "play_card", "card_index": rocket["i"], "target_index": 0}],
                     "anonymous-rocket-play", card="ROCKET_PUNCH", kind="Attack")
    restored = rollout_prefix(client, played, [])
    rocket = next(row for row in restored["pile_cards"]["discard"] if row["card_id"] == "ROCKET_PUNCH")
    assert rocket["cost"] == 2 and rocket["cost_observation_only"] is False, restored
    broken = deepcopy(drawn)
    broken["public_history"]["combat_cost_origins"].pop(1)
    rejected = client.request({"op": "search", "public_state": broken, "simulations": 8})
    assert not rejected.get("ok") and "anonymous_cost_cohort_command_gap_not_witnessed" in str(rejected), rejected


def test_active_nightmare_without_its_selected_reference_does_not_accept_a_fabricated_counter(client):
    """Power amount and external arena integers cannot substitute for the card snapshot selected in the real rule."""
    before = history_frame([public_card("DEFEND_SILENT")], character="SILENT")
    before["combat"]["player"]["powers"] = ["NIGHTMARE_POWER 3"]
    for fabricated in (False, True):
        state = deepcopy(before)
        if fabricated:
            state["combat"]["player"]["power_details"] = [{"index": 0, "power_id": "NIGHTMARE_POWER", "amount": 3,
                "display_amount": 3, "counter": 1, "selected_card_uid": 1}]
        result = client.request({"op": "search", "public_state": state, "simulations": 8})
        assert not result.get("ok") and result["best"] is None, result
        assert "nightmare_selected_card_not_restored" in result["support"]["blocking_reasons"], result


def test_original_nightmare_choice_keeps_its_selected_snapshot_and_pre_draw_copies(client):
    """A witnessed original selection still creates the correct three copies before the next natural draw."""
    before = history_frame([public_card("NIGHTMARE", cost=3),
        public_card("STRIKE_SILENT", index=1, upgraded=True, target="enemies"), public_card("DEFEND_SILENT", index=2)],
        character="SILENT")
    for index, row in enumerate(before["combat"]["hand"]):
        row["instance_id"] = f"nightmare-source-{index}"
    before["combat"]["play"] = []
    current = open_choice(client, before, {"type": "play_card", "card_index": 0})
    current["combat"]["play"] = [{**deepcopy(before["combat"]["hand"][0]), "i": 0, "source_pile": "Play"}]
    selected = next(row["i"] for row in current["selection"]["cards"] if row["card_id"] == "STRIKE_SILENT")
    copied = rollout_prefix(client, current, [{"type": "select_deck_card", "option_index": selected}, {"type": "end_turn"}])
    assert copied["round"] == 2 and all(row["card_id"] == "STRIKE_SILENT" and row["upgraded"]
        for row in copied["hand"][:3]), copied
    assert sum(row["card_id"] == "STRIKE_SILENT" for row in copied["hand"]) == 4, copied
    assert not any(power.startswith("NIGHTMARE_POWER") for power in copied["player"]["powers"]), copied


def test_internal_prediction_does_not_publish_the_new_generation_annotation(client):
    """Authorized internal simulation keeps its rule state while returning no new private generation field values."""
    state = search_frame([public_card("BOOST_AWAY", cost=0)], character="DEFECT")
    result = client.request({"op": "evaluate", "rollout": True, "read_access": "internal", "public_state": state,
        "internal_state": internal_state(state), "samples": 1, "simulations": 1, "max_rollout_depth": 1,
        "scenario_seed": 41, "candidate_plans": [{"id": "private", "actions": [{"type": "play_card", "card_index": 0}]}]})
    assert result.get("ok"), result
    sample = result["candidates"][0]["samples"][0]
    assert sample["status"] == "completed", result
    cards = sample["state"]["hand"] + [row for pile in sample["state"]["pile_cards"].values() for row in pile]
    assert any(row["card_id"] == "DAZED" for row in cards), result
    assert all(row["generated_in_combat"] is None for row in cards), result

"""Regress native-witnessed counter restoration and deterministic survival-aware search."""

from copy import deepcopy

import pytest

from test_public_adapter import client, evaluate, frame, public_card


def witnessed(state, attacks=0, skills=0, manual=1):
    """Attach complete public totals that deliberately differ from manual dispatch counters."""
    state["public_history"] = {
        "schema_version": 1, "history_complete": True,
        "total_card_plays_this_turn": attacks + skills,
        "total_attack_plays_this_turn": attacks,
        "total_skill_plays_this_turn": skills,
        "manual_card_plays_this_turn": manual,
    }
    state["combat"]["player"].update(cards_played_this_turn=manual,
                                    attacks_played_this_turn=min(attacks, manual),
                                    skills_played_this_turn=min(skills, manual))
    return state


@pytest.mark.parametrize("relic,power", [("ORNAMENTAL_FAN", None), ("KUNAI", "DEXTERITY_POWER 1"),
                                         ("SHURIKEN", "STRENGTH_POWER 1")])
def test_total_attack_history_restores_third_attack_relics(client, relic, power):
    """A duplicated first attack counts for the next native three-attack trigger."""
    state = witnessed(frame([public_card("STRIKE_IRONCLAD", target="enemies")]), attacks=2)
    state["run"].update(relic_ids=[relic], relic_stacks=[None])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result["ok"], result
    assert result["state"]["player"]["attacks_played_this_turn"] == 3
    if power:
        assert power in result["state"]["player"]["powers"]
    else:
        assert result["state"]["player"]["block"] == 4


def test_complete_history_satisfies_known_flags_without_legacy_counters(client):
    """Complete witnessed counts remain valid even when obsolete bridge counters are absent."""
    state = witnessed(frame([public_card("DEFEND_IRONCLAD")]), skills=2)
    state["run"].update(relic_ids=["LETTER_OPENER"], relic_stacks=[None])
    for name in ("cards_played_this_turn", "attacks_played_this_turn", "skills_played_this_turn"):
        state["combat"]["player"].pop(name)
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result["ok"], result
    assert result["state"]["enemies"][0]["hp"]["current"] == 95


def lethal_root():
    """Expose a choice where immediate attack score used to outweigh certain enemy-turn death."""
    state = frame([public_card("BLUDGEON", cost=3, target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=1)])
    state["combat"]["player"].update(hp="4/80", energy=3)
    state["combat"]["enemies"] = [{"i": 0, "enemy_id": "EXOSKELETON", "hp": "100/100", "block": 0,
        "powers": [], "alive": True, "hittable": True,
        "intents": [{"intent_type": "Attack", "damage": 8, "hits": 1, "total_damage": 8}]}]
    return state


@pytest.mark.parametrize("strategy", ["greedy", "beam", "exhaustive"])
def test_search_prefers_deterministic_survival_over_damage(client, strategy):
    """The known-safe Defend outranks Bludgeon even when all planners share one objective."""
    result = client.request({"op": "search", "strategy": strategy, "public_state": lethal_root()})
    assert result["best"]["actions"][0]["card_id"] == "DEFEND_IRONCLAD", result
    evaluation = result["best"]["evaluation"]
    assert evaluation["outcome"] == "survived_through_enemy_turn" and evaluation["hp"] == 1
    assert result["best"]["state"]["player"]["hp"]["current"] == 4
    assert result["best"]["state"]["end_turn_executed"] is False


def test_unknown_enemy_settlement_does_not_become_safe(client):
    """An unsupported telegraph retains unknown status instead of trusting its arithmetic preview."""
    state = lethal_root()
    state["combat"]["enemies"][0]["intents"][0].update(damage=99, total_damage=99)
    result = client.request({"op": "search", "strategy": "beam", "public_state": state})
    assert result["best"]["evaluation"]["outcome"] == "unknown", result
    assert "not_matched" in result["best"]["evaluation"]["boundary_reason"]


def test_growth_power_is_kept_without_claiming_future_win_probability(client):
    """A persistent Strength improvement no longer loses solely to unspent ending energy."""
    result = client.request({"op": "search", "strategy": "beam", "public_state": frame([public_card("INFLAME")])})
    assert result["best"]["actions"][0]["card_id"] == "INFLAME", result
    assert result["best"]["evaluation"]["growth_heuristic"] > 0
    assert result["heuristic_score_only"] is True


def test_new_power_survives_prefix_and_prevents_certain_death(client):
    """Feel No Pain must persist until Dazed exhausts at turn end and grants lifesaving block."""
    state = lethal_root()
    state["combat"]["player"]["hp"] = "6/80"
    # The bridge exports current energy cost; static catalogue -1 describes Unplayable instead.
    dazed = public_card("DAZED", index=1, cost=0)
    dazed["playable"] = False
    state["combat"]["hand"] = [public_card("FEEL_NO_PAIN"), dazed]
    result = client.request({"op": "search", "strategy": "beam", "public_state": state})
    assert result["best"]["actions"][0]["card_id"] == "FEEL_NO_PAIN", result
    assert result["best"]["evaluation"]["outcome"] == "survived_through_enemy_turn"
    assert result["best"]["evaluation"]["hp"] == 1
    invalid_quote = deepcopy(state)
    invalid_quote["combat"]["hand"][1]["energy_cost"] = -1
    invalid = client.request({"op": "search", "strategy": "beam", "public_state": invalid_quote})
    assert "card_energy_cost_not_reconstructible:-1:core_expected:0" in str(invalid["support"]), invalid
    assert invalid["best"]["evaluation"]["outcome"] == "unknown", invalid
    assert invalid["best"]["evaluation"]["boundary_reason"] == "transition_depends_on_unsupported_card_overlay", invalid


def test_exhaustive_budget_keeps_best_so_far_and_withholds_optimality(client):
    """A one-pass partial traversal returns its useful first result, never a false optimum."""
    state = frame([public_card("STRIKE_IRONCLAD", index=i, target="enemies") for i in range(3)])
    result = client.request({"op": "search", "strategy": "exhaustive", "public_state": state,
                             "exhaustive_limit": 2})
    assert result["best"] is not None and result["best"]["actions"][0]["type"] == "play_card", result
    assert result["reason"] == "exhaustive_terminal_limit_exceeded"
    assert result["optimal_within_supported_horizon"] is False
    assert result["metrics"]["count_nodes"] == 0


def test_pile_composition_export_does_not_expose_hypothetical_order(client):
    """Reversing input rows leaves the sorted multiset descriptors unchanged."""
    state = frame()
    state["combat"]["draw"] = [{"card_ids": [name], "line": name, "energy_cost": 1}
                                for name in ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD")]
    first = client.request({"op": "inspect", "public_state": state})
    reverse = deepcopy(state)
    reverse["combat"]["draw"].reverse()
    second = client.request({"op": "inspect", "public_state": reverse})
    assert first["state"]["pile_cards"] == second["state"]["pile_cards"]
    assert len(first["state"]["pile_cards"]["draw"]) == 2
    assert first["state"]["pile_order"] == "unordered_multiset"


def test_scarf_discount_applies_only_to_hand(client):
    """Native differential evidence shows the fifth-play discount never changes pile costs."""
    state = witnessed(frame([public_card("DEFEND_IRONCLAD", cost=0)]), skills=4, manual=4)
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4])
    state["combat"]["draw"] = [{"card_ids": ["DEFEND_IRONCLAD"], "energy_cost": 1,
                                 "line": "DEFEND_IRONCLAD [1]"}]
    result = client.request({"op": "inspect", "public_state": state})
    assert result["ok"], result
    assert result["state"]["hand"][0]["cost"] == 0
    assert result["state"]["pile_cards"]["draw"][0]["cost"] == 1


def test_pending_manual_series_does_not_claim_completed_history(client):
    """A suspended card play has started but is absent from native completed public history."""
    state = frame([public_card("ARMAMENTS"), public_card("STRIKE_IRONCLAD", index=1, target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=2)])
    result = evaluate(client, state, [{"type": "play_card", "card_index": 0}])["samples"][0]
    assert result["state"]["pending_choice"] is not None, result
    assert result["state"]["player"]["manual_card_plays_this_turn"] is None
    assert result["state"]["player"]["manual_card_series_started_this_turn"] == 1


def test_scarf_counts_duplicated_completed_plays_on_reimport(client):
    """Native batch #47 disproves using manual series instead of Scarf's completed-play count."""
    state = witnessed(frame([public_card("BLUDGEON", cost=0, target="enemies"),
                              public_card("DEFEND_IRONCLAD", index=1, cost=0)]), attacks=1, skills=3, manual=3)
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4])
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0},
                    {"type": "play_card", "card_index": 1}]})
    assert result["ok"], result
    assert result["state"]["player"]["energy"] == 2
    assert result["state"]["player"]["cards_played_this_turn"] == 6
    assert result["state"]["player"]["manual_card_plays_this_turn"] == 5


def test_scarf_replay_counter_evolves_without_reimport(client):
    """A duplicate followed by two manual cards activates the fifth completed-play discount."""
    state = frame([public_card("DEFEND_IRONCLAD", index=i) for i in range(3)] +
                  [public_card("BLUDGEON", index=3, cost=3, target="enemies")])
    state["combat"]["player"]["energy"] = 8
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[0])
    state["run"]["potions"] = [{"i": 0, "potion_id": "DUPLICATOR", "occupied": True, "usable": True}]
    actions = [{"type": "use_potion", "option_index": 0}] + [
        {"type": "play_card", "card_index": i} for i in range(3)]
    result = evaluate(client, state, actions)["samples"][0]
    assert result["state"]["player"]["cards_played_this_turn"] == 4
    assert result["state"]["player"]["manual_card_plays_this_turn"] == 3
    assert result["state"]["hand"][0]["cost"] == 0


@pytest.mark.parametrize("cards", [
    ["DEFEND_IRONCLAD", "STRIKE_IRONCLAD"],
    ["DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "INFLAME"],
    ["DEFEND_IRONCLAD", "DEFEND_IRONCLAD", "BODY_SLAM"],
])
def test_small_complete_beam_agrees_with_exhaustive_objective(client, cards):
    """A beam wider than these tiny legal frontiers must preserve the exhaustive endpoint objective."""
    state = lethal_root()
    state["combat"]["hand"] = [public_card(name, index=index,
        target="enemies" if name in {"STRIKE_IRONCLAD", "BODY_SLAM"} else None)
        for index, name in enumerate(cards)]
    beam = client.request({"op": "search", "strategy": "beam", "public_state": state})
    exhaustive = client.request({"op": "search", "strategy": "exhaustive", "public_state": state})
    assert exhaustive["optimal_within_supported_horizon"], exhaustive
    assert beam["best"]["evaluation"]["outcome"] == exhaustive["best"]["evaluation"]["outcome"]
    assert beam["best"]["evaluation"]["score"] == exhaustive["best"]["evaluation"]["score"]


def test_beam_keeps_non_improving_setup_needed_to_survive(client):
    """Paying HP for energy is initially insufficient, but enables the only lifesaving block card."""
    state = lethal_root()
    state["combat"]["player"].update(hp="6/80", energy=0)
    state["combat"]["hand"] = [public_card("BLOODLETTING", cost=0),
                                 public_card("IMPERVIOUS", index=1, cost=2)]
    for strategy in ("beam", "exhaustive"):
        result = client.request({"op": "search", "strategy": strategy, "public_state": state})
        assert [action.get("card_id") for action in result["best"]["actions"][:2]] == ["BLOODLETTING", "IMPERVIOUS"], result
        assert result["best"]["evaluation"]["outcome"] == "survived_through_enemy_turn"


@pytest.mark.parametrize("automatic,expected", [(True, 3), (False, 0)])
def test_scarf_distinguishes_auto_from_copy_in_complete_native_history(client, automatic, expected):
    """Havoc and Duplicator can share total/manual counts while their native Scarf costs differ."""
    state = witnessed(frame([public_card("BLUDGEON", cost=expected, target="enemies")]), skills=4, manual=3)
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[None])
    state["combat"]["public_card_play_history"] = {"available": True, "complete": True,
        "scope": "current_combat", "plays": [{"card_type": "Skill", "this_turn": True,
            "auto_played": automatic if index == 0 else False,
            "first_in_series": index != 0 or automatic} for index in range(4)]}
    result = client.request({"op": "inspect", "public_state": state})
    assert result["state"]["hand"][0]["cost"] == expected
    assert result["state"]["hand"][0]["cost_observation_only"] is False
    assert result["state"]["player"]["non_auto_card_plays_this_turn"] == (3 if automatic else 4)


def test_scarf_total_and_manual_scalars_do_not_invent_non_auto_history(client):
    """Without non-auto history or the displayed relic counter the ambiguous threshold remains unknown."""
    state = witnessed(frame([public_card("DEFEND_IRONCLAD")]), skills=4, manual=3)
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[None])
    result = client.request({"op": "inspect", "public_state": state})
    assert "brilliant_scarf_non_auto_completed_count_not_observed" in result["support"]["blocking_reasons"]
    assert result["state"]["player"]["non_auto_card_plays_this_turn"] is None

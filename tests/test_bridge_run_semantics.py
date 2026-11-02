"""Preserve structured run-policy facts through the public bridge and model observation boundary."""

from copy import deepcopy

from sts2_agent.bridge import Bridge
from sts2_agent.public import observation


def test_fetch_cohort_predicate_preserves_true_false_and_unknown_without_new_ids():
    """The bridge keeps native cohort facts distinct through the model observation boundary."""
    rows = [{"card_ids": ["FETCH"], "card_count": 1, "known_instance_ids": [],
             "fetch_played_this_turn": value} for value in (True, False, None)]
    public = {"protocol_version": 1, "screen": "COMBAT", "available_actions": [],
              "combat": {"discard": rows, "hand": [{"card_id": "FETCH", "instance_id": "card-1",
                         "fetch_played_this_turn": True}]}}
    bridge = Bridge({"bridge": {"base_url": "http://127.0.0.1:1"}})
    bridge._request = lambda *args, **kwargs: {
        "public_state": deepcopy(public), "public_observation_token": "fetch-public-token",
        "simulation_internal": {"hidden_card_ids": [123]}}
    try:
        state, token = bridge.snapshot()
        obs = observation(state, "run", "episode")
        assert token == "fetch-public-token"
        assert obs["state"]["combat"]["discard"] == rows
        assert [row["fetch_played_this_turn"] for row in rows] == [True, False, None]
        assert all(row["known_instance_ids"] == [] for row in obs["state"]["combat"]["discard"])
        assert obs["state"]["combat"]["hand"][0]["fetch_played_this_turn"] is True
        assert bridge.simulation_internal is None
    finally:
        bridge.client.close()


def test_structured_run_choices_survive_without_raw_or_private_siblings():
    """Native keys, prices, selection intent and map coordinates never require localized line parsing."""
    public = {"protocol_version": 1, "screen": "EVENT", "available_actions": ["choose_event_option"],
              "run": {"character": "静默猎手", "character_id": "SILENT", "act_index": 2, "act_count": 3,
                      "room_type": None, "is_last_act": False, "awaiting_second_boss": None,
                      "map_coord": {"row": 4, "col": 2}, "act_floor": 5,
                      "relic_ids": ["VAJRA", "ANCHOR"], "relic_is_melted": [False, True],
                      "relic_statuses": ["Active", None]},
              "reward": {"rewards": [{"i": 3, "reward_type": "Potion", "potion_id": "BLOCK_POTION",
                  "potion_slot_available": False, "potion_procurement_allowed": True, "potion_can_receive": False,
                  "visible": True, "successfully_selected": False},
                  {"i": 4, "reward_type": "Relic", "relic_id": "VAJRA"}]},
              "shop": {"cards": [{"i": 0, "price": 77, "stocked": True, "affordable": False}],
                       "relics": [{"i": 1, "price": 199}], "potions": [{"i": 2, "price": 55}]},
              "event": {"id": "NEOW", "options": [{"i": 0,
                  "option_id": "NEOW.pages.INITIAL.options.ARCANE_SCROLL",
                  "text_key": "NEOW.pages.INITIAL.options.ARCANE_SCROLL", "label": "可见选项",
                  "label_loc_table": "events", "label_loc_key": "NEOW.pages.INITIAL.options.ARCANE_SCROLL.title",
                  "description_loc_table": "events",
                  "description_loc_key": "NEOW.pages.INITIAL.options.ARCANE_SCROLL.description",
                  "label_template": "可见选项", "description_template": "失去{HpLoss}点生命。",
                  "description": "失去7点生命。", "preview_relic_ids": ["ARCANE_SCROLL"]}]},
              "map": {"options": [{"i": 3, "coord": {"row": 4, "col": 2}, "node_type": "Monster"}]},
              "combat": {"player": {"power_details": [{"power_id": "SURROUNDED_POWER", "facing": "Left"}]},
                  "energy_spent_this_turn": 2, "hp_loss_events_this_turn": 1, "hp_loss_events_this_combat": 4,
                  "doom_applied_this_turn": False, "cards_exhausted_this_turn": 1, "cards_discarded_this_turn": 0,
                  "cards_drawn_by_effect_this_turn": 2, "status_cards_drawn_this_turn": 0, "cards_drawn_this_combat": 7,
                  "lightning_orbs_channeled_this_combat": 2, "stars_gained_this_turn": 0, "osty_attacks_this_turn": 0,
                  "powered_hits_by_target_this_turn": [{"instance_id": "enemy-2", "hits": 2}],
                  "ally_powered_hits_by_target_this_turn": [], "generated_cards_this_combat": None,
                  "energy_wasted_this_combat": None,
                  "unspent_energy_at_turn_end_sum": None,
                  "public_card_play_history": {"plays": [{"sequence": 2, "known_instance_id": "card-1",
                      "was_ethereal": False, "energy_value": 0, "energy_spent": 0, "stars_spent": None}]},
                  "public_card_block_history": {"available": True, "complete": True,
                  "scope": "current_combat", "source": "native_local_card_block_gains",
                  "entries": [{"this_turn": True, "card_play_index": 2, "amount": 5}]},
                  "draw_knowledge": {"schema_version": 1, "available": True, "reason": None,
                  "source": "native_public_hook_observer", "kind": "combat_reshuffle",
                  "bands": [{"cards": [{"card_ids": ["STRIKE_IRONCLAD"], "card_count": 1,
                                         "known_instance_ids": ["card-1"]}]}],
                  "inserted": [], "discard_bands": []},
                  "draw": [{"card_ids": ["STRIKE_IRONCLAD"], "card_count": 2,
                  "energy_cost": 0, "known_instance_ids": ["card-1"]}]},
              "selection": {"kind": "deck_upgrade_select", "operation": "upgrade", "prompt_key": "TO_UPGRADE",
                            "source_capture": "unavailable", "source_energy_paid": None, "source_stars_paid": None,
                            "can_skip": True, "enchantment_id": None, "enchantment_capture": "no_visible_preview_match",
                            "cards": [{"i": 0, "instance_id": "card-1", "source_pile": "Draw",
                                       "source_index": None, "selectable": None}]}}
    payload = {"public_state": public, "public_observation_token": "native-public-token",
               "state": {"seed": "hidden"}, "simulation_internal": {"rng": {"run_seed": 99}}}
    bridge = Bridge({"bridge": {"base_url": "http://127.0.0.1:1"}})

    def request(method, path, **kwargs):
        """Emulate only the public default route without opening a native game or model connection."""
        assert (method, path, kwargs) == ("GET", "/decision-snapshot", {})
        return deepcopy(payload)

    bridge._request = request
    try:
        state, token = bridge.snapshot()
        obs = observation(state, "run", "episode")
        assert obs["state"] == public
        assert token == "native-public-token"
        assert bridge.simulation_internal is None
        assert "state" not in state and "simulation_internal" not in state
        obs["state"]["map"]["options"][0]["coord"]["row"] = 99
        assert public["map"]["options"][0]["coord"]["row"] == 4
    finally:
        bridge.client.close()

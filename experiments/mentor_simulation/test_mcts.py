"""Exercise the production MCTS contract using real rule transitions and terminal statistics."""
from copy import deepcopy

import pytest

from test_public_adapter import client, frame, public_card
from scenarios import enemy


def request_search(client, state, **options):
    """Keep the test search reproducible while preserving production rollout depth."""
    return client.request({"op":"search", "strategy":"mcts", "public_state":state,
        "simulations":256, "max_rollout_depth":200, "scenario_seed":41,
        "timeout_ms":8000, **options})


def test_mcts_defaults_use_deployed_budget_and_deep_rollouts(client):
    """The published default names the actual serving lane instead of legacy depth ten."""
    result=client.request({"op":"metadata"})
    assert result["mcts"]["simulations"]==1000
    assert result["mcts"]["boss_simulations"]==4000
    assert result["mcts"]["max_rollout_depth"]==200
    assert "mcts" in result["source_hashes"]


def test_fire_potion_search_targets_second_enemy_to_survive_and_win(client):
    """Only potioning slot eight permits the next-turn Strike to finish harmless slot three."""
    state=frame()
    first=enemy(hp=6,damage=0)
    second=enemy(hp=20,damage=12)
    first["i"],second["i"]=3,8
    state["combat"]["enemies"]=[first,second]
    state["combat"]["player"].update(hp="1/80",energy=0)
    state["combat"]["draw"]=[{"card_ids":["STRIKE_IRONCLAD"],"line":"STRIKE_IRONCLAD [1]"}]
    state["run"]["potions"]=[{"i":2,"potion_id":"FIRE_POTION","occupied":True,
        "usable":True,"target":"enemies","targets":[3,8]}]
    result=request_search(client,state)
    assert result["ok"],result
    action=result["best"]["actions"][0]
    assert action["type"]=="use_potion" and action["target_index"]==8,result
    assert result["best"]["mcts"]["wins"]>0,result
    stats=result["best"]["mcts"]
    # Suboptimal rollout continuations can still die after the correct first action.
    assert stats["hp_sum"]==stats["wins"],result
    assert stats["resolved_hp_samples"]==stats["visits"],result
    assert stats["mean_final_hp"]==pytest.approx(stats["wins"]/stats["visits"]),result


def test_public_search_ignores_input_unknown_draw_order(client):
    """Input row order cannot change an honest search's recommendations or terminal counters."""
    state=frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"]=[{"card_ids":[card],"line":card} for card in ("STRIKE_IRONCLAD","DEFEND_IRONCLAD")]
    other=deepcopy(state)
    other["combat"]["draw"].reverse()
    left=request_search(client,state,simulations=64)
    right=request_search(client,other,simulations=64)
    assert left["ok"] and right["ok"],(left,right)
    assert left["best"]["actions"]==right["best"]["actions"]
    assert left["mcts"]["wins"]==right["mcts"]["wins"]
    assert left["mcts"]["losses"]==right["mcts"]["losses"]


def test_internal_permission_missing_snapshot_does_not_fall_back(client):
    """An authorized private search still needs the matching private snapshot."""
    result=request_search(client,frame(),read_access="internal")
    assert not result["ok"] and result["best"] is None,result
    assert "internal_state_unavailable" in result["support"]["blocking_reasons"]


def test_fixed_prefix_continues_to_terminal_and_reports_real_counts(client):
    """A declared immediate lethal is a real win, whereas an unfinished rollout stays unresolved."""
    state=frame([public_card("STRIKE_IRONCLAD",target="enemies")])
    state["combat"]["enemies"]=[enemy(hp=6)]
    result=client.request({"op":"evaluate","rollout":True,"public_state":state,"samples":1,
        "simulations":64,"candidate_plans":[{"id":"lethal","actions":[
            {"type":"play_card","card_index":0,"target_index":0}]}]})
    candidate=result["candidates"][0]
    assert candidate["status"]=="estimated",result
    assert candidate["mcts"]["wins"]==1 and candidate["mcts"]["losses"]==0,result
    assert candidate["mcts"]["p_win"]==1,result
    assert candidate["mcts"]["cutoffs"]==0,result


def test_declared_multi_choice_is_resolved_atomically(client):
    """A two-of-three discard is a legal complete set and must draw exactly two cards once."""
    state=frame([public_card("DEFEND_IRONCLAD",index=index) for index in range(3)])
    state["combat"]["draw"]=[{"card_ids":["ANGER"],"line":"ANGER*2 [0]"}]
    state["run"]["potions"]=[{"i":0,"potion_id":"GAMBLERS_BREW","occupied":True,
        "usable":True,"target":None,"targets":[]}]
    result=client.request({"op":"evaluate","rollout":True,"public_state":state,"samples":1,
        "simulations":32,"candidate_plans":[{"id":"discard-two","actions":[
            {"type":"use_potion","option_index":0},
            {"type":"select_card","choice_indices":[0,1]}]}]})
    sample=result["candidates"][0]["samples"][0]
    assert sample["status"]=="completed",result
    assert sample["state"]["pending_choice"] is None,result
    assert sum(card["card_id"]=="ANGER" for card in sample["state"]["hand"])==2,result


def test_havoc_unknown_top_reaches_both_real_terminal_outcomes(client):
    """An unknown Strike wins and an unknown Defend loses, each with one-half draw probability."""
    state=frame([public_card("HAVOC")])
    state["combat"]["player"].update(hp="1/80",energy=1)
    state["combat"]["enemies"]=[enemy(hp=6,damage=12)]
    state["combat"]["draw"]=[{"card_ids":[card],"line":card} for card in
        ("STRIKE_IRONCLAD","DEFEND_IRONCLAD")]
    result=request_search(client,state,simulations=1000)
    assert result["ok"],result
    assert result["best"]["actions"][0]["card_id"]=="HAVOC",result
    stats=result["best"]["mcts"]
    assert stats["wins"]>0 and stats["losses"]>0,result
    assert stats["cutoffs"]==0,result
    assert stats["p_win"]==pytest.approx(0.5,abs=0.15),result


def test_fixed_prefix_world_probabilities_do_not_depend_on_continuation_visit_budget(client):
    """One-half unknown-top win probability must not shrink when losing worlds receive more visits."""
    state=frame([public_card("HAVOC")])
    state["combat"]["player"].update(hp="1/80",energy=1)
    state["combat"]["enemies"]=[enemy(hp=6,damage=12)]
    state["combat"]["draw"]=[{"card_ids":[card],"line":card} for card in
        ("STRIKE_IRONCLAD","DEFEND_IRONCLAD")]
    candidates=[]
    for budget in (120,1200):
        result=client.request({"op":"evaluate","rollout":True,"public_state":state,"samples":12,
            "scenario_seed":41,"simulations":budget,"candidate_plans":[{"id":"havoc",
                "actions":[{"type":"play_card","card_index":0}]}]})
        candidate=result["candidates"][0]
        assert candidate["status"]=="estimated",result
        won_worlds=sum(sample["state"]["enemies"][0]["hp"]["current"]<=0
            for sample in candidate["samples"])
        expected=won_worlds/12
        assert 0<won_worlds<12,result
        assert expected==pytest.approx(0.5,abs=0.15),result
        assert candidate["mcts"]["p_win"]==pytest.approx(expected),result
        assert candidate["mcts"]["mean_final_hp"]==pytest.approx(expected),result
        assert candidate["mcts"]["p_unresolved"]==0,result
        candidates.append(candidate)
    assert candidates[0]["mcts"]["visits"]!=candidates[1]["mcts"]["visits"]
    assert candidates[0]["mcts"]["p_win"]==candidates[1]["mcts"]["p_win"]
    # Upstream accumulates returns in f32; larger visit counts introduce sub-micro rounding drift.
    assert candidates[0]["mcts"]["mean_value"]==pytest.approx(candidates[1]["mcts"]["mean_value"],abs=1e-6)

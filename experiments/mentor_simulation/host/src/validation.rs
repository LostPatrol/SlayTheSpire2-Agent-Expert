//! Offline policy trials keep one hypothetical combat world alive across every decision.
use crate::public::PublicPosition;
use serde_json::{json, Value};
use std::time::{Duration, Instant};
use sts2_core::{Action, CombatState, StepResult};

// A trial is an offline diagnostic, bounded independently of one production recommendation.
const DEFAULT_DECISIONS: u64 = 200;
// Keep accidentally cyclic scenarios observable as cutoffs instead of wedging the runner.
const MAX_DECISIONS: u64 = 2_000;
// This wall deadline is cooperative; a single core transition cannot be interrupted here.
const DEFAULT_WALL_MS: u64 = 120_000;

/// Derive reproducible policy randomness without reading or consuming the world's RNG.
fn mixed(seed: u64, index: u64) -> u64 {
    let mut value = seed.wrapping_add(index.wrapping_mul(0x9e37_79b9_7f4a_7c15));
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

/// Export current resources and native core terminal flags, including newly spawned enemies.
fn endpoint(state: &CombatState) -> Value {
    json!({"round":state.round,"phase":format!("{:?}",state.phase),
        "terminal":sts2_core::is_terminal(state),"player_won":state.player_won,
        "hp":state.player.current_hp,"block":state.player.block,"energy":state.player.energy,
        "stars":state.player.stars,"hand":state.player.hand.len(),
        "hand_ids":state.player.hand.iter().map(|uid|{
            let card=&state.cards[uid.0.get() as usize-1];
            format!("{}{}",crate::canonical_public_card_id(card.card_id),if card.upgraded {"+"} else {""})
        }).collect::<Vec<_>>(),
        "draw":state.player.draw.len(),"discard":state.player.discard.len(),
        "exhaust":state.player.exhaust.len(),"pending_choice":state.pending_choice.is_some(),
        "enemies":state.enemy_uids.iter().filter_map(|uid|state.creatures.iter()
            .find(|creature|creature.id==uid.0).map(|creature|json!({"uid":creature.id.get(),
                "kind":format!("{:?}",creature.species),"hp":creature.current_hp,
                "block":creature.block,"powers":creature.powers.iter().filter_map(|uid|
                    state.powers.get_by_id(uid.0).map(|power|json!({"kind":format!("{:?}",power.kind),"amount":power.amount})))
                    .collect::<Vec<_>>()}))).collect::<Vec<_>>()})
}

/// One-step HCE baseline deliberately uses no tree search or hidden-world lookahead.
fn greedy(state: &CombatState, legal: &[Action], seed: u64) -> Action {
    // Match the public arm: a one-step baseline must not peek at the trial world's future draw.
    let mut blinded=state.clone();
    sts2_core::blindfold::belief_root(&mut blinded,seed);
    legal.iter().max_by(|left,right| {
        let score = |action:&Action| {
            let mut next=blinded.clone();
            if sts2_core::apply(&mut next,action.clone())==StepResult::InvalidTarget {
                return f32::NEG_INFINITY;
            }
            if sts2_core::is_terminal(&next) {
                return if next.player_won {1_000_000.0} else {-1_000_000.0};
            }
            sts2_core::evaluate_in_progress(&next)
        };
        score(left).total_cmp(&score(right))
    }).expect("nonempty legal actions checked by trial").clone()
}

/// Play a continuous sampled world with repeated public-boundary MCTS or explicit baselines.
pub fn combat_trial(request: &Value) -> Result<Value,String> {
    let started=Instant::now();
    let seed=request["seed"].as_u64().unwrap_or(0);
    let policy=request["policy"].as_str().unwrap_or("mcts");
    if !["mcts","random","one_step_hce","scripted"].contains(&policy) {return Err("unknown_trial_policy".into());}
    let max_decisions=request["max_decisions"].as_u64().unwrap_or(DEFAULT_DECISIONS);
    if max_decisions==0 || max_decisions>MAX_DECISIONS {return Err("trial_decision_limit_out_of_range".into());}
    let wall_ms=request["trial_timeout_ms"].as_u64().unwrap_or(DEFAULT_WALL_MS).min(600_000);
    let deadline=started+Duration::from_millis(wall_ms);
    let source=request.get("public_state").ok_or("missing_public_state")?;
    // Complete wire initializes the truth world; only independent search clones are blinded.
    let wire=request.get("wire_state").or_else(||source.get("wire_state"))
        .or_else(||request["internal_state"].get("wire_state")).ok_or("trial_complete_wire_required")?;
    let state=sts2_wire::state_from_json(wire).map_err(|error|error.to_string())?;
    let mut position=PublicPosition::from_core(state,source,request["read_access"]=="internal");
    let support=position.search_support();
    if support["blocking_reasons"].as_array().is_some_and(|reasons|!reasons.is_empty()) {
        return Ok(json!({"ok":false,"status":"unsupported","support":support}));
    }
    let initial=endpoint(&position.state);
    let mut trace=Vec::new();
    let mut reason="decision_cap".to_owned();
    let mut simulations=0_u64;
    let mut rollouts=0_u64;
    let mut depth_cuts=0_u64;
    for step in 0..max_decisions {
        if sts2_core::is_terminal(&position.state) {reason="terminal".into();break;}
        if Instant::now()>=deadline {reason="wall_deadline".into();break;}
        let legal=sts2_core::legal_actions(&position.state);
        if legal.is_empty() {reason="no_legal_actions_nonterminal".into();break;}
        let search_seed=mixed(seed^0x504f_4c49_4359,step+1);
        let action=if policy=="scripted" {
            match request["actions"].get(step as usize) {
                Some(action)=>position.decode_search_action(action)?,
                None=>{reason="scripted_prefix_complete".into();break;}
            }
        } else if policy=="mcts" {
            let mut honest=position.state.clone();
            sts2_core::blindfold::belief_root(&mut honest,search_seed);
            let mut controls=request.clone();
            controls["scenario_seed"]=json!(search_seed);
            controls["read_access"]=json!("public");
            let cfg=crate::mcts::config(&controls,&honest,search_seed)?;
            match crate::mcts::run_with_request(&honest,cfg,deadline,&controls) {
                Ok((search,action,spent,_))=>{
                    simulations+=u64::from(spent);
                    let (count,cuts)=search.rollout_stats();
                    rollouts+=count;depth_cuts+=cuts;
                    action
                },
                Err(error)=>{reason=format!("search_error:{error}");break;}
            }
        } else if policy=="random" {
            legal[(search_seed%legal.len() as u64) as usize].clone()
        } else {greedy(&position.state,&legal,search_seed)};
        if !legal.contains(&action) {reason="search_returned_illegal_action".into();break;}
        let before=endpoint(&position.state);
        let encoded=position.encode_search_action(&action);
        let result=sts2_core::apply(&mut position.state,action.clone());
        trace.push(json!({"decision":step,"before":before,"action":encoded,
            "core_action":format!("{action:?}"),"legal_action_count":legal.len(),
            "result":format!("{result:?}"),"after":endpoint(&position.state)}));
        if result==StepResult::InvalidTarget {reason="legal_action_rejected_by_core".into();break;}
    }
    let final_state=endpoint(&position.state);
    if final_state["terminal"]==true {reason="terminal".into();}
    let status=if reason=="terminal" {"terminal"} else if ["decision_cap","wall_deadline","scripted_prefix_complete"].contains(&reason.as_str()) {"cutoff"} else {"error"};
    Ok(json!({"ok":status!="error","status":status,"stop_reason":reason,
        "scope":"continuous_hypothetical_combat_policy_not_native_parity_or_full_run_winrate",
        "policy":policy,"world_seed":seed,"initial":initial,"final":final_state,
        "decisions":trace.len(),"simulations_spent":simulations,"rollouts":rollouts,
        "depth_cap_hits":depth_cuts,"trace":trace,"support":support,
        "elapsed_ms":started.elapsed().as_secs_f64()*1000.0}))
}

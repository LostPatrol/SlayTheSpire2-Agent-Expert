//! Independent JSONL diagnostics compiled against an untouched pinned upstream checkout.
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use sts2_runai::ppo::encoder::{empty_cat, featurize_with, Featurized};
use sts2_runai::ppo::features::Features;
use sts2_runai::ppo::net::PpoNet;
use sts2_runai::ppo::obs::{ActionDesc, PpoObs};
use sts2_runai::ppo::policy::PpoPolicy;
use sts2_runai::ppo::wire::WireDecision;

// This source pin is independently checked by prepare_reference_probe.py before compilation.
const UPSTREAM_COMMIT: &str = "28429066d91ed05c10b7b57597fc624196d197a4";

/// Expose the exact reusable encoder output before any graph-specific width truncation.
fn features_json(feat: &Featurized) -> Value {
    json!({"obs_cat":feat.obs_cat,"obs_cont":feat.obs_cont,"obs_sec":feat.obs_sec,
        "act_cat":feat.act_cat,"act_cont":feat.act_cont,"act_sec":feat.act_sec,
        "n_actions":feat.n_actions})
}

/// Pad the same token arrays as upstream PpoNet::infer to expose active raw logits.
fn raw_logits(net: &PpoNet, feat: &Featurized) -> Result<(Vec<f32>, f32), String> {
    let observations=feat.obs_cat.len();
    let actions=feat.n_actions;
    if observations+actions>net.t_max() || actions>net.a_max() {
        return Err("reference_probe_export_budget_exceeded".into());
    }
    let cat_width=net.cat_dim();
    let cont_width=net.cont_dim();
    let mut cat=Vec::with_capacity(net.t_max()*cat_width);
    let mut cont=vec![0.0_f32;net.t_max()*cont_width];
    let mut sections=vec![0_i64;net.t_max()];
    let pad=empty_cat();
    for row in 0..net.t_max() {
        let token=if row<observations {&feat.obs_cat[row]}
            else if row<observations+actions {&feat.act_cat[row-observations]} else {&pad};
        cat.extend_from_slice(&token[..cat_width]);
    }
    for (index,row) in feat.obs_cont.iter().chain(feat.act_cont.iter()).enumerate() {
        cont[index*cont_width..(index+1)*cont_width].copy_from_slice(&row[..cont_width]);
    }
    sections[..observations].copy_from_slice(&feat.obs_sec);
    sections[observations..observations+actions].copy_from_slice(&feat.act_sec);
    let mut padding=vec![true;net.t_max()];
    padding[..observations+actions].fill(false);
    let mut action_mask=vec![false;net.a_max()];
    action_mask[..actions].fill(true);
    let (mut logits,value)=net.forward_raw(&cat,&cont,&sections,&padding,observations as i64,&action_mask)?;
    logits.truncate(actions);
    Ok((logits,value))
}

/// Answer explicit observations or the upstream wire adapter without consulting any model API.
fn handle(request: &Value) -> Result<Value,String> {
    if request["op"]=="metadata" {
        return Ok(json!({"ok":true,"upstream_commit":UPSTREAM_COMMIT,
            "source_hashes":{"core":sts2_core::SRC_HASH,"content":sts2_content::SRC_HASH,
                "runai":sts2_runai::SRC_HASH},"backend":"pristine_upstream_reference"}));
    }
    if request["op"]=="value_diagnostics" {
        let path=request["value_weights_path"].as_str().ok_or("missing_value_weights_path")?;
        #[cfg(feature="legacy-prefix")]
        let model=if request["legacy_prefix"]==true {
            sts2_mcts::value_net::LearnedValueNet::from_legacy_prefix_weights_path(std::path::Path::new(path))
        }else{sts2_mcts::value_net::LearnedValueNet::from_weights_path(std::path::Path::new(path))};
        #[cfg(not(feature="legacy-prefix"))]
        let model=sts2_mcts::value_net::LearnedValueNet::from_weights_path(std::path::Path::new(path));
        // A pristine schema rejection is preserved before any fixture construction or adaptation.
        let model=model.map_err(|error|format!("value_model_load:{error}"))?;
        let state=sts2_wire::state_from_wire(&&request["wire_state"]).map_err(|error|format!("wire_state:{error}"))?;
        let features=sts2_core::eval_features_wide(&state);
        let (value,policy)=model.evaluate_with_policy(&state);
        return Ok(json!({"ok":true,"legacy_prefix":cfg!(feature="legacy-prefix")&&request["legacy_prefix"]==true,
            "wide_features":features.to_vec(),"value":value,"policy":policy,"wide_feature_dim":sts2_core::WIDE_FEATURE_DIM,
            "source_hashes":{"core":sts2_core::SRC_HASH,"content":sts2_content::SRC_HASH,"runai":sts2_runai::SRC_HASH,"mcts":sts2_mcts::SRC_HASH}}));
    }
    let (obs,actions,responses,labels):(PpoObs,Vec<ActionDesc>,Vec<Value>,Vec<String>)=
        if request["op"]=="wire" {
            let surface=request["surface"].as_str().ok_or("missing_surface")?;
            let decision=sts2_runai::ppo::wire::build_decision(&request["wire"],surface)
                .ok_or("upstream_wire_decision_unavailable")?;
            (decision.obs,decision.actions,decision.responses,decision.labels)
        } else {
            (serde_json::from_value(request["obs"].clone()).map_err(|error|format!("obs:{error}"))?,
             serde_json::from_value(request["actions"].clone()).map_err(|error|format!("actions:{error}"))?,
             vec![],vec![])
        };
    let features=Features::try_parse(request["features"].as_str().unwrap_or(""))?;
    let feat=featurize_with(&obs,&actions,features);
    let mut result=json!({"ok":true,"obs":obs,"actions":actions,"responses":responses,
        "labels":labels,"features":features_json(&feat),"source_hashes":{
            "core":sts2_core::SRC_HASH,"content":sts2_content::SRC_HASH,"runai":sts2_runai::SRC_HASH}});
    if let Some(path)=request["model_path"].as_str() {
        let model=PpoNet::load(std::path::Path::new(path))?;
        let (probs,value)=model.infer(&feat)?;
        let (logits,raw_value)=raw_logits(&model,&feat)?;
        let decision=WireDecision {obs,actions,responses:vec![],labels:vec![]};
        let policy=PpoPolicy::from_path_with(std::path::Path::new(path),features);
        let grouped=policy.evaluate_wire(&decision).map(|(_,_,pick)|pick);
        let raw_pick=probs.iter().enumerate().fold(None,|best:Option<(usize,f32)>,(index,value)|
            if best.is_none_or(|(_,current)|*value>current) {Some((index,*value))} else {best}).map(|(index,_)|index);
        result["network"]=json!({"logits":logits,"probs":probs,"value":value,"raw_value":raw_value,
            "grouped_argmax":grouped,"raw_argmax":raw_pick,"shape":{"cat_dim":model.cat_dim(),
                "cont_dim":model.cont_dim(),"t_max":model.t_max(),"a_max":model.a_max()}});
    }
    Ok(result)
}

/// Retain malformed requests and inference failures as explicit JSON responses.
fn main() {
    sts2_content::cards::dispatch::ensure_installed();
    let stdin=io::stdin();
    let mut stdout=io::stdout().lock();
    for line in stdin.lock().lines() {
        let line=match line {Ok(line)=>line,Err(error)=>{
            let _=writeln!(stdout,"{}",json!({"ok":false,"error":error.to_string()}));
            break;
        }};
        let output=std::panic::catch_unwind(|| {
            let request:Value=serde_json::from_str(&line).map_err(|error|error.to_string())?;
            handle(&request)
        });
        let response=match output {
            Ok(Ok(response))=>response,
            Ok(Err(error))=>json!({"ok":false,"error":error}),
            Err(_)=>json!({"ok":false,"error":"pristine_upstream_panic"}),
        };
        if writeln!(stdout,"{response}").and_then(|_|stdout.flush()).is_err() {break;}
    }
}

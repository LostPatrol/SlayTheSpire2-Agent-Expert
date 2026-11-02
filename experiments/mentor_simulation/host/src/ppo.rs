//! Upstream PPO inference over public scene observations and explicit alignment payloads.
//! Network weights, encoder features, native action indices and follow-up targets stay paired.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use sts2_core::potions::CharacterId;
use sts2_runai::ppo::{
    encoder,
    features::Features,
    net::{self, PpoNet},
    obs::{ActionDesc, PpoObs},
    policy::PpoPolicy,
    wire::{self, WireDecision},
};
#[path = "ppo_batch.rs"]
mod batch;
#[path = "ppo_event_world.rs"]
mod event_world;
#[path = "ppo_transform.rs"]
mod transform;

// Keep each loaded champion and its diagnostic view alive for the JSONL process.
static MODELS: OnceLock<Mutex<HashMap<String, Loaded>>> = OnceLock::new();
// The pinned public English table translates only text whose displayed bindings are recoverable.
const EVENT_ENGLISH: &str =
    include_str!("../../../../vendor/spire-mentor/sts2-pycli/localization_eng/events.json");
// The original manifest, not filename globs, declares the deployed champion identities.
const CHAMPIONS: &str =
    include_str!("../../../../vendor/spire-mentor/sts2-pycli/data/ppo/champions.toml");
// Public uncertainty evaluates every sampled world rather than substituting unknown effects with zero.
const DEFAULT_UNCERTAINTY_WORLDS: usize = 32;
// Bound an explicitly requested ensemble without limiting ordinary deck or action counts.
const MAX_UNCERTAINTY_WORLDS: usize = 256;
// Keep the sampler's independent RNG domain separate from every actual run seed and combat search.
const PUBLIC_NEOW_WORLD_SALT: u64 = 0x504F_4E45_4F57_5055;
// This internal signal requests public sampling; every other adapter error remains explicit.
const NEOW_UNCERTAINTY_REQUIRED: &str = "ppo_neow_effect_requires_public_uncertainty:";
// Other audited random event pickups use separate public conditioning domains while sharing the NN ensemble.
const EVENT_UNCERTAINTY_REQUIRED: &str = "ppo_event_effect_requires_public_uncertainty:";
// Batched transforms hide earlier replacements until native confirmation, requiring a public prior.
const TRANSFORM_UNCERTAINTY_REQUIRED: &str = "ppo_transform_effect_requires_public_uncertainty:";
// Transform continuations must not reuse the event-reward sampler's established world domain.
const PUBLIC_TRANSFORM_WORLD_SALT: u64 = 0x5050_4F54_5241_4E53;

#[derive(Clone, Copy)]
struct NeowWorld {
    seed: u64,
    full_unlock: bool,
}

struct Loaded {
    policy: PpoPolicy,
    diagnostic_net: Option<PpoNet>,
    model_bytes: Vec<u8>,
    features: Features,
    identity: Value,
}

struct Prepared {
    decision: WireDecision,
    contexts: Vec<Value>,
    warnings: Vec<String>,
    queued_effects: Value,
}

/// Read the pinned manifest's simple quoted fields without selecting another available model.
fn manifest_field(character: CharacterId, field: &str) -> Option<String> {
    let section = format!("[{}]", format!("{character:?}").to_ascii_lowercase());
    let mut active = false;
    for line in CHAMPIONS.lines().map(str::trim) {
        if line.starts_with('[') {
            active = line == section;
            continue;
        }
        if active {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() == field {
                    return value.split('"').nth(1).map(str::to_string);
                }
            }
        }
    }
    None
}

/// Advertise actual and expected five-character assets without initializing tract inference.
pub fn model_manifest() -> Value {
    static MANIFEST: OnceLock<Value> = OnceLock::new();
    MANIFEST.get_or_init(|| {
        let characters=[CharacterId::Ironclad,CharacterId::Silent,CharacterId::Defect,CharacterId::Regent,CharacterId::Necrobinder];
        let models=characters.into_iter().map(|character| {
            let filename=net::champion_onnx_filename(character);
            let deployed=std::env::current_exe().ok().and_then(|path|path.parent().map(|root|root.join("models/ppo").join(filename)));
            let path=deployed.filter(|path|path.is_file()).or_else(||net::onnx_model_path_for(character));
            let actual=path.as_ref().and_then(|path|std::fs::read(path).ok()).map(|bytes|format!("{:x}",Sha256::digest(bytes)));
            let expected=manifest_field(character,"sha256");
            json!({"character":format!("{character:?}"),"filename":filename,"path":path.map(|path|path.to_string_lossy().to_string()),
                "expected_sha256":expected,"actual_sha256":actual,"available":actual.is_some(),"matches_manifest":actual.is_some()&&actual==expected,
                "features":manifest_field(character,"features")})
        }).collect::<Vec<_>>();
        json!({"implementation":"sts2-runai_PpoPolicy_tract","decision_pipeline":"upstream_wire_advisor","models":models,
            "public_event_localization_sha256":format!("{:x}",Sha256::digest(EVENT_ENGLISH.as_bytes()))})
    }).clone()
}

/// Read an optional string without coercing other public field types.
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Iterate only an explicitly present array.
fn rows<'a>(v: &'a Value, key: &str) -> &'a [Value] {
    v.get(key)
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

/// Preserve the bridge's actual handle, including sparse indices.
fn index(v: &Value) -> Result<i64, String> {
    v["i"]
        .as_i64()
        .ok_or_else(|| "ppo_public_option_index_missing".into())
}

/// Resolve canonical IDs and the established legacy character labels.
fn character(value: &str) -> Result<CharacterId, String> {
    match value.to_ascii_uppercase().as_str() {
        "IRONCLAD" | "THE IRONCLAD" | "铁甲战士" | "铁甲" => Ok(CharacterId::Ironclad),
        "SILENT" | "THE SILENT" | "静默猎手" => Ok(CharacterId::Silent),
        "DEFECT" | "THE DEFECT" | "故障机器人" => Ok(CharacterId::Defect),
        "REGENT" | "THE REGENT" | "储君" => Ok(CharacterId::Regent),
        "NECROBINDER" | "THE NECROBINDER" | "亡灵契约师" => Ok(CharacterId::Necrobinder),
        _ => Err(format!("ppo_unknown_character:{value}")),
    }
}

/// Select the champion belonging to this observation, never another character's fallback.
fn request_character(request: &Value) -> Result<CharacterId, String> {
    let run = &request["public_state"]["run"];
    let value = request
        .get("character_id")
        .or_else(|| request.get("character"))
        .and_then(Value::as_str)
        .or_else(|| run.get("character_id").and_then(Value::as_str))
        .or_else(|| run.get("character").and_then(Value::as_str))
        .ok_or("ppo_character_missing")?;
    character(value)
}

/// Load the pinned champion at its own feature schema; explicit diagnostic models name theirs.
fn with_model<T>(
    request: &Value,
    f: impl FnOnce(&mut Loaded) -> Result<T, String>,
) -> Result<T, String> {
    let character = request_character(request)?;
    let filename = net::champion_onnx_filename(character);
    let path = if let Some(path) = request.get("model_path").and_then(Value::as_str) {
        PathBuf::from(path)
    } else if let Some(root) = request.get("model_dir").and_then(Value::as_str) {
        PathBuf::from(root).join(filename)
    } else {
        let deployed = std::env::current_exe()
            .map_err(|e| e.to_string())?
            .parent()
            .ok_or("ppo_executable_parent_missing")?
            .join("models/ppo")
            .join(filename);
        if deployed.is_file() {
            deployed
        } else {
            net::onnx_model_path_for(character).ok_or("ppo_champion_missing")?
        }
    };
    let features = request
        .get("features")
        .and_then(Value::as_str)
        .map(Features::try_parse)
        .unwrap_or_else(|| Ok(net::features_for(character)))?;
    let canonical = path
        .canonicalize()
        .map_err(|e| format!("ppo_model_unavailable:{}:{e}", path.display()))?;
    let key = format!("{}:{character:?}:{features:?}", canonical.display());
    let mut models = MODELS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .map_err(|_| "ppo_model_cache_poisoned")?;
    if !models.contains_key(&key) {
        let bytes = std::fs::read(&canonical).map_err(|e| format!("ppo_model_read:{e}"))?;
        let policy = PpoPolicy::from_bytes_with(&bytes, features)
            .map_err(|e| format!("ppo_model_load:{e}"))?;
        let identity = json!({
            "kind":if request.get("model_path").is_some(){"explicit_ppo_model"}else{"upstream_ppo_champion"}, "character":format!("{character:?}"),
            "path":canonical.to_string_lossy(), "filename":canonical.file_name().map(|name|name.to_string_lossy()), "champion_filename":filename,
            "sha256":format!("{:x}", Sha256::digest(&bytes)),
            "features":{"reward_upgrade":features.reward_upgrade,"card_mods":features.card_mods},
            "public_event_localization_sha256":format!("{:x}",Sha256::digest(EVENT_ENGLISH.as_bytes())),
            "group_decode":std::env::var("STS2_RUNAI_PPO_GROUP_DECODE").ok().as_deref()!=Some("0"),
            "decoder":"upstream_PpoPolicy_evaluate_wire"});
        // Live advice loads one graph; the second raw-logit graph is only for diagnostics.
        models.insert(
            key.clone(),
            Loaded {
                policy,
                diagnostic_net: None,
                model_bytes: bytes,
                features,
                identity,
            },
        );
    }
    f(models
        .get_mut(&key)
        .ok_or("ppo_model_cache_entry_missing")?)
}

/// Preserve unavailable states explicitly so direct mode cannot silently invoke another policy.
fn unsupported(reason: String) -> Value {
    json!({"ok":false,"status":"unsupported","best_action":null,"candidates":[],"next_context":null,
        "support":{"level":"unsupported","blocking_reasons":[reason.clone()]},"errors":[reason],
        "decision_pipeline":"upstream_wire_advisor"})
}

/// Recover structured card metadata and the documented visible aggregation count.
fn card(row: &Value) -> Result<Value, String> {
    let ids = rows(row, "card_ids");
    let id = row
        .get("card_id")
        .or_else(|| row.get("id"))
        .and_then(Value::as_str)
        .or_else(|| {
            if ids.len() == 1 {
                ids[0].as_str()
            } else {
                None
            }
        })
        .filter(|id| !id.is_empty())
        .ok_or("ppo_public_card_identity_missing_or_ambiguous")?;
    let upgraded = row
        .get("upgraded")
        .and_then(Value::as_bool)
        .unwrap_or_else(|| {
            text(row, "line")
                .split('[')
                .next()
                .unwrap_or("")
                .contains('+')
        });
    let mut out = json!({"id":wire::vocab_card_id(id),"upgraded":upgraded,"name":id});
    if let Some(id) = row["enchantment"]["enchantment_id"]
        .as_str()
        .filter(|id| !id.is_empty())
    {
        out["enchantment"] = json!(id);
        out["enchantment_amount"] = row["enchantment"]
            .get("amount")
            .cloned()
            .unwrap_or(json!(1));
    }
    Ok(out)
}

/// Expand the public multiset rather than mistaking aggregate row count for deck size.
fn deck(run: &Value) -> Result<Vec<Value>, String> {
    let source = run
        .get("deck")
        .and_then(Value::as_array)
        .ok_or("ppo_public_deck_missing")?;
    let mut out = Vec::new();
    for row in source {
        let c = card(row)?;
        let count = row
            .get("card_count")
            .and_then(Value::as_u64)
            .map(|n| n as usize)
            .unwrap_or_else(|| {
                text(row, "line")
                    .split('[')
                    .next()
                    .unwrap_or("")
                    .rsplit_once('*')
                    .and_then(|(_, n)| n.trim().parse::<usize>().ok())
                    .unwrap_or(1)
            });
        if count == 0 || count > 8192 {
            return Err("ppo_invalid_public_card_count".into());
        }
        out.extend(std::iter::repeat_n(c, count));
    }
    if let Some(expected) = run["deck_size"].as_u64() {
        if expected as usize != out.len() {
            return Err("ppo_public_deck_count_mismatch".into());
        }
    }
    Ok(out)
}

/// Parse the bridge's public HP pair without filling an unknown maximum.
fn hp(run: &Value) -> Result<(i64, i64), String> {
    let raw = text(run, "hp");
    let (left, right) = raw.split_once('/').ok_or("ppo_public_hp_missing")?;
    Ok((
        left.trim().parse().map_err(|_| "ppo_public_hp_invalid")?,
        right
            .trim()
            .parse()
            .map_err(|_| "ppo_public_max_hp_invalid")?,
    ))
}

/// Interpret only explicit map coordinate structures or their stable compact pair spelling.
fn coord(value: &Value) -> Result<Value, String> {
    if let (Some(row), Some(col)) = (value["row"].as_i64(), value["col"].as_i64()) {
        return Ok(json!({"row":row,"col":col}));
    }
    let (row, col) = value
        .as_str()
        .and_then(|v| v.split_once(','))
        .ok_or("ppo_public_map_coordinate_missing")?;
    Ok(
        json!({"row":row.trim().parse::<i64>().map_err(|_|"ppo_map_row_invalid")?,
        "col":col.trim().parse::<i64>().map_err(|_|"ppo_map_col_invalid")?}),
    )
}

/// Carry the complete already-public map graph into the upstream map feature builder.
fn map_wire(map: &Value, run: &Value, live_map: bool) -> Result<Value, String> {
    let mut nodes = Vec::new();
    for node in rows(map, "nodes") {
        let mut item = coord(&node["coord"])?;
        item["type"] = node
            .get("node_type")
            .or_else(|| node.get("type"))
            .cloned()
            .unwrap_or(Value::Null);
        if item["type"].as_str().is_none() {
            return Err("ppo_public_map_node_type_missing".into());
        }
        item["visited"] = node.get("visited").cloned().unwrap_or(json!(false));
        item["children"] = json!(rows(node, "children")
            .iter()
            .map(coord)
            .collect::<Result<Vec<_>, _>>()?);
        nodes.push(item);
    }
    // A cached graph preserves topology, never a stale cursor from an earlier room.
    let current = if let Some(value) = run.get("map_coord") {
        if value.is_null() {
            None
        } else {
            Some(coord(value)?)
        }
    } else if live_map {
        map.get("current")
            .filter(|v| !v.is_null())
            .map(coord)
            .transpose()?
    } else {
        None
    };
    let boss = map
        .get("boss_node")
        .filter(|v| !v.is_null())
        .map(coord)
        .transpose()?
        .map(|mut v| {
            v["id"] = run["boss_id"].clone();
            v
        });
    let second = map
        .get("second_boss_node")
        .filter(|v| !v.is_null())
        .map(coord)
        .transpose()?;
    Ok(json!({"nodes":nodes,"current":current,"boss":boss,"second_boss":second}))
}

/// Prefer the bridge's explicit one-based index while preserving its legacy zero-based act identifier.
fn public_act_index(run: &Value) -> Result<i64, String> {
    let explicit = run
        .get("act_index")
        .filter(|value| !value.is_null())
        .map(|value| {
            value
                .as_i64()
                .filter(|index| *index > 0)
                .ok_or("ppo_public_act_index_invalid")
        })
        .transpose()?;
    let legacy = text(run, "act_id")
        .parse::<i64>()
        .ok()
        .map(|index| index + 1);
    if let (Some(index), Some(old)) = (explicit, legacy) {
        if index != old {
            return Err("ppo_public_act_indices_conflict".into());
        }
    }
    explicit
        .or(legacy)
        .filter(|index| *index > 0)
        .ok_or("ppo_public_act_index_missing".into())
}

/// Adapt common public fields to the original upstream wire contract.
fn base_wire(request: &Value) -> Result<Value, String> {
    let source = &request["public_state"];
    let run = &source["run"];
    let (current, maximum) = hp(run)?;
    if maximum <= 0
        || run.get("relic_ids").and_then(Value::as_array).is_none()
        || run.get("potions").and_then(Value::as_array).is_none()
    {
        return Err("ppo_public_run_metadata_incomplete".into());
    }
    let act = public_act_index(run)?;
    let mut potions = Vec::new();
    for potion in rows(run, "potions") {
        let id = text(potion, "potion_id");
        if id.is_empty() && potion["occupied"] == true {
            return Err("ppo_occupied_potion_identity_missing".into());
        }
        potions.push(json!({"id":id,"empty":id.is_empty()}));
    }
    let live_map = source.get("map").filter(|m| !rows(m, "nodes").is_empty());
    let map = live_map
        .or_else(|| request.get("public_map"))
        .unwrap_or(&Value::Null);
    let public_current = run
        .get("map_coord")
        .filter(|value| !value.is_null())
        .map(coord)
        .transpose()?;
    Ok(
        json!({"type":"", "floor":run.get("floor").and_then(Value::as_i64).ok_or("ppo_public_floor_missing")?,"act":act,
        "run_state":{"master_deck":deck(run)?,"relics":rows(run,"relic_ids").iter().map(|r|json!({"id":r})).collect::<Vec<_>>(),
            "potions":potions,"ascension":run.get("ascension").and_then(Value::as_i64).ok_or("ppo_public_ascension_missing")?,
            "hp":current,"max_hp":maximum,"gold":run.get("gold").and_then(Value::as_i64).ok_or("ppo_public_gold_missing")?},
        "map":map_wire(map,run,live_map.is_some())?,"public_map_coord":public_current,
        "public_act_floor":run.get("act_floor"),"has_public_map_coord":run.get("map_coord").is_some(),
        "public_relic_state":{"ids":run["relic_ids"],"stacks":run["relic_stacks"],"is_melted":run["relic_is_melted"]},
        "public_current_coord_unknown":live_map.is_none()&&run.get("map_coord").is_none()&&!rows(map,"nodes").is_empty()}),
    )
}

/// Keep the original wire builder while adding newly exported current-room facts to its observation.
fn upstream_wire_decision(state: &Value, surface: &str) -> Option<WireDecision> {
    let mut decision = wire::build_decision(state, surface)?;
    if state["has_public_map_coord"] == true {
        decision.obs.map_coord = state
            .get("public_map_coord")
            .filter(|value| !value.is_null())
            .and_then(|value| {
                let row = value["row"].as_i64()?;
                let col = value["col"].as_i64()?;
                // Keep upstream's act-start row-zero graph guard.
                if row == 0 && decision.obs.map_coord.is_none() {
                    None
                } else {
                    Some((col, row))
                }
            });
    }
    let guarded_start = state["public_map_coord"]["row"] == 0 && decision.obs.map_coord.is_none();
    if !guarded_start {
        if let Some(floor) = state["public_act_floor"]
            .as_i64()
            .filter(|floor| *floor >= 0)
        {
            decision.obs.act_floor = floor;
        }
    }
    Some(decision)
}

/// Check the current native legal action names, not an assumed screen-wide superset.
fn legal(source: &Value, name: &str) -> bool {
    rows(source, "available_actions").iter().any(|v| v == name)
}

/// Bind follow-up card choices to the same public floor and exact card identity.
fn context(source: &Value, intent: &str, target: &Value, occurrence: usize) -> Value {
    json!({"origin":{"act_id":source["run"]["act_id"],"act_index":public_act_index(&source["run"]).ok(),"floor":source["run"]["floor"]},
        "intent":intent,"target":target,"occurrence":occurrence})
}

/// Keep modifiers alongside card identity when carrying a delayed shop-removal target.
fn same_card(a: &Value, b: &Value) -> bool {
    let identity = match (
        sts2_content::names::resolve_card_name(text(a, "id"), None),
        sts2_content::names::resolve_card_name(text(b, "id"), None),
    ) {
        (Ok(left), Ok(right)) => left.id == right.id,
        _ => a.get("id") == b.get("id"),
    };
    let enchantment = match (a["enchantment"].as_str(), b["enchantment"].as_str()) {
        (Some(left), Some(right)) => match (
            sts2_core::cards::EnchantmentId::from_wire_name(left),
            sts2_core::cards::EnchantmentId::from_wire_name(right),
        ) {
            (Some(left), Some(right)) => left == right,
            _ => left == right,
        },
        (None, None) => true,
        _ => false,
    };
    identity
        && enchantment
        && ["upgraded", "enchantment_amount"]
            .iter()
            .all(|key| a.get(key) == b.get(key))
}

/// Keep a reward page's deferred declines through its nested card-choice popup only.
fn nested_reward_context(request: &Value) -> Value {
    let source = &request["public_state"];
    let outer = &request["selection_context"];
    let carry = if outer["resume_context"].is_object() {
        &outer["resume_context"]
    } else {
        outer
    };
    let underlying = source["selection"]
        .get("underlying_screen")
        .or_else(|| source["modal"].get("underlying_screen"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let same_act = carry["origin"]["act_index"]
        .as_i64()
        .is_some_and(|index| public_act_index(&source["run"]).ok() == Some(index));
    if text(carry, "intent") == "reward_sequence"
        && same_act
        && carry["origin"]["floor"] == source["run"]["floor"]
        && (text(source, "screen") == "REWARD" || underlying == "REWARD")
        && carry["claim_after_discard"].is_null()
    {
        carry.clone()
    } else {
        Value::Null
    }
}

/// Remove visible formatting tags without discarding literal numeric brackets.
fn strip_tags(value: &str) -> String {
    let mut out = String::new();
    let mut remaining = value;
    while let Some(start) = remaining.find('[') {
        out.push_str(&remaining[..start]);
        let after = &remaining[start + 1..];
        if let Some(end) = after.find(']') {
            let name = after[..end].trim_start_matches('/');
            if name
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            {
                remaining = &after[end + 1..];
                continue;
            }
        }
        out.push('[');
        remaining = after;
    }
    out.push_str(remaining);
    out
}

/// Split plain variable slots; conditional/formatting DSLs remain explicitly unsupported.
fn template_parts(template: &str) -> Result<(Vec<String>, Vec<String>), String> {
    let mut parts = Vec::new();
    let mut names = Vec::new();
    let mut remaining = template;
    while let Some(start) = remaining.find('{') {
        parts.push(remaining[..start].to_string());
        let after = &remaining[start + 1..];
        let end = after.find('}').ok_or("ppo_locale_template_unclosed")?;
        let name = &after[..end];
        if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
            return Err("ppo_locale_template_expression_not_resolved".into());
        }
        names.push(name.to_string());
        remaining = &after[end + 1..];
    }
    parts.push(remaining.to_string());
    if names.len() > 32 {
        return Err("ppo_locale_template_too_complex".into());
    }
    Ok((parts, names))
}

/// Enumerate at most two complete bindings, preserving repeated-variable equality.
fn match_bindings(
    visible: &str,
    parts: &[String],
    names: &[String],
    slot: usize,
    start: usize,
    bindings: &mut HashMap<String, String>,
    solutions: &mut Vec<HashMap<String, String>>,
) {
    if solutions.len() > 1 {
        return;
    }
    if slot == names.len() {
        if start == visible.len() {
            solutions.push(bindings.clone());
        }
        return;
    }
    let literal = &parts[slot + 1];
    let ends = if literal.is_empty() {
        if slot + 1 == names.len() {
            vec![visible.len()]
        } else {
            return;
        }
    } else {
        visible[start..]
            .match_indices(literal)
            .map(|(idx, _)| idx + start)
            .collect::<Vec<_>>()
    };
    for end in ends {
        let value = &visible[start..end];
        let name = &names[slot];
        let existing = bindings.get(name).cloned();
        if existing.as_deref().is_some_and(|old| old != value) {
            continue;
        }
        bindings.insert(name.clone(), value.into());
        match_bindings(
            visible,
            parts,
            names,
            slot + 1,
            end + literal.len(),
            bindings,
            solutions,
        );
        if let Some(old) = existing {
            bindings.insert(name.clone(), old);
        } else {
            bindings.remove(name);
        }
        if solutions.len() > 1 {
            return;
        }
    }
}

/// Recover only variables uniquely demonstrated by the formatted text visible to the player.
fn visible_bindings(template: &str, visible: &str) -> Result<HashMap<String, String>, String> {
    let template = strip_tags(template);
    let visible = strip_tags(visible);
    let (parts, names) = template_parts(&template)?;
    if !visible.starts_with(&parts[0]) {
        return Err("ppo_locale_visible_template_mismatch".into());
    }
    let mut solutions = Vec::new();
    match_bindings(
        &visible,
        &parts,
        &names,
        0,
        parts[0].len(),
        &mut HashMap::new(),
        &mut solutions,
    );
    if solutions.len() != 1 {
        return Err("ppo_locale_visible_binding_not_unique".into());
    }
    Ok(solutions.remove(0))
}

/// Translate the same public text, never event-private variables or unshown roll outcomes.
fn english_option_text(option: &Value, field: &str) -> Result<String, String> {
    let visible = text(option, field);
    if !visible
        .chars()
        .any(|c| ('\u{3400}'..='\u{9fff}').contains(&c))
    {
        if visible.contains('{') {
            return Err("ppo_event_visible_text_unresolved".into());
        }
        return Ok(visible.into());
    }
    let template_key = format!("{field}_template");
    let loc_key = format!("{field}_loc_key");
    let loc_table = format!("{field}_loc_table");
    let template = option
        .get(&template_key)
        .and_then(Value::as_str)
        .ok_or("ppo_localized_event_template_missing")?;
    let key = option
        .get(&loc_key)
        .and_then(Value::as_str)
        .ok_or("ppo_localized_event_key_missing")?;
    if option.get(&loc_table).and_then(Value::as_str) != Some("events") {
        return Err("ppo_localized_event_table_not_supported".into());
    }
    static TABLE: OnceLock<Value> = OnceLock::new();
    let table = TABLE.get_or_init(|| {
        serde_json::from_str(EVENT_ENGLISH).expect("pinned event localization JSON")
    });
    let english = table
        .get(key)
        .and_then(Value::as_str)
        .ok_or("ppo_english_event_key_missing")?;
    let bindings = visible_bindings(template, visible)?;
    let (parts, names) = template_parts(english)?;
    let mut out = parts[0].clone();
    for (slot, name) in names.iter().enumerate() {
        let value = bindings
            .get(name)
            .ok_or("ppo_event_variable_not_publicly_recoverable")?;
        out.push_str(value);
        out.push_str(&parts[slot + 1]);
    }
    Ok(out)
}

/// Restore only public resources needed by audited event rules; no hidden room, reward or RNG state enters.
fn public_effect_run(
    state: &Value,
    character: CharacterId,
) -> Result<sts2_core::runstate::RunState, String> {
    use sts2_core::{runstate::RunState, RelicId};
    let public = &state["run_state"];
    sts2_content::relics::dispatch::install_default_registry();
    let mut run = RunState::new();
    run.character_id = character;
    run.game_version = sts2_core::GameVersion::V0_107_1;
    run.current_hp = public["hp"].as_i64().ok_or("ppo_public_hp_invalid")? as i32;
    run.max_hp = public["max_hp"]
        .as_i64()
        .ok_or("ppo_public_max_hp_invalid")? as i32;
    run.gold = public["gold"].as_i64().ok_or("ppo_public_gold_missing")? as i32;
    run.ascension_level = public["ascension"]
        .as_u64()
        .ok_or("ppo_public_ascension_missing")? as u8;
    run.total_floor = state["floor"].as_i64().ok_or("ppo_public_floor_missing")? as i32 - 1;
    run.relics = rows(public, "relics")
        .iter()
        .map(|item| {
            RelicId::all()
                .iter()
                .copied()
                .find(|id| id.to_python_name() == text(item, "id"))
                .ok_or_else(|| format!("ppo_neow_unknown_owned_relic:{}", text(item, "id")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let relic_state = &state["public_relic_state"];
    if let Some(melted) = relic_state["is_melted"].as_array() {
        if melted.len() != run.relics.len() || melted.iter().any(|flag| !flag.is_boolean()) {
            return Err("ppo_public_relic_melted_flags_invalid".into());
        }
        for (id, flag) in run.relics.iter().zip(melted) {
            if flag == true {
                run.melted_relics.insert(*id);
            }
        }
    }
    // Native DisplayAmount is the publicly visible accumulated count for this audited add-card hook.
    if let Some(position) = run
        .relics
        .iter()
        .position(|id| *id == RelicId::BookOfFiveRings)
    {
        if let Some(count) = rows(relic_state, "stacks")
            .get(position)
            .and_then(Value::as_i64)
        {
            if !(0..=4).contains(&count) {
                return Err("ppo_public_book_counter_invalid".into());
            }
            run.relic_counters
                .insert(RelicId::BookOfFiveRings, count as i32);
        }
    }
    run.master_deck = rows(public, "master_deck")
        .iter()
        .map(|item| {
            let spec = sts2_content::names::resolve_card_name(text(item, "id"), None)
                .map_err(|_| format!("ppo_neow_unknown_public_card:{}", text(item, "id")))?;
            let mut card = sts2_core::build_card_for_version(
                spec.id,
                item["upgraded"] == true,
                run.game_version,
            )
            .ok_or_else(|| format!("ppo_neow_public_card_factory_missing:{}", text(item, "id")))?;
            if let Some(id) = item["enchantment"].as_str() {
                let kind = sts2_core::cards::EnchantmentId::from_wire_name(id)
                    .ok_or("ppo_public_event_enchantment_unknown")?;
                let amount = item["enchantment_amount"]
                    .as_i64()
                    .and_then(|value| i32::try_from(value).ok())
                    .ok_or("ppo_public_event_enchantment_amount_missing")?;
                card.set_enchantment(kind, amount);
            }
            Ok::<_, String>(card)
        })
        .collect::<Result<Vec<_>, _>>()?;
    run.potions = rows(public, "potions")
        .iter()
        .filter(|item| item["empty"] != true)
        .map(|item| {
            sts2_core::potions::PotionKind::ALL
                .into_iter()
                .find(|kind| kind.python_name() == text(item, "id"))
                .ok_or_else(|| format!("ppo_neow_unknown_public_potion:{}", text(item, "id")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    run.max_potion_slots = rows(public, "potions").len() as i32;
    run.interactive_card_removal = true;
    run.interactive_card_upgrade = true;
    run.interactive_card_transform = true;
    run.direct_event_relic_grant = true;
    Ok(run)
}

/// Preserve the original gained/lost multisets and current/max HP deltas as distinct quantities.
fn event_effect_json(effect: sts2_content::events::dispatch::OptionEffectDelta) -> Value {
    json!({"gold_delta":effect.gold_delta,"hp_delta":effect.hp_delta,"max_hp_delta":effect.max_hp_delta,
        "relics_gained":effect.relics_gained,"relics_lost":effect.relics_lost,
        "potions_gained":effect.potions_gained,"potions_lost":effect.potions_lost,
        "cards_gained":effect.cards_gained,"cards_lost":effect.cards_lost})
}

/// Reuse Neow pickup rules over public state, with explicit independent worlds for hidden draws.
fn public_neow_effect(
    event: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
    world: Option<NeowWorld>,
) -> Result<Option<Value>, String> {
    if !text(event, "id").eq_ignore_ascii_case("NEOW") {
        return Ok(None);
    }
    use sts2_core::{NeowOffers, NeowRelic, RelicId};
    // These pickups have no hidden random identity that changes the nine aggregate effect features.
    let supported = [
        NeowRelic::WingedBoots,
        NeowRelic::LostCoffer,
        NeowRelic::SilkenTress,
        NeowRelic::BoomingConch,
        NeowRelic::GoldenPearl,
        NeowRelic::NutritiousOyster,
        NeowRelic::NeowsTorment,
        NeowRelic::CursedPearl,
        NeowRelic::MassiveScroll,
        NeowRelic::StoneHumidifier,
        NeowRelic::LavaRock,
        NeowRelic::SilverCrucible,
        NeowRelic::PreciseScissors,
        NeowRelic::PrecariousShears,
        NeowRelic::NewLeaf,
        NeowRelic::NeowsTalisman,
        NeowRelic::Pomander,
        NeowRelic::ArcaneScroll,
        NeowRelic::LeadPaperweight,
        NeowRelic::HeftyTablet,
        NeowRelic::ScrollBoxes,
        NeowRelic::Kaleidoscope,
        NeowRelic::PhialHolster,
        NeowRelic::FishingRod,
    ];
    let relic = rows(option, "preview_relic_ids")
        .first()
        .and_then(Value::as_str)
        .ok_or("ppo_neow_revealed_relic_identity_missing")?;
    let uncertain = [
        NeowRelic::NeowsBones,
        NeowRelic::LargeCapsule,
        NeowRelic::SmallCapsule,
        NeowRelic::LeafyPoultice,
    ];
    let nr = supported
        .into_iter()
        .chain(uncertain)
        .find(|nr| nr.snake_case_id() == relic)
        .ok_or_else(|| format!("ppo_neow_relic_not_in_pinned_version:{relic}"))?;
    if uncertain.contains(&nr) && world.is_none() {
        return Err(format!("{NEOW_UNCERTAINTY_REQUIRED}{relic}"));
    }
    let mut run = public_effect_run(state, character)?;
    // Other acquired relics can carry unobserved hook counters; the normal Neow opening has starters only.
    if run.relics.iter().any(|id| {
        !matches!(
            id,
            RelicId::BurningBlood
                | RelicId::RingOfTheSnake
                | RelicId::CrackedCore
                | RelicId::BoundPhylactery
                | RelicId::DivineRight
        )
    }) {
        return Err("ppo_neow_owned_relic_hook_state_not_public".into());
    }
    run.current_event = Some(sts2_core::events::EventId::Neow);
    // All three original handlers just grant their revealed relic, so slot zero is sufficient.
    run.neow_offers = Some(NeowOffers {
        positive_1: nr,
        positive_2: nr,
        cursed: nr,
    });
    if let Some(world) = world {
        run.rng = sts2_core::run_rng_set::RunRngSet::new_for(run.game_version, world.seed);
        if world.full_unlock {
            let (mut buckets, fallback, boss) =
                sts2_content::run_callbacks::default_relic_grab_bag_for(character);
            // Preserve the original setup's bucket order while sampling an independent unseen bag.
            use sts2_core::runstate::RelicRarity;
            for rarity in [
                RelicRarity::Uncommon,
                RelicRarity::Common,
                RelicRarity::Rare,
                RelicRarity::Shop,
            ] {
                if let Some(bucket) = buckets.get_mut(&rarity) {
                    run.rng.up_front.shuffle(bucket);
                }
            }
            run.populate_relic_grab_bag_by_rarity(buckets, fallback, boss);
        } else {
            // Bones may acquire a Capsule recursively. Detect that access instead of using an empty bag as zero.
            let mut audit = run.clone();
            sts2_content::events::dispatch::resolve_by_index(&mut audit, 0)
                .ok_or("ppo_public_neow_rule_preview_failed")?;
            if audit
                .relics
                .iter()
                .any(|id| matches!(id, RelicId::SmallCapsule | RelicId::LargeCapsule))
            {
                return Err("ppo_uncertainty_reward_pool_unknown:Capsule_requires_explicit_full_unlock_catalogue".into());
            }
        }
    }
    // Exact pickups have invariant counts; sampled pickups resolve only their independent hypothetical draws.
    let effect = sts2_content::events::dispatch::preview_option_effect(&run, 0)
        .ok_or("ppo_public_neow_rule_preview_failed")?;
    Ok(Some(event_effect_json(effect)))
}

/// Wellspring's displayed potion count is invariant; the original rule still applies the Sozu veto.
fn public_wellspring_effect(
    event: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
) -> Result<Option<Value>, String> {
    if !text(event, "id").eq_ignore_ascii_case("WELLSPRING") {
        return Ok(None);
    }
    let idx = match text(option, "text_key")
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_ascii_uppercase()
        .as_str()
    {
        "BOTTLE" => 0,
        "BATHE" => 1,
        _ => return Ok(None),
    };
    let mut run = public_effect_run(state, character)?;
    run.current_event = Some(sts2_core::events::EventId::Wellspring);
    // The random potion's identity is never exposed: only the invariant offer count reaches the encoder.
    let effect = sts2_content::events::dispatch::preview_option_effect(&run, idx)
        .ok_or("ppo_public_wellspring_rule_preview_failed")?;
    Ok(Some(event_effect_json(effect)))
}

/// Tea Master's public fixed prices and offered relics use the original event handlers exactly.
fn public_tea_master_effect(
    event: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
) -> Result<Option<Value>, String> {
    if !text(event, "id")
        .replace('_', "")
        .eq_ignore_ascii_case("TeaMaster")
    {
        return Ok(None);
    }
    let id = sts2_core::events::EventId::TeaMaster;
    let spec = sts2_content::events::dispatch::spec_for(id);
    let Some(idx) = spec
        .options
        .iter()
        .position(|entry| entry.loc_key.eq_ignore_ascii_case(text(option, "text_key")))
    else {
        return Ok(None);
    };
    let mut run = public_effect_run(state, character)?;
    run.current_event = Some(id);
    let effect = sts2_content::events::dispatch::preview_option_effect(&run, idx)
        .ok_or("ppo_public_tea_master_rule_preview_failed")?;
    Ok(Some(event_effect_json(effect)))
}

/// Resolve audited event effects through original handlers without revealing future reward identities.
fn public_fixed_event_effect(
    event: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
) -> Result<Option<Value>, String> {
    use sts2_content::events::dispatch::{present, preview_option_effect, spec_for};
    use sts2_core::events::EventId;
    let id = match text(event, "id")
        .replace('_', "")
        .to_ascii_uppercase()
        .as_str()
    {
        "DOLLROOM" => EventId::DollRoom,
        "GRAVEOFTHEFORGOTTEN" => EventId::GraveOfTheForgotten,
        "THEFUTUREOFPOTIONS" => EventId::TheFutureOfPotions,
        _ => return Ok(None),
    };
    let spec = spec_for(id);
    let key = text(option, "text_key");
    if id == EventId::TheFutureOfPotions {
        let potion = option["vars"]["Potion"]
            .as_str()
            .ok_or("ppo_future_of_potions_public_potion_identity_missing")?;
        let mut run = public_effect_run(state, character)?;
        run.current_event = Some(id);
        let presented = present(&run).ok_or("ppo_future_of_potions_not_presented")?;
        // All native trade rows share a key; the visible Potion binding identifies held candidates.
        let effects = presented
            .iter()
            .enumerate()
            .filter(|(_, entry)| {
                entry.loc_key.eq_ignore_ascii_case(key)
                    && entry.vars.iter().any(|(name, value)| {
                        *name == "Potion"
                            && matches!(value, sts2_core::EventVar::Text(value) if value == potion)
                    })
            })
            .map(|(index, _)| {
                preview_option_effect(&run, index)
                    .ok_or("ppo_future_of_potions_rule_preview_failed")
            })
            .collect::<Result<Vec<_>, _>>()?;
        let effect = effects
            .first()
            .ok_or("ppo_future_of_potions_public_potion_not_presented")?;
        // Duplicate held potions need no guessed slot when their original aggregate effects agree.
        if effects.iter().any(|candidate| candidate != effect) {
            return Err("ppo_future_of_potions_public_effect_not_invariant".into());
        }
        // The preview's hypothetical card offer never becomes a public observation or neural input.
        return Ok(Some(event_effect_json(*effect)));
    }
    let mut selected = spec.options.iter().find(|entry| {
        (!entry.loc_key.is_empty() && entry.loc_key.eq_ignore_ascii_case(key))
            || entry.option_id.eq_ignore_ascii_case(key)
    });
    // Native doll-pick rows share a localization key; only an actually previewed relic identifies the handler.
    if selected.is_none() && id == EventId::DollRoom {
        let dolls = [
            ("DAUGHTER_OF_THE_WIND", "doll_daughter"),
            ("MR_STRUGGLES", "doll_mr_struggles"),
            ("BING_BONG", "doll_bingbong"),
        ];
        if let [relic] = rows(option, "preview_relic_ids") {
            selected = dolls
                .iter()
                .find(|(name, _)| {
                    relic
                        .as_str()
                        .is_some_and(|id| id.eq_ignore_ascii_case(name))
                })
                .and_then(|(_, option_id)| {
                    spec.options
                        .iter()
                        .find(|entry| entry.option_id == *option_id)
                });
        }
    }
    let Some(selected) = selected else {
        return Ok(None);
    };
    let mut run = public_effect_run(state, character)?;
    run.current_event = Some(id);
    if id == EventId::GraveOfTheForgotten && selected.option_id == "confront" {
        // Decay uses the same public add-card hooks as a transform replacement.
        public_transform_hook_state(state, &run)?;
    }
    let presented = present(&run).ok_or("ppo_public_fixed_event_not_presented")?;
    let preview = |option_id: &str| {
        let index = presented
            .iter()
            .position(|entry| entry.option_id == option_id)
            .ok_or("ppo_public_fixed_event_handler_not_presented")?;
        preview_option_effect(&run, index).ok_or("ppo_public_fixed_event_preview_failed")
    };
    let effect = if id == EventId::DollRoom && selected.option_id == "random" {
        // The original random handler chooses exactly one of these grants; no unseen identity is selected here.
        let outcomes = ["doll_daughter", "doll_mr_struggles", "doll_bingbong"]
            .map(preview)
            .into_iter()
            .collect::<Result<Vec<_>, _>>()?;
        if outcomes.iter().any(|effect| *effect != outcomes[0]) {
            return Err("ppo_doll_room_public_reward_aggregate_not_invariant".into());
        }
        outcomes[0]
    } else {
        preview(selected.option_id)?
    };
    Ok(Some(event_effect_json(effect)))
}

/// Read This or That's already displayed gold amount through the original English cost parser.
fn public_this_or_that_gold(event: &Value) -> Result<i64, String> {
    let plain = rows(event, "options")
        .iter()
        .find(|option| {
            text(option, "text_key")
                .rsplit('.')
                .next()
                .is_some_and(|key| key.eq_ignore_ascii_case("PLAIN"))
        })
        .ok_or("ppo_this_or_that_public_plain_option_missing")?;
    let description = english_option_text(plain, "description")?;
    let (gold, _) = encoder::event_payload(&strip_tags(&description), 1);
    if !gold.is_finite() || gold <= 0.0 || gold.fract() != 0.0 {
        return Err("ppo_this_or_that_displayed_gold_not_recoverable".into());
    }
    Ok(gold as i64)
}

/// Condition the known displayed roll and sample only the unseen relic pickup through original rules.
fn public_this_or_that_effect(
    event: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
    world: Option<NeowWorld>,
) -> Result<Option<Value>, String> {
    if !text(event, "id")
        .replace('_', "")
        .eq_ignore_ascii_case("ThisOrThat")
    {
        return Ok(None);
    }
    let Some(world) = world else {
        return Err(format!("{EVENT_UNCERTAINTY_REQUIRED}THIS_OR_THAT"));
    };
    let gold = public_this_or_that_gold(event)?;
    let run = public_effect_run(state, character)?;
    let key = text(option, "text_key").rsplit('.').next().unwrap_or("");
    let effect = event_world::preview_this_or_that(run, gold, key, world.seed, world.full_unlock)?;
    Ok(Some(event_effect_json(effect)))
}

/// Keep original queued descriptors from an accepted public event choice, never its future RNG draws.
fn public_event_selection_context(
    source: &Value,
    option: &Value,
    state: &Value,
    character: CharacterId,
) -> Result<Value, String> {
    use sts2_core::events::EventId;
    let canonical = text(&source["event"], "id")
        .replace('_', "")
        .to_ascii_uppercase();
    let event = match canonical.as_str() {
        "WOODCARVINGS" => EventId::WoodCarvings,
        "SAPPHIRESEED" => EventId::SapphireSeed,
        "SELFHELPBOOK" => EventId::SelfHelpBook,
        "FIELDOFMANSIZEDHOLES" => EventId::FieldOfManSizedHoles,
        "MORPHICGROVE" => EventId::MorphicGrove,
        "WHISPERINGHOLLOW" => EventId::WhisperingHollow,
        "SYMBIOTE" => EventId::Symbiote,
        "AROMAOFCHAOS" => EventId::AromaOfChaos,
        _ => return Ok(Value::Null),
    };
    let spec = sts2_content::events::dispatch::spec_for(event);
    let Some(index) = spec
        .options
        .iter()
        .position(|entry| entry.loc_key.eq_ignore_ascii_case(text(option, "text_key")))
    else {
        return Ok(Value::Null);
    };
    let mut run = public_effect_run(state, character)?;
    run.current_event = Some(event);
    sts2_content::events::dispatch::resolve_by_index(&mut run, index)
        .ok_or("ppo_public_enchant_origin_rule_unavailable")?;
    let mut carry = queued_rule_context(source, &run, "event")?;
    if carry.is_null() {
        return Ok(carry);
    }
    carry["public_source"] = json!({"event_id":source["event"]["id"],"option_key":option["text_key"],
        "identity_source":"original_event_handler_over_public_resources"});
    Ok(carry)
}

/// Serialize only descriptor identity and audited constraints; no random replacement is rolled here.
fn transform_descriptors(entries: &[sts2_core::PendingDeckTransform]) -> Value {
    json!(entries.iter().map(|entry| json!({"basic_only":entry.basic_only,
        "fixed_target":entry.fixed_target.as_ref().map(core_wire_card),
        "event_stream":entry.event_stream_roll.map(|(name,skip)|json!({"name":name,"skip_reads":skip})),
        "post_hp_loss":entry.post_hp_loss})).collect::<Vec<_>>())
}

/// Build a resumable public context from the original handler's pending queue.
fn queued_rule_context(
    source: &Value,
    run: &sts2_core::runstate::RunState,
    origin_kind: &str,
) -> Result<Value, String> {
    let (intent, count) = if !run.pending_deck_enchants.is_empty() {
        (
            format!("{origin_kind}_enchant"),
            run.pending_deck_enchants.len(),
        )
    } else if !run.pending_deck_transforms.is_empty() {
        (
            format!("{origin_kind}_transform"),
            run.pending_deck_transforms.len(),
        )
    } else {
        return Ok(Value::Null);
    };
    let mut carry = context(source, &intent, &Value::Null, 0);
    carry["required_count"] = json!(count);
    if let Some(pending) = run.pending_deck_enchants.first() {
        if run
            .pending_deck_enchants
            .iter()
            .any(|entry| entry != pending)
        {
            return Err("ppo_public_enchant_queue_has_different_descriptors".into());
        }
        carry["pending_enchantment_id"] = json!(pending.enchantment.wire_name());
        carry["pending_enchantment_amount"] = json!(pending.amount);
    }
    if !run.pending_deck_transforms.is_empty() {
        carry["pending_transforms"] = transform_descriptors(&run.pending_deck_transforms);
    }
    Ok(carry)
}

/// Use fixed public pickup rules to retain an actual relic-origin enchant or transform queue.
fn public_relic_selection_context(
    source: &Value,
    relic: &str,
    state: &Value,
    character: CharacterId,
) -> Result<Value, String> {
    use sts2_core::RelicId;
    let Some(id) = [
        RelicId::GnarledHammer,
        RelicId::Kifuda,
        RelicId::BeautifulBracelet,
        RelicId::TriBoomerang,
        RelicId::RoyalStamp,
        RelicId::PunchDagger,
        RelicId::ElectricShrymp,
        RelicId::NewLeaf,
    ]
    .into_iter()
    .find(|id| id.to_python_name() == relic) else {
        return Ok(Value::Null);
    };
    let mut run = public_effect_run(state, character)?;
    run.obtain_relic(id);
    let mut carry = queued_rule_context(source, &run, "relic")?;
    if !carry.is_null() {
        carry["public_source"] = json!({"relic_id":relic,"identity_source":"original_fixed_relic_pickup_over_public_resources"});
    }
    Ok(carry)
}

/// Recover the same original queue from its accepted public source while discarding repeated side effects.
fn recover_origin_run(
    carry: &Value,
    public_run: sts2_core::runstate::RunState,
) -> Result<sts2_core::runstate::RunState, String> {
    let source = &carry["public_source"];
    let mut run = public_run;
    if let Some(event_id) = source["event_id"].as_str() {
        let canonical = event_id.replace('_', "").to_ascii_uppercase();
        let id = sts2_core::events::EventId::all()
            .iter()
            .copied()
            .find(|id| format!("{id:?}").to_ascii_uppercase() == canonical)
            .ok_or("ppo_transform_public_source_event_unknown")?;
        let spec = sts2_content::events::dispatch::spec_for(id);
        let index = spec
            .options
            .iter()
            .position(|entry| {
                entry
                    .loc_key
                    .eq_ignore_ascii_case(text(source, "option_key"))
            })
            .ok_or("ppo_transform_public_source_option_unknown")?;
        run.current_event = Some(id);
        sts2_content::events::dispatch::resolve_by_index(&mut run, index)
            .ok_or("ppo_transform_public_source_rule_failed")?;
    } else if let Some(relic) = source["relic_id"].as_str() {
        use sts2_core::RelicId;
        let id = [
            RelicId::GnarledHammer,
            RelicId::Kifuda,
            RelicId::BeautifulBracelet,
            RelicId::TriBoomerang,
            RelicId::RoyalStamp,
            RelicId::PunchDagger,
            RelicId::ElectricShrymp,
            RelicId::NewLeaf,
        ]
        .into_iter()
        .find(|id| id.to_python_name() == relic)
        .ok_or("ppo_selection_public_source_relic_unknown")?;
        run.obtain_relic(id);
    } else {
        return Err("ppo_transform_public_source_missing".into());
    }
    Ok(run)
}

/// Match the accepted descriptor against the original rule without applying its repeated resource changes.
fn recover_transform_queue(
    carry: &Value,
    public_run: sts2_core::runstate::RunState,
) -> Result<Vec<sts2_core::PendingDeckTransform>, String> {
    let run = recover_origin_run(carry, public_run)?;
    if run.pending_deck_transforms.is_empty()
        || transform_descriptors(&run.pending_deck_transforms) != carry["pending_transforms"]
    {
        return Err("ppo_transform_public_source_descriptor_conflict".into());
    }
    Ok(run.pending_deck_transforms)
}

/// Require actual visible state only for owned hooks used while the original transform adds a card.
fn public_transform_hook_state(
    state: &Value,
    run: &sts2_core::runstate::RunState,
) -> Result<(), String> {
    use sts2_core::RelicId;
    // These are the pinned run-side add/modify-add registrations; their active state affects the projection.
    const ADD_HOOK_RELICS: [RelicId; 8] = [
        RelicId::BookOfFiveRings,
        RelicId::LuckyFysh,
        RelicId::DarkstonePeriapt,
        RelicId::BingBong,
        RelicId::FresnelLens,
        RelicId::FrozenEgg,
        RelicId::MoltenEgg,
        RelicId::ToxicEgg,
    ];
    if run.relics.iter().any(|id| ADD_HOOK_RELICS.contains(id))
        && !state["public_relic_state"]["is_melted"].is_array()
    {
        return Err("ppo_transform_owned_add_hook_active_state_not_public".into());
    }
    if run.owns_active_relic(RelicId::BookOfFiveRings)
        && !run.relic_counters.contains_key(&RelicId::BookOfFiveRings)
    {
        return Err("ppo_transform_book_of_five_rings_visible_counter_missing".into());
    }
    Ok(())
}

/// Let the original wire builder recognize Ancient relic choices from revealed identities alone.
fn public_boss_relic_surface(state: &Value, event: &Value) -> bool {
    let mut probe = state.clone();
    probe["options"] = json!(rows(event, "options")
        .iter()
        .map(|option| json!({"index":option["i"],
        "enabled":option["locked"]!=true,"event_id":event["id"],"text_key":option["text_key"],
        "label":option["label"],"description":option["description"],
        "relic_id":rows(option,"preview_relic_ids").first()}))
        .collect::<Vec<_>>());
    upstream_wire_decision(&probe, "event").is_some_and(|decision| {
        !decision.actions.is_empty()
            && decision
                .actions
                .iter()
                .all(|action| matches!(action, ActionDesc::PickBossRelic { .. }))
    })
}

/// Reuse the original public safety mask before resolving unknown rewards when only one action can survive.
fn forced_public_event(
    request: &Value,
    state: &Value,
    warnings: &[String],
) -> Result<Option<Prepared>, String> {
    let source = &request["public_state"];
    let event = &source["event"];
    let enabled = rows(event, "options")
        .iter()
        .filter(|option| option["locked"] != true)
        .collect::<Vec<_>>();
    if enabled.is_empty() {
        return Ok(None);
    }
    let mut probe = state.clone();
    let mut options = Vec::new();
    for option in &enabled {
        let description = if enabled.len() == 1 || option["kill"].is_boolean() {
            text(option, "description").to_string()
        } else {
            match english_option_text(option, "description") {
                Ok(text) => text,
                Err(_) => return Ok(None),
            }
        };
        options.push(
            json!({"index":index(option)?,"enabled":true,"event_id":event["id"],
            "text_key":option["text_key"],"label":option["label"],"description":description,
            "will_kill_player":option["kill"],"relic_id":rows(option,"preview_relic_ids").first()}),
        );
    }
    probe["type"] = json!("event");
    probe["options"] = json!(options);
    let Some(mut decision) = upstream_wire_decision(&probe, "event") else {
        return Ok(None);
    };
    if decision.actions.len() != 1 {
        return Ok(None);
    }
    let native = decision.responses[0]["index"]
        .as_i64()
        .ok_or("ppo_wire_response_index_missing")?;
    let option = enabled
        .iter()
        .find(|option| option["i"] == native)
        .ok_or("ppo_forced_event_native_index_missing")?;
    decision.responses = vec![json!({"type":"choose_event_option","option_index":native})];
    let mut carry =
        public_event_selection_context(source, option, state, request_character(request)?)?;
    if carry.is_null() && rows(option, "preview_relic_ids").len() == 1 {
        carry = public_relic_selection_context(
            source,
            rows(option, "preview_relic_ids")[0].as_str().unwrap_or(""),
            state,
            request_character(request)?,
        )?;
    }
    if carry.is_null() {
        carry = nested_reward_context(request);
    }
    Ok(Some(Prepared {
        decision,
        contexts: vec![carry],
        warnings: warnings.to_vec(),
        queued_effects: Value::Null,
    }))
}

/// Create the upstream decision while keeping each native response positionally parallel.
fn prepare(request: &Value) -> Result<Prepared, String> {
    prepare_with_world(request, None)
}

/// Apply a sampled independent Neow world without putting it into the actual public observation.
fn prepare_with_world(request: &Value, world: Option<NeowWorld>) -> Result<Prepared, String> {
    let source = &request["public_state"];
    let mut state = base_wire(request)?;
    let mut mapped = HashMap::<i64, Value>::new();
    let mut warnings = Vec::new();
    if state["public_current_coord_unknown"] == true {
        warnings.push("historical_map_topology_current_coordinate_unknown".into());
    }
    let surface = match text(source, "screen") {
        "MAP" => {
            if !legal(source, "choose_map_node") {
                return Err("ppo_map_choice_not_ready".into());
            }
            let mut nodes = Vec::new();
            for option in rows(&source["map"], "options") {
                let n = index(option)?;
                let position = if !option["coord"].is_null() {
                    coord(&option["coord"])?
                } else {
                    warnings.push("legacy_visible_map_coordinate_text".into());
                    let line = text(option, "line");
                    coord(&json!(line
                        .rsplit_once('(')
                        .and_then(|(_, v)| v.strip_suffix(')'))
                        .ok_or("ppo_map_option_coordinate_missing")?))?
                };
                let mut node = position;
                node["index"] = json!(n);
                let full = rows(&state["map"], "nodes")
                    .iter()
                    .find(|item| item["row"] == node["row"] && item["col"] == node["col"])
                    .ok_or("ppo_offered_map_node_missing_from_graph")?;
                node["type"] = full["type"].clone();
                nodes.push(node);
                mapped.insert(n, json!({"type":"choose_map_node","option_index":n}));
            }
            state["nodes"] = json!(nodes);
            "map_select"
        }
        "REWARD" if legal(source, "choose_reward_card") || legal(source, "skip_reward_cards") => {
            let cards = rows(&source["reward"], "cards");
            state["cards"] = json!(cards.iter().map(card).collect::<Result<Vec<_>, _>>()?);
            state["can_skip"] = json!(legal(source, "skip_reward_cards"));
            for (position, c) in cards.iter().enumerate() {
                mapped.insert(
                    position as i64,
                    json!({"type":"choose_reward_card","option_index":index(c)?}),
                );
            }
            "card_reward"
        }
        "EVENT" => {
            if !legal(source, "choose_event_option") {
                return Err("ppo_event_choice_not_ready".into());
            }
            if let Some(forced) = forced_public_event(request, &state, &warnings)? {
                return Ok(forced);
            }
            let event = &source["event"];
            let enabled = rows(event, "options")
                .iter()
                .filter(|option| option["locked"] != true && option["kill"] != true)
                .collect::<Vec<_>>();
            // A sole native Proceed button is an observed forced action, not a relic offer to score.
            let forced_proceed = enabled.len() == 1
                && text(enabled[0], "text_key")
                    .rsplit('.')
                    .next()
                    .is_some_and(|key| key.eq_ignore_ascii_case("PROCEED"));
            let boss_relic = public_boss_relic_surface(&state, event);
            let mut options = Vec::new();
            for option in rows(event, "options") {
                let n = index(option)?;
                if text(option, "text_key").is_empty()
                    || option.get("description").and_then(Value::as_str).is_none()
                {
                    return Err("ppo_event_structured_public_text_missing_rebuild_bridge".into());
                }
                let relics = rows(option, "preview_relic_ids");
                let neow_effect = if forced_proceed || boss_relic {
                    None
                } else {
                    public_neow_effect(event, option, &state, request_character(request)?, world)?
                };
                let is_neow = neow_effect.is_some();
                let public_effect = if forced_proceed || boss_relic {
                    None
                } else if is_neow {
                    neow_effect
                } else if let Some(effect) =
                    public_wellspring_effect(event, option, &state, request_character(request)?)?
                {
                    Some(effect)
                } else if let Some(effect) =
                    public_tea_master_effect(event, option, &state, request_character(request)?)?
                {
                    Some(effect)
                } else if let Some(effect) =
                    public_fixed_event_effect(event, option, &state, request_character(request)?)?
                {
                    Some(effect)
                } else {
                    public_this_or_that_effect(
                        event,
                        option,
                        &state,
                        request_character(request)?,
                        world,
                    )?
                };
                // Complete public-rule deltas supersede localized text; Neow alone trains on a relic-ID label.
                let (label, description) = if forced_proceed || boss_relic {
                    (
                        text(option, "label").to_string(),
                        text(option, "description").to_string(),
                    )
                } else if is_neow {
                    (
                        relics[0]
                            .as_str()
                            .ok_or("ppo_neow_revealed_relic_identity_missing")?
                            .to_string(),
                        text(option, "description").to_string(),
                    )
                } else if public_effect.is_some() {
                    (
                        text(option, "label").to_string(),
                        text(option, "description").to_string(),
                    )
                } else {
                    (
                        english_option_text(option, "label")?,
                        english_option_text(option, "description")?,
                    )
                };
                let mut item = json!({"index":n,"enabled":option["locked"]!=true,"event_id":event["id"],
                    "text_key":option["text_key"],"label":label,"description":description,
                    "will_kill_player":option["kill"],"relic_id":relics.first().cloned().unwrap_or(Value::Null)});
                for key in [
                    "gold_delta",
                    "hp_delta",
                    "max_hp_delta",
                    "relics_gained",
                    "relics_lost",
                    "potions_gained",
                    "potions_lost",
                    "cards_gained",
                    "cards_lost",
                ] {
                    if let Some(value) = option.get(key) {
                        item[key] = value.clone();
                    }
                }
                if let Some(effect) = public_effect {
                    for (key, value) in
                        effect.as_object().ok_or("ppo_public_neow_effect_invalid")?
                    {
                        item[key] = value.clone();
                    }
                }
                options.push(item);
                mapped.insert(n, json!({"type":"choose_event_option","option_index":n}));
            }
            state["options"] = json!(options);
            "event"
        }
        "SHOP" if source["shop"]["open"] == true => {
            let mut options = Vec::new();
            for (collection, kind, idkey) in [
                ("cards", "buy_card", "card_id"),
                ("relics", "buy_relic", "relic_id"),
                ("potions", "buy_potion", "potion_id"),
            ] {
                if !legal(source, kind) {
                    continue;
                }
                for item in rows(&source["shop"], collection) {
                    if item["affordable"] != true || item["stocked"] == false {
                        continue;
                    }
                    let handle = options.len() as i64;
                    let price = item
                        .get("price")
                        .and_then(Value::as_i64)
                        .ok_or("ppo_shop_structured_price_missing_rebuild_bridge")?;
                    if text(item, idkey).is_empty() {
                        return Err("ppo_shop_item_id_missing".into());
                    }
                    options.push(json!({"index":handle,"action":kind,"price":price,"enabled":true,"item":{"id":item[idkey],"upgraded":item["upgraded"]}}));
                    mapped.insert(handle, json!({"type":kind,"option_index":index(item)?}));
                }
            }
            if legal(source, "remove_card_at_shop")
                && source["shop"]["remove"]["affordable"] == true
                && source["shop"]["remove"]["available"] == true
            {
                let handle = options.len() as i64;
                let price = source["shop"]["remove"]["price"]
                    .as_i64()
                    .ok_or("ppo_shop_removal_price_missing")?;
                options.push(
                    json!({"index":handle,"action":"remove_card","price":price,"enabled":true}),
                );
                mapped.insert(handle, json!({"type":"remove_card_at_shop"}));
            }
            if legal(source, "close_shop_inventory") || legal(source, "proceed") {
                let handle = options.len() as i64;
                let name = if legal(source, "close_shop_inventory") {
                    "close_shop_inventory"
                } else {
                    "proceed"
                };
                options.push(json!({"index":handle,"action":"leave_shop","enabled":true}));
                mapped.insert(handle, json!({"type":name}));
            }
            state["options"] = json!(options);
            "shop"
        }
        "REST" => {
            if !legal(source, "choose_rest_option") {
                return Err("ppo_rest_choice_not_ready".into());
            }
            // Native multiplayer targets are players, not Smith-card indices.
            state["cards"] = json!([]);
            state["can_skip"] = json!(true);
            state["type"] = json!("rest_site");
            let mut d = upstream_wire_decision(&state, "card_reward")
                .ok_or("ppo_base_observation_missing")?;
            d.obs.phase = "rest_site".into();
            d.obs.card_reward_option_ids = None;
            d.obs.card_reward_option_upgraded = None;
            d.obs.card_reward_option_enchantments = None;
            d.obs.rest_option_labels = Some(vec!["rest".into(), "smith".into()]);
            d.actions.clear();
            d.responses.clear();
            d.labels.clear();
            let mut contexts = Vec::new();
            for option in rows(&source["rest"], "options") {
                if option["enabled"] == false {
                    continue;
                }
                if option["requires_target"] == true {
                    return Err("ppo_multiplayer_rest_target_not_supported".into());
                }
                let key = text(option, "option_id").to_ascii_uppercase();
                let n = match key.as_str() {
                    "REST" => 0,
                    "SMITH" => 1,
                    _ => return Err(format!("ppo_untrained_rest_option:{key}")),
                };
                d.actions.push(ActionDesc::RestOption { idx: n });
                d.responses
                    .push(json!({"type":"choose_rest_option","option_index":index(option)?}));
                d.labels.push(key.clone());
                contexts.push(if key == "SMITH" {
                    context(source, "smith", &Value::Null, 0)
                } else {
                    Value::Null
                });
            }
            return Ok(Prepared {
                decision: d,
                contexts,
                warnings,
                queued_effects: Value::Null,
            });
        }
        "CARD_SELECTION" => return prepare_selection(request, state, warnings, world),
        screen => return Err(format!("ppo_scene_not_supported:{screen}")),
    };
    state["type"] = json!(surface);
    let mut decision = upstream_wire_decision(&state, surface).ok_or("ppo_no_scored_candidates")?;
    let mut contexts = Vec::with_capacity(decision.actions.len());
    for (response, desc) in decision.responses.iter_mut().zip(&decision.actions) {
        let raw = response.clone();
        *response = if raw["action"] == "skip" {
            json!({"type":"skip_reward_cards"})
        } else {
            mapped
                .get(
                    &raw["index"]
                        .as_i64()
                        .ok_or("ppo_wire_response_index_missing")?,
                )
                .cloned()
                .ok_or("ppo_wire_response_unmapped")?
        };
        let carry = if let ActionDesc::ShopRemoveCard { deck_idx } = desc {
            let deck = rows(&state["run_state"], "master_deck");
            let target = deck
                .get(*deck_idx as usize)
                .ok_or("ppo_shop_remove_target_missing")?;
            let occurrence = deck
                .iter()
                .take(*deck_idx as usize)
                .filter(|c| same_card(c, target))
                .count();
            context(source, "shop_remove", target, occurrence)
        } else if text(response, "type") == "choose_event_option" {
            let option = rows(&source["event"], "options")
                .iter()
                .find(|option| option["i"] == response["option_index"])
                .ok_or("ppo_event_context_option_missing")?;
            let pending = public_event_selection_context(
                source,
                option,
                &state,
                request_character(request)?,
            )?;
            if pending.is_null() && rows(option, "preview_relic_ids").len() == 1 {
                public_relic_selection_context(
                    source,
                    rows(option, "preview_relic_ids")[0].as_str().unwrap_or(""),
                    &state,
                    request_character(request)?,
                )?
            } else {
                pending
            }
        } else if matches!(desc, ActionDesc::ShopBuyRelic { .. }) {
            let row = rows(&source["shop"], "relics")
                .iter()
                .find(|row| row["i"] == response["option_index"])
                .ok_or("ppo_shop_relic_context_row_missing")?;
            public_relic_selection_context(
                source,
                text(row, "relic_id"),
                &state,
                request_character(request)?,
            )?
        } else {
            nested_reward_context(request)
        };
        contexts.push(carry);
    }
    if decision
        .actions
        .iter()
        .any(|action| matches!(action, ActionDesc::Unknown))
    {
        return Err("ppo_unknown_action_surface".into());
    }
    Ok(Prepared {
        decision,
        contexts,
        warnings,
        queued_effects: Value::Null,
    })
}

/// Score native non-combat card picks under their actual mutation semantics.
fn prepare_selection(
    request: &Value,
    mut state: Value,
    warnings: Vec<String>,
    world: Option<NeowWorld>,
) -> Result<Prepared, String> {
    let source = &request["public_state"];
    let selection = &source["selection"];
    let kind = text(selection, "kind");
    let underlying = selection
        .get("underlying_screen")
        .or_else(|| {
            source
                .get("modal")
                .and_then(|value| value.get("underlying_screen"))
        })
        .and_then(Value::as_str)
        .unwrap_or("");
    let explicit_run_kind = kind.starts_with("deck_")
        || matches!(
            text(selection, "operation"),
            "remove" | "upgrade" | "transform" | "enchant" | "grant"
        )
        || (kind == "choose_card_select"
            && matches!(underlying, "REWARD" | "EVENT" | "REST" | "SHOP"));
    if kind.starts_with("combat_")
        || (!explicit_run_kind
            && rows(&source["combat"], "enemies")
                .iter()
                .any(|enemy| enemy["alive"] == true))
    {
        return Err("ppo_combat_selection_belongs_to_combat_policy".into());
    }
    let required = selection["max"]
        .as_u64()
        .filter(|count| *count > 0)
        .and_then(|count| usize::try_from(count).ok())
        .ok_or("ppo_selection_target_count_missing")?;
    let selected_count = selection["selected"].as_u64().unwrap_or(0) as usize;
    if selected_count > required {
        return Err("ppo_selection_selected_count_exceeds_limit".into());
    }
    let batch = required > 1;
    if batch && selection["min"].as_u64() != Some(required as u64) {
        return Err("ppo_optional_multi_card_selection_semantics_unknown".into());
    }
    let carry = &request["selection_context"];
    let same_act = carry["origin"]["act_index"].as_i64().map_or_else(
        || carry["origin"]["act_id"] == source["run"]["act_id"],
        |act| public_act_index(&source["run"]).ok() == Some(act),
    );
    let valid = same_act && carry["origin"]["floor"] == source["run"]["floor"];
    let intent = if valid { text(carry, "intent") } else { "" };
    // Native targets remain queued until the real confirm button commits the whole batch.
    if selected_count == required
        && selection["confirm"] == true
        && legal(source, "confirm_selection")
    {
        state["cards"] = json!([]);
        state["can_skip"] = json!(true);
        let mut decision =
            upstream_wire_decision(&state, "card_reward").ok_or("ppo_base_observation_missing")?;
        decision.actions = vec![ActionDesc::Unknown];
        decision.responses = vec![json!({"type":"confirm_selection"})];
        decision.labels = vec!["confirm selected card".into()];
        return Ok(Prepared {
            decision,
            contexts: vec![nested_reward_context(request)],
            warnings,
            queued_effects: Value::Null,
        });
    }
    if kind == "choose_card_select" && legal(source, "skip_selection") {
        if batch {
            return Err("ppo_generated_multi_card_selection_semantics_unknown".into());
        }
        if !legal(source, "select_deck_card") {
            return Err("ppo_card_selection_not_ready".into());
        }
        let offered = rows(selection, "cards");
        state["cards"] = json!(offered.iter().map(card).collect::<Result<Vec<_>, _>>()?);
        state["can_skip"] = json!(true);
        let mut decision = upstream_wire_decision(&state, "card_reward")
            .ok_or("ppo_generated_card_reward_empty")?;
        for (position, response) in decision.responses.iter_mut().enumerate() {
            *response = if position < offered.len() {
                json!({"type":"select_deck_card","option_index":index(&offered[position])?})
            } else {
                json!({"type":"skip_selection"})
            };
        }
        let contexts = vec![nested_reward_context(request); decision.actions.len()];
        return Ok(Prepared {
            decision,
            contexts,
            warnings,
            queued_effects: Value::Null,
        });
    }
    if kind == "choose_card_select" && selection["can_skip"] == true {
        return Err("ppo_optional_generated_card_skip_not_ready".into());
    }
    let operation = match text(selection, "kind") {
        "deck_upgrade_select" => {
            if intent == "smith" {
                "smith"
            } else {
                "upgrade"
            }
        }
        "deck_transform_select" => "transform",
        "deck_enchant_select" => "enchant",
        "deck_card_select" if intent == "shop_remove" => "remove",
        "choose_card_select" => "grant",
        _ => match text(selection, "operation") {
            "remove" => "remove",
            "upgrade" => "upgrade",
            "transform" => "transform",
            "enchant" => "enchant",
            _ => return Err("ppo_card_selection_operation_unknown".into()),
        },
    };
    if !legal(source, "select_deck_card") {
        return Err("ppo_card_selection_not_ready".into());
    }
    let offered_all = rows(selection, "cards");
    let remaining_native = offered_all
        .iter()
        .filter(|row| row["selected"] != true && row["selectable"] != false)
        .collect::<Vec<_>>();
    if batch && selected_count + 1 == required && remaining_native.len() == 1 {
        let selected = offered_all
            .iter()
            .filter(|row| row["selected"] == true)
            .collect::<Vec<_>>();
        if selected.len() != selected_count {
            return Err("ppo_batch_selected_count_does_not_match_public_targets".into());
        }
        state["cards"] = json!([]);
        state["can_skip"] = json!(true);
        let mut decision =
            upstream_wire_decision(&state, "card_reward").ok_or("ppo_base_observation_missing")?;
        decision.actions = vec![ActionDesc::Unknown];
        decision.responses =
            vec![json!({"type":"select_deck_card","option_index":index(remaining_native[0])?})];
        decision.labels = vec!["complete the sole remaining mandatory native target".into()];
        return Ok(Prepared {
            decision,
            contexts: vec![if valid {
                carry.clone()
            } else {
                nested_reward_context(request)
            }],
            warnings,
            queued_effects: json!({"scope":"hypothetical_until_native_confirm","operation":operation,"required_count":required,
                "selected_native_indices":selected.iter().map(|row|row["i"].clone()).collect::<Vec<_>>(),
                "native_observation_mutated":false,"projected_effects_available":false,
                "projection_not_needed_for_forced_action":true}),
        });
    }
    let all_cards = offered_all
        .iter()
        .map(card)
        .collect::<Result<Vec<_>, _>>()?;
    let mut original_indices = Vec::with_capacity(all_cards.len());
    let original_master = rows(&state["run_state"], "master_deck");
    let mut used_original = Vec::new();
    for c in &all_cards {
        let found = original_master
            .iter()
            .enumerate()
            .find(|(idx, target)| !used_original.contains(idx) && same_card(c, target))
            .map(|(idx, _)| idx);
        if let Some(index) = found {
            used_original.push(index);
        } else if operation != "grant" {
            return Err("ppo_selection_card_not_in_public_deck".into());
        }
        original_indices.push(found);
    }
    let native_enchantment = selection
        .get("enchantment_id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty());
    let carried_enchantment = if matches!(intent, "event_enchant" | "relic_enchant") {
        carry.get("pending_enchantment_id").and_then(Value::as_str)
    } else {
        None
    };
    if batch
        && matches!(
            intent,
            "event_enchant" | "relic_enchant" | "event_transform" | "relic_transform"
        )
        && carry["required_count"]
            .as_u64()
            .is_some_and(|count| count != required as u64)
    {
        return Err("ppo_batch_public_source_target_count_conflict".into());
    }
    if let (Some(native), Some(carried)) = (native_enchantment, carried_enchantment) {
        if sts2_core::cards::EnchantmentId::from_wire_name(native)
            != sts2_core::cards::EnchantmentId::from_wire_name(carried)
        {
            return Err("ppo_public_enchantment_identity_conflict".into());
        }
    }
    let pending_enchantment = native_enchantment.or(carried_enchantment);
    if operation == "enchant" && pending_enchantment.is_none() {
        return Err("ppo_pending_enchantment_identity_missing".into());
    }
    let mut queued_effects = Value::Null;
    let mut projected_to_original = (0..original_master.len()).map(Some).collect::<Vec<_>>();
    if batch {
        let selected_positions = offered_all
            .iter()
            .enumerate()
            .filter(|(_, row)| row["selected"] == true)
            .map(|(idx, _)| idx)
            .collect::<Vec<_>>();
        if selected_positions.len() != selected_count {
            return Err("ppo_batch_selected_count_does_not_match_public_targets".into());
        }
        let selected_indices = selected_positions
            .iter()
            .map(|position| {
                original_indices[*position].ok_or("ppo_batch_selected_card_not_in_public_deck")
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut applied_indices = Vec::new();
        let enchantment = if operation == "enchant" && selected_count > 0 {
            let id = pending_enchantment
                .and_then(sts2_core::cards::EnchantmentId::from_wire_name)
                .ok_or("ppo_pending_enchantment_identity_unknown")?;
            let amount = selection
                .get("enchantment_amount")
                .filter(|value| !value.is_null())
                .or_else(|| {
                    if matches!(intent, "event_enchant" | "relic_enchant") {
                        carry.get("pending_enchantment_amount")
                    } else {
                        None
                    }
                })
                .and_then(Value::as_i64)
                .and_then(|amount| i32::try_from(amount).ok())
                .ok_or("ppo_batch_pending_enchantment_amount_not_public")?;
            Some((id, amount))
        } else {
            None
        };
        if selected_count > 0 {
            let mut public_run = public_effect_run(&state, request_character(request)?)?;
            if operation == "enchant"
                && matches!(intent, "event_enchant" | "relic_enchant")
                && !carry["public_source"].is_null()
                && !carry["public_source"]["identity_source"].is_null()
            {
                let recovered = recover_origin_run(carry, public_run.clone())?;
                if recovered.pending_deck_enchants.len() != required
                    || recovered
                        .pending_deck_enchants
                        .first()
                        .is_none_or(|entry| Some((entry.enchantment, entry.amount)) != enchantment)
                {
                    return Err("ppo_batch_public_enchantment_descriptor_conflict".into());
                }
                public_run.pending_deck_enchants = recovered.pending_deck_enchants;
            }
            if operation == "transform" {
                if !matches!(intent, "event_transform" | "relic_transform") {
                    return Err("ppo_batch_transform_public_source_missing".into());
                }
                let pending = recover_transform_queue(carry, public_run.clone())?;
                if pending.len() != required {
                    return Err("ppo_batch_public_source_target_count_conflict".into());
                }
                let random = pending
                    .iter()
                    .take(selected_count)
                    .any(|entry| entry.fixed_target.is_none());
                let seed = if random {
                    let sampled = world.ok_or_else(|| {
                        format!("{TRANSFORM_UNCERTAINTY_REQUIRED}queued_replacements")
                    })?;
                    if !sampled.full_unlock {
                        return Err("ppo_transform_card_pool_unknown:requires_explicit_full_unlock_catalogue".into());
                    }
                    sampled.seed
                } else {
                    0
                };
                public_transform_hook_state(&state, &public_run)?;
                let projection = transform::project_selected_transform(
                    public_run,
                    pending,
                    &selected_indices,
                    required,
                    seed,
                )?;
                applied_indices = projection.applied_original_indices;
                projected_to_original = projection.original_deck_indices;
                project_wire_run(&mut state, &projection.run);
            } else {
                let projection = batch::project_selected(
                    public_run,
                    operation,
                    &selected_indices,
                    required,
                    enchantment,
                )?;
                applied_indices = projection.applied_original_indices;
                projected_to_original = projection
                    .original_deck_indices
                    .into_iter()
                    .map(Some)
                    .collect();
                project_wire_run(&mut state, &projection.run);
            }
        }
        queued_effects = json!({"scope":"hypothetical_until_native_confirm","operation":operation,
            "required_count":required,"selected_native_indices":selected_positions.iter().map(|idx|offered_all[*idx]["i"].clone()).collect::<Vec<_>>(),
            "applied_original_deck_indices":applied_indices,
            "projected_to_original_deck_indices":projected_to_original,"native_observation_mutated":false,
            "candidate_space":"native_unselected_targets_only"});
    }
    let remaining_positions = offered_all
        .iter()
        .enumerate()
        .filter(|(_, row)| row["selectable"] != false && (!batch || row["selected"] != true))
        .map(|(idx, _)| idx)
        .collect::<Vec<_>>();
    let offered = remaining_positions
        .iter()
        .map(|position| &offered_all[*position])
        .collect::<Vec<_>>();
    let cards = remaining_positions
        .iter()
        .map(|position| all_cards[*position].clone())
        .collect::<Vec<_>>();
    state["cards"] = json!(cards);
    state["can_skip"] = json!(false);
    let mut decision =
        upstream_wire_decision(&state, "card_reward").ok_or("ppo_no_selection_candidates")?;
    decision.actions.clear();
    decision.responses.clear();
    decision.labels.clear();
    decision.obs.phase = if operation == "smith" {
        "rest_site".into()
    } else {
        "card_reward".into()
    };
    decision.obs.card_reward_option_ids = None;
    decision.obs.card_reward_option_upgraded = None;
    decision.obs.card_reward_option_enchantments = None;
    let pairs = cards
        .iter()
        .map(|c| (text(c, "id").to_owned(), c["upgraded"] == true))
        .collect::<Vec<_>>();
    let enchantments = cards
        .iter()
        .map(|card| {
            card["enchantment"].as_str().map(|id| {
                (
                    id.to_string(),
                    card["enchantment_amount"].as_i64().unwrap_or(1),
                )
            })
        })
        .collect::<Vec<_>>();
    match operation {
        "smith" => {
            decision.obs.smith_options = Some(pairs.iter().map(|(id, _)| id.clone()).collect());
            decision.obs.smith_option_enchantments = Some(enchantments.clone());
        }
        "remove" => {
            decision.obs.card_removal_options = Some(pairs.clone());
            decision.obs.card_removal_option_enchantments = Some(enchantments.clone());
        }
        "grant" => {
            decision.obs.card_grant_options = Some(pairs.clone());
            decision.obs.card_grant_option_enchantments = Some(enchantments.clone());
        }
        _ => {}
    }
    if operation == "enchant" {
        // The model vocabulary uses registry IDs; multiword display titles otherwise become OOV.
        decision.obs.pending_enchantment = Some(pending_enchantment.map(|name| {
            sts2_core::cards::EnchantmentId::from_wire_name(name)
                .map_or_else(|| name.to_string(), |id| id.python_name().to_string())
        }));
    }
    let mut deck_indices = Vec::new();
    let carry_target = valid && intent == "shop_remove" && !carry["target"].is_null();
    let mut target_seen = 0usize;
    for (position, (public, c)) in offered.iter().zip(&cards).enumerate() {
        let original = original_indices[remaining_positions[position]];
        let deck_idx = original.and_then(|original| {
            projected_to_original
                .iter()
                .position(|idx| *idx == Some(original))
        });
        if let Some(idx) = deck_idx {
            deck_indices.push(idx as i64);
        } else if operation != "grant" {
            return Err("ppo_selection_card_not_in_public_deck".into());
        }
        let include = if carry_target {
            if same_card(c, &carry["target"]) {
                let chosen = target_seen == carry["occurrence"].as_u64().unwrap_or(0) as usize;
                target_seen += 1;
                chosen
            } else {
                false
            }
        } else {
            true
        };
        if !include {
            continue;
        }
        let n = position as i64;
        // Mutation action indices address eligible offers; the observation maps those back to deck positions.
        decision.actions.push(match operation {
            "smith" => ActionDesc::SmithCard { idx: n },
            "remove" => ActionDesc::RemoveDeckCard { idx: n },
            "grant" => ActionDesc::GrantDeckCard { idx: n },
            "upgrade" => ActionDesc::UpgradeDeckCard { idx: n },
            "transform" => ActionDesc::TransformDeckCard { idx: n },
            "enchant" => ActionDesc::EnchantDeckCard { idx: n },
            _ => unreachable!(),
        });
        decision
            .responses
            .push(json!({"type":"select_deck_card","option_index":index(public)?}));
        decision.labels.push(text(c, "id").into());
    }
    match operation {
        "upgrade" => decision.obs.upgrade_select_deck_indices = Some(deck_indices),
        "transform" => decision.obs.transform_select_deck_indices = Some(deck_indices),
        "enchant" => decision.obs.enchant_select_deck_indices = Some(deck_indices),
        _ => {}
    }
    if carry_target && decision.actions.len() != 1 {
        return Err("ppo_carried_remove_target_not_available".into());
    }
    let next_context = if batch
        && matches!(
            intent,
            "event_enchant" | "relic_enchant" | "event_transform" | "relic_transform"
        ) {
        carry.clone()
    } else {
        nested_reward_context(request)
    };
    let contexts = vec![next_context; decision.actions.len()];
    Ok(Prepared {
        decision,
        contexts,
        warnings,
        queued_effects,
    })
}

/// Serialize original card mutations for the same upstream wire builder, preserving enchant stacks.
fn core_wire_card(card: &sts2_core::cards::Card) -> Value {
    let mut value = json!({"id":format!("{:?}",card.card_id),"upgraded":card.upgraded});
    if let Some((id, amount)) = sts2_runai::ppo::obs::card_enchant_obs(&card.enchantments) {
        value["enchantment"] = json!(id);
        value["enchantment_amount"] = json!(amount);
    }
    value
}

/// Rebuild only model-visible resources inside a clearly labeled hypothetical local observation.
fn project_wire_run(state: &mut Value, run: &sts2_core::runstate::RunState) {
    state["run_state"]["master_deck"] = json!(run
        .master_deck
        .iter()
        .map(core_wire_card)
        .collect::<Vec<_>>());
    state["run_state"]["hp"] = json!(run.current_hp);
    state["run_state"]["max_hp"] = json!(run.max_hp);
    state["run_state"]["gold"] = json!(run.gold);
    state["run_state"]["relics"] = json!(run
        .relics
        .iter()
        .map(|id| json!({"id":id.to_python_name()}))
        .collect::<Vec<_>>());
    let mut potions = run
        .potions
        .iter()
        .map(|kind| json!({"id":kind.python_name(),"empty":false}))
        .collect::<Vec<_>>();
    while potions.len() < run.max_potion_slots.max(0) as usize {
        potions.push(json!({"id":"","empty":true}));
    }
    state["run_state"]["potions"] = json!(potions);
}

/// Run the original grouped PPO decoder and return the actual mapped native action.
pub fn policy_evaluate(request: &Value) -> Result<Value, String> {
    let source = &request["public_state"];
    if text(source, "screen") == "REWARD"
        && !legal(source, "choose_reward_card")
        && !legal(source, "skip_reward_cards")
    {
        let mut result = reward_sequence_evaluate(request).unwrap_or_else(unsupported);
        result["decision_pipeline"] = json!("upstream_reward_continuation");
        return Ok(result);
    }
    let prepared = match prepare(request) {
        Ok(value) => value,
        Err(reason) => {
            if reason.starts_with(NEOW_UNCERTAINTY_REQUIRED)
                || reason.starts_with(EVENT_UNCERTAINTY_REQUIRED)
                || reason.starts_with(TRANSFORM_UNCERTAINTY_REQUIRED)
            {
                return public_uncertainty_evaluate(request);
            }
            return Ok(unsupported(reason));
        }
    };
    Ok(score_prepared(request, &prepared))
}

/// Score one original decision without retaining the model-cache lock across reward continuations.
fn score_prepared(request: &Value, prepared: &Prepared) -> Value {
    if prepared.decision.actions.is_empty() {
        return unsupported("ppo_no_legal_actions".into());
    }
    let result = with_model(request, |loaded| {
        let (probs, value, pick) = if prepared.decision.actions.len() == 1 {
            (vec![1.0], Value::Null, 0)
        } else {
            let (probs, value, pick) = loaded
                .policy
                .evaluate_wire(&prepared.decision)
                .ok_or("ppo_upstream_inference_failed")?;
            (probs, json!(value), pick)
        };
        let candidates=probs.iter().enumerate().map(|(idx,probability)|json!({"action":prepared.decision.responses[idx],
            "probability":probability,"label":prepared.decision.labels[idx],"upstream_action":prepared.decision.actions[idx],"selected":idx==pick})).collect::<Vec<_>>();
        Ok(
            json!({"ok":true,"status":if probs.len()==1{"forced_public_action"}else{"estimated"},
            "best_action":prepared.decision.responses[pick],"candidates":candidates,"value":value,
            "next_context":prepared.contexts[pick],"model_identity":loaded.identity,
            "support":{"level":if prepared.warnings.is_empty(){"supported"}else{"partial"},"warnings":prepared.warnings,"blocking_reasons":[]},
            "decision_pipeline":"upstream_wire_advisor","obs":prepared.decision.obs,"actions":prepared.decision.actions,
            "queued_selection_effects":prepared.queued_effects,
            "probability_meaning":if probs.len()==1{"forced_public_action_not_network_inference"}else{"upstream_policy_action_mass_not_win_probability"},
            "neural_inference":probs.len()>1,"grouped_argmax":pick}),
        )
    });
    result.unwrap_or_else(unsupported)
}

/// Identify strategic reward rows by their public resource identity, never by translated prose.
fn reward_row_key(row: &Value) -> Result<String, String> {
    let kind = text(row, "reward_type").to_ascii_uppercase();
    let field = match kind.as_str() {
        "RELIC" => "relic_id",
        "POTION" => "potion_id",
        _ => return Err(format!("ppo_reward_type_not_supported:{kind}")),
    };
    let id = text(row, field);
    if id.is_empty() {
        return Err(format!("ppo_reward_public_identity_missing:{kind}"));
    }
    Ok(format!("{kind}:{id}"))
}

/// Exclude native rows known to be invisible or already successfully procured.
fn pending_reward_row(row: &Value) -> bool {
    row["visible"] != false && row["successfully_selected"] != true
}

/// Bind logical declines to this public reward page; a later page cannot inherit them accidentally.
fn reward_context(source: &Value, page: &[String], deferred: &[String]) -> Value {
    let mut carry = context(source, "reward_sequence", &Value::Null, 0);
    carry["page_rows"] = json!(page);
    carry["deferred_rows"] = json!(deferred);
    carry
}

/// Describe a real forced UI transition without inventing neural probabilities or critic values.
fn forced_reward_response(action: Value, carry: Value, reason: &str) -> Value {
    json!({"ok":true,"status":"forced_public_action","best_action":action,"candidates":[{"action":action,
        "probability":1.0,"selected":true,"label":reason}],"value":null,"next_context":carry,
        "decision_pipeline":"upstream_reward_continuation","probability_meaning":"forced_public_action_not_network_inference",
        "support":{"level":"supported","blocking_reasons":[],"warnings":[]}})
}

/// Build original relic-pick and potion-swap actions while retaining their native execution mapping.
fn prepare_reward_row(
    request: &Value,
    row: &Value,
    remaining: &[(String, Value)],
    carry: &Value,
) -> Result<Prepared, String> {
    let source = &request["public_state"];
    let mut state = base_wire(request)?;
    state["type"] = json!("card_reward");
    state["cards"] = json!([]);
    state["can_skip"] = json!(true);
    let mut decision = upstream_wire_decision(&state, "card_reward")
        .ok_or("ppo_reward_base_observation_missing")?;
    decision.obs.card_reward_option_ids = None;
    decision.obs.card_reward_option_upgraded = None;
    decision.obs.card_reward_option_enchantments = None;
    decision.actions.clear();
    decision.responses.clear();
    decision.labels.clear();
    let mut contexts = Vec::new();
    let mut warnings = Vec::new();
    if state["public_current_coord_unknown"] == true {
        warnings.push("historical_map_topology_current_coordinate_unknown".into());
    }
    match text(row, "reward_type").to_ascii_uppercase().as_str() {
        "RELIC" => {
            if !pending_reward_row(row)
                || row["claimable"] != true
                || !legal(source, "claim_reward")
            {
                return Err("ppo_relic_reward_not_claimable".into());
            }
            decision.obs.relic_reward_ids = Some(
                remaining
                    .iter()
                    .rev()
                    .filter(|(_, r)| text(r, "reward_type").eq_ignore_ascii_case("RELIC"))
                    .map(|(_, r)| text(r, "relic_id").to_string())
                    .collect(),
            );
            decision.actions = vec![ActionDesc::PickRelicReward, ActionDesc::SkipRelic];
            decision.responses = vec![
                json!({"type":"claim_reward","option_index":index(row)?}),
                Value::Null,
            ];
            decision.labels = vec![
                format!("take:{}", text(row, "relic_id")),
                format!("defer_decline:{}", text(row, "relic_id")),
            ];
            let mut accepted = public_relic_selection_context(
                source,
                text(row, "relic_id"),
                &state,
                request_character(request)?,
            )?;
            if accepted.is_null() {
                accepted = carry.clone();
            } else {
                accepted["resume_context"] = carry.clone();
            }
            contexts = vec![accepted, carry.clone()];
        }
        "POTION" => {
            if row["potion_procurement_allowed"] != true || row["potion_slot_available"] != false {
                return Err("ppo_potion_swap_public_gate_incomplete".into());
            }
            // A full belt disables its claim button; the native public reward status proves it is still pending.
            let pending_native = row["visible"] == true && row["successfully_selected"] == false;
            if !pending_reward_row(row) || (row["claimable"] != true && !pending_native) {
                return Err("ppo_potion_reward_row_readiness_unknown".into());
            }
            decision.obs.pending_potion_swap = Some(text(row, "potion_id").to_string());
            decision
                .actions
                .push(ActionDesc::PotionSwap { discard_slot: None });
            decision.responses.push(Value::Null);
            decision.labels.push("defer_decline_potion".into());
            contexts.push(carry.clone());
            let mut held_index = 0i64;
            for potion in rows(&source["run"], "potions") {
                if potion["occupied"] == false || potion["potion_id"].is_null() {
                    continue;
                }
                let id = text(potion, "potion_id");
                if id.is_empty() {
                    return Err("ppo_held_potion_identity_missing".into());
                }
                if legal(source, "discard_potion") {
                    let can_discard = potion["discard"]
                        .as_bool()
                        .ok_or("ppo_public_potion_discardability_missing")?;
                    if can_discard {
                        let slot = index(potion)?;
                        let mut next = carry.clone();
                        next["claim_after_discard"] = json!({"row_key":reward_row_key(row)?,"potion_id":row["potion_id"],
                            "discarded_slot":slot,"discarded_potion_id":id});
                        decision.actions.push(ActionDesc::PotionSwap {
                            discard_slot: Some(held_index),
                        });
                        decision
                            .responses
                            .push(json!({"type":"discard_potion","option_index":slot}));
                        decision.labels.push(format!("discard:{id}"));
                        contexts.push(next);
                    }
                }
                held_index += 1;
            }
        }
        _ => return Err("ppo_reward_row_type_not_scored".into()),
    }
    Ok(Prepared {
        decision,
        contexts,
        warnings,
        queued_effects: Value::Null,
    })
}

/// Defer individual declines locally until every remaining native reward can safely be left together.
fn reward_sequence_evaluate(request: &Value) -> Result<Value, String> {
    let source = &request["public_state"];
    let reward = &source["reward"];
    let raw = reward
        .get("rewards")
        .and_then(Value::as_array)
        .ok_or("ppo_public_reward_rows_missing")?;
    let mut strategic = Vec::<(String, Value)>::new();
    for row in raw {
        if !pending_reward_row(row) {
            continue;
        }
        let kind = text(row, "reward_type").to_ascii_uppercase();
        if (kind == "RELIC" && row["claimable"] == true) || kind == "POTION" {
            let key = reward_row_key(row)?;
            if strategic.iter().any(|(old, _)| old == &key) {
                return Err("ppo_reward_repeated_identity_without_stable_handle".into());
            }
            strategic.push((key, row.clone()));
        } else if row["claimable"] == true && !matches!(kind.as_str(), "GOLD" | "CARD") {
            return Err(format!("ppo_reward_type_not_supported:{kind}"));
        }
    }
    let page = strategic
        .iter()
        .map(|(key, _)| key.clone())
        .collect::<Vec<_>>();
    let old = &request["selection_context"];
    let valid = text(old, "intent") == "reward_sequence"
        && old["origin"]["floor"] == source["run"]["floor"]
        && old["origin"]["act_index"].as_i64() == Some(public_act_index(&source["run"])?)
        && page
            .iter()
            .all(|key| rows(old, "page_rows").iter().any(|item| item == key));
    let mut deferred = if valid {
        rows(old, "deferred_rows")
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let mut steps = Vec::<Value>::new();
    let outcome = (|| -> Result<Value, String> {
        if valid && !old["claim_after_discard"].is_null() {
            let pending = &old["claim_after_discard"];
            let row = strategic
                .iter()
                .find(|(key, _)| key == text(pending, "row_key"))
                .map(|(_, row)| row)
                .ok_or("ppo_pending_potion_reward_row_disappeared")?;
            let slot = rows(&source["run"], "potions")
                .iter()
                .find(|slot| slot["i"] == pending["discarded_slot"])
                .ok_or("ppo_pending_potion_discard_slot_missing")?;
            if slot["occupied"] != false && !slot["potion_id"].is_null() {
                return Err("ppo_pending_potion_discard_not_observed".into());
            }
            if row["potion_can_receive"] != true
                || row["claimable"] != true
                || !legal(source, "claim_reward")
            {
                return Err("ppo_pending_potion_reward_not_receivable".into());
            }
            let mut result = forced_reward_response(
                json!({"type":"claim_reward","option_index":index(row)?}),
                reward_context(source, &page, &deferred),
                "complete_observed_potion_replacement",
            );
            result["policy_steps"] = json!([{ "source":"completed_discard_public_followup","neural_inference":false,
            "native_action":result["best_action"],"reward_row":reward_row_key(row)?}]);
            return Ok(result);
        }
        loop {
            let remaining = strategic
                .iter()
                .filter(|(key, _)| !deferred.contains(key))
                .cloned()
                .collect::<Vec<_>>();
            // Upstream commits gold and empty-slot potions without a take/skip policy decision.
            let housekeeping = raw
                .iter()
                .find(|row| {
                    pending_reward_row(row)
                        && row["claimable"] == true
                        && text(row, "reward_type").eq_ignore_ascii_case("GOLD")
                })
                .or_else(|| {
                    remaining
                        .iter()
                        .find(|(_, row)| {
                            text(row, "reward_type").eq_ignore_ascii_case("POTION")
                                && row["potion_can_receive"] == true
                                && row["claimable"] == true
                        })
                        .map(|(_, row)| row)
                });
            if let Some(row) = housekeeping {
                if !legal(source, "claim_reward") {
                    return Err("ppo_forced_reward_claim_not_available".into());
                }
                let mut result = forced_reward_response(
                    json!({"type":"claim_reward","option_index":index(row)?}),
                    reward_context(source, &page, &deferred),
                    "upstream_forced_reward_drain",
                );
                result["policy_steps"] = json!(steps);
                return Ok(result);
            }
            // A full-belt swap preempts card picks and relic picks in the original RunManager.
            let blocked_potion = remaining
                .iter()
                .find(|(_, row)| text(row, "reward_type").eq_ignore_ascii_case("POTION"));
            let focused = if let Some(entry) = blocked_potion {
                Some(entry)
            } else {
                if let Some(row) = raw.iter().find(|row| {
                    pending_reward_row(row)
                        && row["claimable"] == true
                        && text(row, "reward_type").eq_ignore_ascii_case("CARD")
                }) {
                    if !legal(source, "claim_reward") {
                        return Err("ppo_card_reward_open_not_available".into());
                    }
                    let mut result = forced_reward_response(
                        json!({"type":"claim_reward","option_index":index(row)?}),
                        reward_context(source, &page, &deferred),
                        "open_upstream_card_reward_choice",
                    );
                    result["policy_steps"] = json!(steps);
                    return Ok(result);
                }
                remaining
                    .iter()
                    .rev()
                    .find(|(_, row)| text(row, "reward_type").eq_ignore_ascii_case("RELIC"))
            };
            let Some((key, row)) = focused else {
                if !legal(source, "proceed") || reward["can_proceed"] != true {
                    return Err("ppo_reward_page_cannot_proceed".into());
                }
                let mut result = forced_reward_response(
                    json!({"type":"proceed"}),
                    Value::Null,
                    "commit_all_pending_reward_declines",
                );
                if let Some(last) = steps
                    .iter()
                    .rev()
                    .find(|step| step["candidates"].is_array())
                {
                    result["candidates"] = last["candidates"].clone();
                    result["value"] = last["value"].clone();
                    result["model_identity"] = last["model_identity"].clone();
                    result["grouped_argmax"] = last["grouped_argmax"].clone();
                    result["obs"] = last["obs"].clone();
                    result["actions"] = last["actions"].clone();
                    if last["neural_inference"] == true {
                        result["status"] = json!("estimated");
                    }
                    if let Some(candidates) = result["candidates"].as_array_mut() {
                        for candidate in candidates {
                            if candidate["selected"] == true && candidate["action"].is_null() {
                                candidate["action"] = json!({"type":"proceed"});
                            }
                        }
                    }
                    result["probability_meaning"] = json!(
                        "upstream_policy_mass_final_logical_decline_commits_native_page_exit"
                    );
                }
                result["policy_steps"] = json!(steps);
                result["logical_declines_pending"] = json!(deferred);
                return Ok(result);
            };
            if text(row, "reward_type").eq_ignore_ascii_case("POTION")
                && row["potion_procurement_allowed"] == false
            {
                steps.push(json!({"source":"native_public_potion_procurement_veto","neural_inference":false,
                "reward_row":key,"reward_row_i":index(row)?,"reward_type":row["reward_type"],"potion_id":row["potion_id"],
                "logical_decline":key,"native_action":null,"executed":false}));
                deferred.push(key.clone());
                continue;
            }
            let carry = reward_context(source, &page, &deferred);
            let prepared = match prepare_reward_row(request, row, &remaining, &carry) {
                Ok(value) => value,
                Err(reason) => {
                    let mut result = unsupported(reason);
                    result["policy_steps"] = json!(steps);
                    result["logical_declines_pending"] = json!(deferred);
                    return Ok(result);
                }
            };
            let mut scored = score_prepared(request, &prepared);
            let mut step = scored.clone();
            step["reward_row"] = json!(key);
            step["neural_inference"] = json!(prepared.decision.actions.len() > 1);
            step["reward_row_i"] = json!(index(row)?);
            step["reward_type"] = row["reward_type"].clone();
            step["relic_id"] = row["relic_id"].clone();
            step["potion_id"] = row["potion_id"].clone();
            if let Some(pick) = scored["grouped_argmax"]
                .as_u64()
                .and_then(|pick| usize::try_from(pick).ok())
            {
                step["selected_upstream_action"] = json!(prepared.decision.actions.get(pick));
            }
            step["executed"] = json!(false);
            step["native_action"] = scored["best_action"].clone();
            if scored["ok"] != true {
                steps.push(step);
                scored["policy_steps"] = json!(steps);
                return Ok(scored);
            }
            if scored["best_action"].is_null() {
                step["logical_decline"] = json!(key);
                steps.push(step);
                deferred.push(key.clone());
                continue;
            }
            steps.push(step);
            scored["policy_steps"] = json!(steps);
            scored["logical_declines_pending"] = json!(deferred);
            scored["decision_pipeline"] = json!("upstream_reward_continuation");
            return Ok(scored);
        }
    })();
    match outcome {
        Ok(result) => Ok(result),
        Err(reason) => {
            let mut result = unsupported(reason);
            result["policy_steps"] = json!(steps);
            result["logical_declines_pending"] = json!(deferred);
            Ok(result)
        }
    }
}

/// Derive replayable hypothetical worlds solely from model-visible public features and revealed offers.
fn public_uncertainty_parameters(request: &Value) -> Result<(String, u64, usize, bool), String> {
    let settings = &request["public_uncertainty"];
    let count = settings
        .get("worlds")
        .and_then(Value::as_u64)
        .map(|n| n as usize)
        .unwrap_or(DEFAULT_UNCERTAINTY_WORLDS);
    if !(1..=MAX_UNCERTAINTY_WORLDS).contains(&count) {
        return Err("ppo_public_uncertainty_world_count_out_of_range".into());
    }
    let state = base_wire(request)?;
    let offered = rows(&request["public_state"]["event"], "options")
        .iter()
        .map(|option| option["preview_relic_ids"].clone())
        .collect::<Vec<_>>();
    // The run's actual seed, cursors, hidden bag order and unknown rooms are not part of this payload.
    let mut payload = json!({"character":format!("{:?}",request_character(request)?),"run_state":state["run_state"],
        "floor":state["floor"],"act":state["act"],"revealed_neow_offers":offered});
    let event = &request["public_state"]["event"];
    if text(event, "id")
        .replace('_', "")
        .eq_ignore_ascii_case("ThisOrThat")
    {
        payload["event_domain"] = json!("THIS_OR_THAT_PUBLIC_GOLD_CONDITIONED_V1");
        payload["displayed_gold"] = json!(public_this_or_that_gold(event)?);
    }
    if is_transform_batch(request) {
        let selection = &request["public_state"]["selection"];
        payload["event_domain"] = json!("NATIVE_QUEUED_TRANSFORM_PUBLIC_PRIOR_V1");
        payload["public_selection"] = json!({"required":selection["max"],"selected":selection["selected"],
            "targets":rows(selection,"cards").iter().map(|row|Ok(json!({"i":index(row)?,"card":card(row)?,"selected":row["selected"]})))
                .collect::<Result<Vec<_>,String>>()?});
        payload["public_origin"] = request["selection_context"]["public_source"].clone();
        payload["pending_descriptors"] = request["selection_context"]["pending_transforms"].clone();
        payload["public_relic_state"] = state["public_relic_state"].clone();
    }
    let bytes =
        serde_json::to_vec(&payload).map_err(|e| format!("ppo_public_uncertainty_hash:{e}"))?;
    let digest = Sha256::digest(&bytes);
    let base = u64::from_le_bytes(
        digest[..8]
            .try_into()
            .map_err(|_| "ppo_public_uncertainty_hash_length")?,
    );
    Ok((
        format!("{digest:x}"),
        base,
        count,
        settings["full_unlock_catalogue"] == true,
    ))
}

/// Average original PPO forwards across every public world, then use the original policy decoder.
fn public_uncertainty_evaluate(request: &Value) -> Result<Value, String> {
    let (public_hash, base, count, full_unlock) = match public_uncertainty_parameters(request) {
        Ok(settings) => settings,
        Err(reason) => return Ok(unsupported(reason)),
    };
    let transform_batch = is_transform_batch(request);
    let world_salt = if transform_batch {
        PUBLIC_TRANSFORM_WORLD_SALT
    } else {
        PUBLIC_NEOW_WORLD_SALT
    };
    let prepared = (0..count)
        .map(|ordinal| {
            let seed =
                sts2_core::run_rng_set::outcome_stream_seed_u64(base, ordinal as u32, world_salt);
            (
                ordinal,
                seed,
                prepare_with_world(request, Some(NeowWorld { seed, full_unlock })),
            )
        })
        .collect::<Vec<_>>();
    let event = &request["public_state"]["event"];
    let public_conditions = if text(event, "id")
        .replace('_', "")
        .eq_ignore_ascii_case("ThisOrThat")
    {
        let gold = public_this_or_that_gold(event)?;
        json!({"displayed_gold":gold,"representative_named_event_seed":format!("0x{:016x}",event_world::conditioning_seed(gold)?),
            "meaning":"public_displayed_roll_condition_not_actual_master_seed"})
    } else if transform_batch {
        json!({"public_source":request["selection_context"]["public_source"],
            "pending_descriptors":request["selection_context"]["pending_transforms"],
            "selected_count":request["public_state"]["selection"]["selected"],
            "meaning":"queued_native_targets_with_independent_unseen_replacements_not_actual_master_seed"})
    } else {
        Value::Null
    };
    let result = with_model(request, |loaded| {
        let mut worlds = Vec::with_capacity(count);
        let mut first: Option<Prepared> = None;
        let mut sums = Vec::<f64>::new();
        let mut value_sum = 0.0f64;
        let mut scored = 0usize;
        let mut errors = Vec::<String>::new();
        for (ordinal, seed, prepared) in prepared {
            let mut record = json!({"ordinal":ordinal,"sample_seed":format!("0x{seed:016x}"),
                "normalized_rng_seed":sts2_core::GameVersion::V0_107_1.normalize_seed(seed),
                "sampling_weight":1.0/count as f64,"status":"unsupported","obs":null,
                "probs":null,"value":null,"grouped_argmax":null,"errors":[]});
            let prepared = match prepared {
                Ok(value) => value,
                Err(reason) => {
                    record["errors"] = json!([reason.clone()]);
                    errors.push(reason);
                    worlds.push(record);
                    continue;
                }
            };
            record["obs"] = json!(prepared.decision.obs);
            if !prepared.queued_effects.is_null() {
                record["queued_selection_effects"] = prepared.queued_effects.clone();
            }
            if let Some(reference) = &first {
                if reference.decision.actions != prepared.decision.actions
                    || reference.decision.responses != prepared.decision.responses
                {
                    let reason = "ppo_public_uncertainty_candidate_order_changed".to_string();
                    record["errors"] = json!([reason.clone()]);
                    errors.push(reason);
                    worlds.push(record);
                    continue;
                }
            }
            let inference = loaded.policy.evaluate_wire(&prepared.decision);
            let (probs, value, pick) = match inference {
                Some(result) => result,
                None => {
                    let reason = "ppo_public_uncertainty_upstream_inference_failed".to_string();
                    record["errors"] = json!([reason.clone()]);
                    errors.push(reason);
                    worlds.push(record);
                    continue;
                }
            };
            if probs.len() != prepared.decision.actions.len()
                || !value.is_finite()
                || probs.iter().any(|p| !p.is_finite())
            {
                let reason = "ppo_public_uncertainty_nonfinite_or_incomplete_inference".to_string();
                record["errors"] = json!([reason.clone()]);
                errors.push(reason);
                worlds.push(record);
                continue;
            }
            if first.is_none() {
                sums = vec![0.0; probs.len()];
                first = Some(prepared);
            }
            for (sum, probability) in sums.iter_mut().zip(&probs) {
                *sum += f64::from(*probability);
            }
            value_sum += f64::from(value);
            scored += 1;
            record["status"] = json!("scored");
            record["probs"] = json!(probs);
            record["value"] = json!(value);
            record["grouped_argmax"] = json!(pick);
            worlds.push(record);
        }
        errors.sort();
        errors.dedup();
        let mut result = if scored != count || !errors.is_empty() {
            // Unknown worlds retain their original weight; none are removed to manufacture an average.
            let mut value = unsupported("ppo_public_uncertainty_worlds_incomplete".into());
            value["errors"] = json!(errors);
            value
        } else if let Some(reference) = &first {
            let probs = sums
                .iter()
                .map(|sum| (sum / count as f64) as f32)
                .collect::<Vec<_>>();
            match loaded
                .policy
                .decode_probabilities(&reference.decision.actions, &probs)
            {
                Some(pick) => {
                    let candidates=probs.iter().enumerate().map(|(idx,probability)|json!({
                        "action":reference.decision.responses[idx],"probability":probability,
                        "label":reference.decision.labels[idx],"upstream_action":reference.decision.actions[idx],"selected":idx==pick})).collect::<Vec<_>>();
                    json!({"ok":true,"status":"public_approximation","best_action":reference.decision.responses[pick],
                        "candidates":candidates,"value":value_sum/count as f64,"next_context":reference.contexts[pick],
                        "grouped_argmax":pick,"support":{"level":"partial","warnings":["public_independent_rng_prior_not_exact_upstream_future"],"blocking_reasons":[]},
                        "probability_meaning":"mean_upstream_policy_action_mass_over_public_hypothetical_worlds_not_win_probability",
                        "value_meaning":"mean_upstream_critic_over_public_hypothetical_worlds"})
                }
                None => unsupported("ppo_public_uncertainty_decode_failed".into()),
            }
        } else {
            unsupported("ppo_public_uncertainty_no_scored_worlds".into())
        };
        result["obs"] = Value::Null;
        result["actions"] = first
            .as_ref()
            .map(|p| json!(p.decision.actions))
            .unwrap_or(json!([]));
        result["model_identity"] = loaded.identity.clone();
        result["decision_pipeline"] = json!("public_uncertainty_mixture");
        if transform_batch {
            result["queued_selection_effects"] = json!({"scope":"hypothetical_until_native_confirm",
            "operation":"transform","required_count":request["public_state"]["selection"]["max"],
            "selected_count":request["public_state"]["selection"]["selected"],
            "candidate_space":"native_unselected_targets_only","native_observation_mutated":false,
            "per_world_effects":"uncertainty.worlds[].queued_selection_effects"});
        }
        result["uncertainty"] = json!({"kind":"public_uncertainty_mixture","distribution":"independent_public_prior",
            "event_id":if transform_batch {request["selection_context"]["public_source"]["event_id"].clone()}else{event["id"].clone()},
            "public_conditions":public_conditions,
            "sample_count":count,"scored_worlds":scored,"failed_worlds":count-scored,"complete":scored==count,
            "seed_source":"public_features_sha256_only","public_features_sha256":public_hash,
            "reward_pool":if full_unlock{"full_unlock"}else{"known"},
            "assumptions":if full_unlock {vec![if transform_batch {"original_v0.107.1_single_player_full_unlock_character_card_catalogue"}
                else {"original_v0.107.1_single_player_full_unlock_character_relic_catalogue"},
                "unseen_rng_independent_prior_not_conditioned_on_actual_run_seed"]}else{vec![if transform_batch {"unknown_transform_card_catalogue_is_not_assumed"}
                else {"unknown_relic_grab_bag_is_not_assumed"},
                "unseen_rng_independent_prior_not_conditioned_on_actual_run_seed"]},"worlds":worlds});
        Ok(result)
    });
    Ok(result.unwrap_or_else(unsupported))
}

/// Recognize only explicit native transform selection semantics for the separate sampling domain.
fn is_transform_batch(request: &Value) -> bool {
    let source = &request["public_state"];
    text(source, "screen") == "CARD_SELECTION"
        && source["selection"]["max"]
            .as_u64()
            .is_some_and(|count| count > 1)
        && (text(&source["selection"], "operation") == "transform"
            || text(&source["selection"], "kind") == "deck_transform_select")
}

/// Expose upstream encoder tensors and raw inference for same-payload differential checks.
pub fn network_diagnostics(request: &Value) -> Result<Value, String> {
    let obs: PpoObs = serde_json::from_value(
        request
            .get("obs")
            .or_else(|| request.get("ppo_observation"))
            .cloned()
            .ok_or("ppo_diagnostic_observation_missing")?,
    )
    .map_err(|e| format!("ppo_observation:{e}"))?;
    let actions: Vec<ActionDesc> = serde_json::from_value(
        request
            .get("actions")
            .or_else(|| request.get("ppo_actions"))
            .cloned()
            .ok_or("ppo_diagnostic_actions_missing")?,
    )
    .map_err(|e| format!("ppo_actions:{e}"))?;
    if actions.is_empty() {
        return Err("ppo_diagnostic_actions_empty".into());
    }
    with_model(request, |loaded| {
        let feat = encoder::featurize_with(&obs, &actions, loaded.features);
        let (o, a) = (feat.obs_cat.len(), feat.n_actions);
        if loaded.diagnostic_net.is_none() {
            loaded.diagnostic_net = Some(
                PpoNet::from_bytes(&loaded.model_bytes)
                    .map_err(|e| format!("ppo_diagnostic_model_load:{e}"))?,
            );
        }
        let net = loaded
            .diagnostic_net
            .as_ref()
            .ok_or("ppo_diagnostic_model_unavailable")?;
        let (t_max, a_max, cat_dim, cont_dim) =
            (net.t_max(), net.a_max(), net.cat_dim(), net.cont_dim());
        if o + a > t_max || a > a_max {
            return Err(format!(
                "ppo_export_budget:obs={o}:actions={a}:t_max={t_max}:a_max={a_max}"
            ));
        }
        // The upstream forward_raw seam requires its fixed-shape pad contract.
        let pad = encoder::empty_cat();
        let mut cats = vec![0i64; t_max * cat_dim];
        let mut cont = vec![0f32; t_max * cont_dim];
        let mut sec = vec![0i64; t_max];
        for t in 0..t_max {
            let row = if t < o {
                &feat.obs_cat[t]
            } else if t < o + a {
                &feat.act_cat[t - o]
            } else {
                &pad
            };
            cats[t * cat_dim..(t + 1) * cat_dim].copy_from_slice(&row[..cat_dim]);
            if t < o {
                cont[t * cont_dim..(t + 1) * cont_dim]
                    .copy_from_slice(&feat.obs_cont[t][..cont_dim]);
                sec[t] = feat.obs_sec[t];
            } else if t < o + a {
                cont[t * cont_dim..(t + 1) * cont_dim]
                    .copy_from_slice(&feat.act_cont[t - o][..cont_dim]);
                sec[t] = feat.act_sec[t - o];
            }
        }
        let mut pad_mask = vec![true; t_max];
        pad_mask[..o + a].fill(false);
        let mut act_mask = vec![false; a_max];
        act_mask[..a].fill(true);
        let (logits, raw_value) =
            net.forward_raw(&cats, &cont, &sec, &pad_mask, o as i64, &act_mask)?;
        let (probs, value) = net.infer(&feat)?;
        let decision = WireDecision {
            obs: obs.clone(),
            actions: actions.clone(),
            responses: vec![Value::Null; a],
            labels: vec![String::new(); a],
        };
        let grouped = if a == 1 {
            0
        } else {
            loaded
                .policy
                .evaluate_wire(&decision)
                .ok_or("ppo_diagnostic_group_decode_failed")?
                .2
        };
        let rawargmax = (0..a).fold(0, |best, idx| {
            if logits[idx] > logits[best] {
                idx
            } else {
                best
            }
        });
        Ok(
            json!({"ok":true,"obs":obs,"actions":actions,"encoder":{"obs_cat":feat.obs_cat,"obs_cont":feat.obs_cont,"obs_sec":feat.obs_sec,
            "act_cat":feat.act_cat,"act_cont":feat.act_cont,"act_sec":feat.act_sec,"n_actions":a},
            "features":{"reward_upgrade":loaded.features.reward_upgrade,"card_mods":loaded.features.card_mods},
            "raw_logits":&logits[..a],"probs":probs,"value":value,"raw_value":raw_value,"grouped_argmax":grouped,"rawargmax":rawargmax,
            "shape":{"cat_dim":cat_dim,"cont_dim":cont_dim,"t_max":t_max,"a_max":a_max},"model_identity":loaded.identity}),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Use a native-shaped public run whose duplicated basics stay explicit.
    fn source() -> Value {
        json!({"screen":"MAP","available_actions":["choose_map_node"],
            "run":{"character_id":"IRONCLAD","act_id":"0","floor":12,"ascension":0,"hp":"50/80","gold":99,
                "deck_size":3,"deck":[{"card_id":"STRIKE_IRONCLAD","upgraded":false,"card_count":2},
                    {"card_id":"BASH","upgraded":true,"card_count":1}],"relic_ids":["BURNING_BLOOD"],
                "potions":[{"potion_id":null},{"potion_id":"FIRE_POTION"}]},
            "map":{"current":"0,3","boss_node":"16,3","nodes":[
                {"coord":"0,3","node_type":"Monster","children":["1,2","1,4"],"visited":true},
                {"coord":"1,2","node_type":"Elite","children":["16,3"],"visited":false},
                {"coord":"1,4","node_type":"RestSite","children":["16,3"],"visited":false},
                {"coord":"16,3","node_type":"Boss","children":[],"visited":false}],
                "options":[{"i":7,"coord":{"row":1,"col":2}},{"i":12,"coord":{"row":1,"col":4}}]}})
    }

    /// Public aggregation, native floor and sparse map handles survive the original wire builder.
    #[test]
    fn public_map_preserves_native_features_and_handles() {
        let mut request = json!({"public_state":source()});
        let prepared = prepare(&request).unwrap();
        assert_eq!(prepared.decision.obs.deck.len(), 3);
        assert_eq!(prepared.decision.obs.num_upgraded, 1);
        assert_eq!(prepared.decision.obs.total_floor, 11);
        assert_eq!(prepared.decision.obs.act_index, 1);
        assert_eq!(prepared.decision.obs.map_coord, Some((3, 0)));
        assert_eq!(
            prepared.decision.responses[1],
            json!({"type":"choose_map_node","option_index":12})
        );
        assert_eq!(
            prepared.decision.actions,
            vec![ActionDesc::Move { col: 2 }, ActionDesc::Move { col: 4 }]
        );
        request["public_state"]["run"]["act_id"] = Value::Null;
        request["public_state"]["run"]["act_index"] = json!(1);
        assert_eq!(prepare(&request).unwrap().decision.obs.act_index, 1);
        request["public_state"]["run"]["act_id"] = json!("0");
        request["public_state"]["run"]["act_index"] = json!(2);
        assert_eq!(
            prepare(&request).err().unwrap(),
            "ppo_public_act_indices_conflict"
        );
    }

    /// An observed row-zero Ancient is valid only when its exact coordinate exists in the public graph.
    #[test]
    fn native_ancient_map_start_keeps_current_coordinate_and_destination_features() {
        let mut state = source();
        state["run"]["map_coord"] = json!({"row":0,"col":3});
        state["run"]["act_floor"] = json!(1);
        state["map"]["nodes"][0]["node_type"] = json!("Ancient");
        let current = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(current.decision.obs.map_coord, Some((3, 0)));
        assert_eq!(current.decision.obs.act_floor, 1);
        let encoded = encoder::featurize(&current.decision.obs, &current.decision.actions);
        assert_eq!(
            encoded.act_cat[0][4],
            sts2_runai::ppo::vocab::room_index("elite") as i64
        );
        assert_eq!(
            encoded.act_cat[1][4],
            sts2_runai::ppo::vocab::room_index("rest_site") as i64
        );
        assert_eq!(encoded.act_cont[0][encoder::C_DX], -1.0);
        state["map"]["nodes"].as_array_mut().unwrap().remove(0);
        let absent = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(absent.decision.obs.map_coord, None);
        assert_eq!(absent.decision.obs.act_floor, 0);
    }

    /// A stale or contradictory deck count cannot quietly change the champion's state tokens.
    #[test]
    fn public_deck_count_mismatch_is_an_explicit_failure() {
        let mut state = source();
        state["run"]["deck_size"] = json!(4);
        assert_eq!(
            prepare(&json!({"public_state":state})).err().unwrap(),
            "ppo_public_deck_count_mismatch"
        );
    }

    /// Rest target type follows OptionId rather than sparse or reordered native button indices.
    #[test]
    fn smith_only_rest_option_keeps_the_trained_smith_slot() {
        let mut state = source();
        state["screen"] = json!("REST");
        state["available_actions"] = json!(["choose_rest_option"]);
        state["rest"] =
            json!({"options":[{"i":9,"option_id":"SMITH","enabled":true,"requires_target":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            prepared.decision.actions,
            vec![ActionDesc::RestOption { idx: 1 }]
        );
        assert_eq!(
            prepared.decision.responses[0],
            json!({"type":"choose_rest_option","option_index":9})
        );
        assert_eq!(prepared.contexts[0]["intent"], "smith");
    }

    /// The native displayed variables uniquely determine the public localized cost.
    #[test]
    fn chinese_visible_template_recovers_only_displayed_numeric_bindings() {
        let option = json!({"description":"获得[green]5[/green]点最大生命。受到[red]8[/red]点伤害。",
            "description_template":"获得[green]{MaxHp}[/green]点最大生命。受到[red]{Damage}[/red]点伤害。",
            "description_loc_table":"events","description_loc_key":"ABYSSAL_BATHS.pages.ALL.options.LINGER.description"});
        let english = english_option_text(&option, "description").unwrap();
        assert_eq!(
            encoder::event_payload(&strip_tags(&english), 80),
            (0.0, -3.0 / 80.0)
        );
        assert!(english.contains('5') && english.contains('8'));
    }

    /// Repeated fields must agree and two possible bindings stay unknown.
    #[test]
    fn ambiguous_and_complex_templates_cannot_invent_event_costs() {
        assert!(visible_bindings("{A} {B}", "1 2 3").is_err());
        assert!(visible_bindings("{A}::{A}", "7::8").is_err());
        assert_eq!(visible_bindings("{A}::{A}", "7::7").unwrap()["A"], "7");
        assert!(visible_bindings("{Amount:plural:card:cards}", "2 cards").is_err());
    }

    /// A native WillKillPlayer verdict, not a blind description parse, owns the event veto.
    #[test]
    fn native_lethal_event_option_is_not_scored_when_a_survivor_exists() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"SLIPPERY_BRIDGE","options":[
            {"i":3,"text_key":"SLIPPERY_BRIDGE.options.HOLD","label":"Hold On","description":"","locked":false,"kill":true},
            {"i":8,"text_key":"SLIPPERY_BRIDGE.options.LEAVE","label":"Leave","description":"","locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            prepared.decision.responses,
            vec![json!({"type":"choose_event_option","option_index":8})]
        );
        state["event"]["options"][0]["description"] = json!("Take 50 HP damage.");
        state["event"]["options"][0]["kill"] = Value::Null;
        let unknown = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            unknown.decision.responses,
            vec![json!({"type":"choose_event_option","option_index":8})]
        );
        state["event"]["options"][0]["kill"] = json!(false);
        assert_eq!(
            prepare(&json!({"public_state":state}))
                .unwrap()
                .decision
                .actions
                .len(),
            2
        );
    }

    /// Visible Neow relics recover pickup effects without injecting engine preview deltas.
    #[test]
    fn neow_revealed_pickups_preserve_publicly_determined_effects() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["run"]["floor"] = json!(1);
        state["event"] = json!({"id":"NEOW","options":[
            {"i":0,"text_key":"NEOW.pages.INITIAL.options.WINGED_BOOTS","label":"Winged Boots",
                "description":"Gain a positive relic.","preview_relic_ids":["WINGED_BOOTS"],"locked":false,"kill":false},
            {"i":1,"text_key":"NEOW.pages.INITIAL.options.LOST_COFFER","label":"Lost Coffer",
                "description":"Gain a positive relic.","preview_relic_ids":["LOST_COFFER"],"locked":false,"kill":false},
            {"i":2,"text_key":"NEOW.pages.INITIAL.options.SILKEN_TRESS","label":"Silken Tress",
                "description":"Gain a cursed relic.","preview_relic_ids":["SILKEN_TRESS"],"locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        let options = prepared.decision.obs.event_options.as_ref().unwrap();
        assert_eq!(
            options.iter().map(|o| o.relics_gained).collect::<Vec<_>>(),
            vec![Some(1), Some(1), Some(1)]
        );
        assert_eq!(options[1].potions_gained, Some(1));
        assert_eq!(options[1].cards_gained, Some(1));
        assert_eq!(options[2].gold_delta, Some(-99));
        state["run"]["gold"] = json!(37);
        let changed = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            changed.decision.obs.event_options.as_ref().unwrap()[2].gold_delta,
            Some(-37)
        );
    }

    /// The actual final Neow page proceeds without a relic identity or localized inference inputs.
    #[test]
    fn neow_sole_native_proceed_preserves_its_sparse_handle() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"NEOW","options":[
            {"i":13,"text_key":"NEOW.pages.DONE.options.PROCEED","label":"继续",
                "description":"离开这里。","preview_relic_ids":[],"locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(prepared.decision.actions.len(), 1);
        assert_eq!(
            prepared.decision.responses,
            vec![json!({"type":"choose_event_option","option_index":13})]
        );
        assert_eq!(prepared.contexts, vec![Value::Null]);
    }

    /// Wellspring's potion offer respects the original veto while deferred card trades stay deferred.
    #[test]
    fn wellspring_public_rules_preserve_potion_gain_and_sozu_veto() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"Wellspring","options":[
            {"i":4,"text_key":"WELLSPRING.pages.INITIAL.options.BOTTLE","label":"装瓶",
                "description":"获取1瓶随机药水。","locked":false,"kill":false},
            {"i":9,"text_key":"WELLSPRING.pages.INITIAL.options.BATHE","label":"沐浴",
                "description":"移除1张牌，获得1张内疚。","locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        let options = prepared.decision.obs.event_options.as_ref().unwrap();
        assert_eq!(options[0].potions_gained, Some(1));
        assert_eq!(options[1].cards_gained, Some(0));
        assert_eq!(options[1].cards_lost, Some(0));
        state["run"]["relic_ids"]
            .as_array_mut()
            .unwrap()
            .push(json!("SOZU"));
        let veto = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            veto.decision.obs.event_options.as_ref().unwrap()[0].potions_gained,
            Some(0)
        );
        assert_eq!(
            veto.decision.responses[0],
            json!({"type":"choose_event_option","option_index":4})
        );
    }

    /// Use only revealed offers; Bones' future grants and every actual RNG seed stay absent.
    fn uncertain_neow_request() -> Value {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["run"]["floor"] = json!(1);
        state["event"] = json!({"id":"NEOW","options":[
            {"i":0,"text_key":"NEOW.pages.INITIAL.options.WINGED_BOOTS","label":"Winged Boots",
                "description":"Gain a positive relic.","preview_relic_ids":["WINGED_BOOTS"],"locked":false,"kill":false},
            {"i":1,"text_key":"NEOW.pages.INITIAL.options.LOST_COFFER","label":"Lost Coffer",
                "description":"Gain a positive relic.","preview_relic_ids":["LOST_COFFER"],"locked":false,"kill":false},
            {"i":2,"text_key":"NEOW.pages.INITIAL.options.NEOWS_BONES","label":"Neow's Bones",
                "description":"Gain a cursed relic.","preview_relic_ids":["NEOWS_BONES"],"locked":false,"kill":false}]});
        json!({"public_state":state,"model_dir":concat!(env!("CARGO_MANIFEST_DIR"),"/../../../vendor/spire-mentor/sts2-pycli/data/ppo"),
            "public_uncertainty":{"worlds":32,"full_unlock_catalogue":true}})
    }

    /// The public sampler averages every actual forward and cannot depend on real seed/cursor fields.
    #[test]
    fn public_uncertainty_retains_worlds_and_ignores_private_seed_fields() {
        let mut request = uncertain_neow_request();
        let result = policy_evaluate(&request).unwrap();
        assert_eq!(result["ok"], true);
        assert_eq!(result["decision_pipeline"], "public_uncertainty_mixture");
        assert_eq!(result["uncertainty"]["complete"], true);
        assert!(result["obs"].is_null());
        let worlds = rows(&result["uncertainty"], "worlds");
        assert_eq!(worlds.len(), 32);
        for (idx, candidate) in rows(&result, "candidates").iter().enumerate() {
            let mean = worlds
                .iter()
                .map(|world| world["probs"][idx].as_f64().unwrap())
                .sum::<f64>()
                / 32.0;
            assert!((candidate["probability"].as_f64().unwrap() - mean).abs() < 1e-7);
        }
        let mean = worlds
            .iter()
            .map(|world| world["value"].as_f64().unwrap())
            .sum::<f64>()
            / 32.0;
        assert_eq!(result["value"].as_f64().unwrap(), mean);
        request["public_state"]["run"]["seed"] = json!("ACTUAL-SEED-MUST-NOT-BE-USED");
        request["public_state"]["run"]["rng"] = json!({"rewards":123456,"niche":99999});
        request["scenario_seed"] = json!(987654321);
        let different_private = policy_evaluate(&request).unwrap();
        assert_eq!(result["uncertainty"], different_private["uncertainty"]);
        assert_eq!(result["candidates"], different_private["candidates"]);
        request["public_uncertainty"]["full_unlock_catalogue"] = json!(false);
        let unknown_pool = policy_evaluate(&request).unwrap();
        assert_eq!(unknown_pool["ok"], false);
        assert_eq!(unknown_pool["uncertainty"]["complete"], false);
        assert_eq!(rows(&unknown_pool["uncertainty"], "worlds").len(), 32);
        assert!(
            unknown_pool["uncertainty"]["failed_worlds"]
                .as_u64()
                .unwrap()
                > 0
        );
        assert!(rows(&unknown_pool, "candidates").is_empty());
    }

    /// Mean probabilities retain the trained pooled-intent decoder rather than individual argmax.
    #[test]
    fn public_uncertainty_uses_original_group_decoder() {
        let request = uncertain_neow_request();
        with_model(&request, |loaded| {
            let actions = vec![
                ActionDesc::ShopRemoveCard { deck_idx: 0 },
                ActionDesc::ShopRemoveCard { deck_idx: 1 },
                ActionDesc::ShopBuyCard { idx: 0 },
            ];
            assert_eq!(
                loaded
                    .policy
                    .decode_probabilities(&actions, &[0.3, 0.3, 0.4]),
                Some(0)
            );
            Ok(())
        })
        .unwrap();
    }

    /// Shop removal is fan-out before inference and an exact carried target afterward.
    #[test]
    fn shop_remove_target_survives_the_native_selection_transition() {
        let mut state = source();
        state["screen"] = json!("SHOP");
        state["available_actions"] = json!(["remove_card_at_shop", "close_shop_inventory"]);
        state["shop"] = json!({"open":true,"cards":[],"relics":[],"potions":[],"remove":{"price":75,"affordable":true,"available":true}});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        let target = prepared
            .contexts
            .iter()
            .find(|c| c["target"]["id"] == "BASH")
            .unwrap()
            .clone();
        assert_eq!(prepared.decision.actions.len(), 4);
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_card_select","min":1,"max":1,"selected":0,"confirm":false,
            "cards":[{"i":14,"card_id":"BASH","upgraded":true},{"i":7,"card_id":"STRIKE_IRONCLAD","upgraded":false}]});
        let selection = prepare(&json!({"public_state":state,"selection_context":target})).unwrap();
        assert_eq!(
            selection.decision.responses,
            vec![json!({"type":"select_deck_card","option_index":14})]
        );
        state["selection"]["selected"] = json!(1);
        state["selection"]["confirm"] = json!(true);
        state["available_actions"] = json!(["select_deck_card", "confirm_selection"]);
        let confirmation = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            confirmation.decision.responses,
            vec![json!({"type":"confirm_selection"})]
        );
    }

    /// An unknown generic selector cannot silently become a removal or grant screen.
    #[test]
    fn unclassified_card_selection_is_not_guessed_from_the_prompt() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["selection"] = json!({"kind":"deck_card_select","prompt":"Remove a card?","min":1,"max":1,
            "cards":[{"i":0,"card_id":"BASH","upgraded":true}]});
        assert_eq!(
            prepare(&json!({"public_state":state})).err().unwrap(),
            "ppo_card_selection_operation_unknown"
        );
    }

    /// Eligible-list positions remain distinct from master-deck positions at A5 and on sparse native handles.
    #[test]
    fn mutation_selection_indices_address_the_eligible_list_once() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["run"]["deck"] = json!([{"card_id":"ASCENDERS_BANE","upgraded":false},
            {"card_id":"STRIKE_IRONCLAD","upgraded":false},{"card_id":"BASH","upgraded":false}]);
        state["selection"] = json!({"min":1,"max":1,"selected":0,"confirm":false,"enchantment_id":"SHARP",
            "cards":[{"i":12,"card_id":"STRIKE_IRONCLAD","upgraded":false},{"i":8,"card_id":"BASH","upgraded":false}]});
        for operation in ["upgrade", "transform", "enchant"] {
            state["selection"]["kind"] = json!(format!("deck_{operation}_select"));
            state["selection"]["operation"] = json!(operation);
            let prepared = prepare(&json!({"public_state":state})).unwrap();
            let actions = match operation {
                "upgrade" => vec![
                    ActionDesc::UpgradeDeckCard { idx: 0 },
                    ActionDesc::UpgradeDeckCard { idx: 1 },
                ],
                "transform" => vec![
                    ActionDesc::TransformDeckCard { idx: 0 },
                    ActionDesc::TransformDeckCard { idx: 1 },
                ],
                _ => vec![
                    ActionDesc::EnchantDeckCard { idx: 0 },
                    ActionDesc::EnchantDeckCard { idx: 1 },
                ],
            };
            assert_eq!(prepared.decision.actions, actions);
            assert_eq!(
                prepared.decision.responses,
                vec![
                    json!({"type":"select_deck_card","option_index":12}),
                    json!({"type":"select_deck_card","option_index":8})
                ]
            );
            let encoded = encoder::featurize_with(
                &prepared.decision.obs,
                &prepared.decision.actions,
                Features::default(),
            );
            assert_eq!(
                encoded.act_cat[0][0],
                sts2_runai::ppo::vocab::cards().index("STRIKE_IRONCLAD") as i64
            );
            assert_eq!(
                encoded.act_cat[1][0],
                sts2_runai::ppo::vocab::cards().index("BASH") as i64
            );
        }
    }

    /// A cached map's previous room cannot override a current native coordinate or masquerade as one.
    #[test]
    fn historical_map_cursor_is_not_a_current_room_fact() {
        let mut state = source();
        state["screen"] = json!("REST");
        state["available_actions"] = json!(["choose_rest_option"]);
        state["rest"] =
            json!({"options":[{"i":9,"option_id":"SMITH","enabled":true,"requires_target":false}]});
        let mut historical = state["map"].take();
        historical["status"] = json!("historical_public_map");
        historical["last_observed_current"] = json!("1,4");
        historical.as_object_mut().unwrap().remove("current");
        historical["nodes"]
            .as_array_mut()
            .unwrap()
            .push(json!({"coord":"2,2","node_type":"RestSite","children":["16,3"]}));
        state["run"]["map_coord"] = json!({"row":2,"col":2});
        state["run"]["act_floor"] = json!(3);
        let mut request = json!({"public_state":state,"public_map":historical});
        let current = prepare(&request).unwrap();
        assert_eq!(current.decision.obs.map_coord, Some((2, 2)));
        assert_eq!(current.decision.obs.act_floor, 3);
        request["public_state"]["run"]
            .as_object_mut()
            .unwrap()
            .remove("map_coord");
        request["public_state"]["run"]
            .as_object_mut()
            .unwrap()
            .remove("act_floor");
        let unknown = prepare(&request).unwrap();
        assert_eq!(unknown.decision.obs.map_coord, None);
        assert!(unknown
            .warnings
            .iter()
            .any(|warning| warning == "historical_map_topology_current_coordinate_unknown"));
    }

    /// Optional generated palettes use the trained card-reward skip and actual native skip button.
    #[test]
    fn generated_palette_uses_card_reward_actions_and_native_skip() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card", "skip_selection"]);
        state["selection"] = json!({"kind":"choose_card_select","underlying_screen":"EVENT","min":0,"max":1,"can_skip":true,
            "selected":0,"confirm":false,"cards":[{"i":42,"card_id":"POMMEL_STRIKE","upgraded":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            prepared.decision.actions,
            vec![
                ActionDesc::PickCardReward { idx: Some(0) },
                ActionDesc::PickCardReward { idx: None }
            ]
        );
        assert_eq!(
            prepared.decision.responses,
            vec![
                json!({"type":"select_deck_card","option_index":42}),
                json!({"type":"skip_selection"})
            ]
        );
    }

    /// Candidate-specific enchant arrays must reach the original encoder when card_mods is enabled.
    #[test]
    fn selection_candidate_enchantments_survive_their_own_option_lists() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["run"]["deck"] = json!([{"card_id":"STRIKE_IRONCLAD","upgraded":false,"card_count":3,
            "enchantment":{"enchantment_id":"SHARP","amount":2}}]);
        state["selection"] = json!({"min":1,"max":1,"selected":0,"confirm":false,"cards":[
            {"i":8,"card_id":"STRIKE_IRONCLAD","upgraded":false,"enchantment":{"enchantment_id":"SHARP","amount":2}}]});
        for operation in ["smith", "remove", "grant"] {
            state["selection"]["kind"] = json!(match operation {
                "grant" => "choose_card_select",
                "smith" => "deck_upgrade_select",
                _ => "deck_card_select",
            });
            state["selection"]["operation"] = json!(if operation == "smith" {
                "upgrade"
            } else {
                operation
            });
            let carry = if operation == "smith" {
                context(&state, "smith", &Value::Null, 0)
            } else {
                Value::Null
            };
            let prepared =
                prepare(&json!({"public_state":state,"selection_context":carry})).unwrap();
            let encoded = encoder::featurize_with(
                &prepared.decision.obs,
                &prepared.decision.actions,
                Features {
                    card_mods: true,
                    ..Features::default()
                },
            );
            assert_eq!(
                encoded.act_cat[0][11],
                sts2_runai::ppo::vocab::enchantments().index("SHARP") as i64
            );
        }
    }

    /// A fixed event identity is carried from the original public-resource rule until a visible preview confirms it.
    #[test]
    fn fixed_event_enchant_origin_recovers_initial_selection_identity() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"WOOD_CARVINGS","options":[{"i":7,"text_key":"WOOD_CARVINGS.pages.INITIAL.options.SNAKE",
            "label":"Snake","description":"Enchant a card with Slither.","locked":false,"kill":false}]});
        let event = prepare(&json!({"public_state":state})).unwrap();
        let carry = event.contexts[0].clone();
        assert_eq!(carry["intent"], "event_enchant");
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_enchant_select","operation":"enchant","min":1,"max":1,"selected":0,
            "cards":[{"i":2,"card_id":"STRIKE_IRONCLAD","upgraded":false}],"enchantment_id":null});
        let prepared = prepare(&json!({"public_state":state,"selection_context":carry})).unwrap();
        assert_eq!(
            sts2_core::cards::EnchantmentId::from_wire_name(
                prepared
                    .decision
                    .obs
                    .pending_enchantment
                    .as_ref()
                    .unwrap()
                    .as_ref()
                    .unwrap()
            ),
            Some(sts2_core::cards::EnchantmentId::Slither)
        );
        state["selection"]["enchantment_id"] = json!("SHARP");
        assert_eq!(
            prepare(&json!({"public_state":state,"selection_context":carry}))
                .err()
                .unwrap(),
            "ppo_public_enchantment_identity_conflict"
        );
    }

    /// Original registry identities keep every known pending enchantment out of the OOV embedding.
    #[test]
    fn pending_enchantment_titles_use_the_original_registry_vocabulary() {
        use sts2_core::cards::EnchantmentId;
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_enchant_select","operation":"enchant",
            "min":1,"max":1,"selected":0,"confirm":false,
            "cards":[{"i":14,"card_id":"STRIKE_IRONCLAD","upgraded":false},
                {"i":20,"card_id":"BASH","upgraded":true}]});
        for id in EnchantmentId::ALL {
            state["selection"]["enchantment_id"] = json!(id.wire_name());
            let prepared = prepare(&json!({"public_state":state})).unwrap();
            assert_eq!(
                prepared.decision.obs.pending_enchantment,
                Some(Some(id.python_name().to_string()))
            );
            let encoded = encoder::featurize(&prepared.decision.obs, &prepared.decision.actions);
            let expected = sts2_runai::ppo::vocab::enchantments().index(id.python_name()) as i64;
            assert_eq!(encoded.obs_cat[0][11], expected, "{id:?}");
            assert!(
                encoded.act_cat.iter().all(|row| row[11] == expected),
                "{id:?}"
            );
        }
        state["selection"]["enchantment_id"] = json!("UNKNOWN_FUTURE_ENCHANTMENT");
        let unknown = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            unknown.decision.obs.pending_enchantment,
            Some(Some("UNKNOWN_FUTURE_ENCHANTMENT".into()))
        );
    }

    /// Public doll and grave effects retain original reward counts and visible add-card counter consequences.
    #[test]
    fn public_fixed_events_use_original_reward_and_add_card_rules() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"DOLL_ROOM","options":[
            {"i":4,"text_key":"DOLL_ROOM.pages.INITIAL.options.RANDOM","label":"随机","description":"获得一个玩偶遗物。","locked":false,"kill":false},
            {"i":7,"text_key":"DOLL_ROOM.pages.TAKE.options.TAKE","label":"挣扎先生","description":"拿走这个玩偶。","preview_relic_ids":["MR_STRUGGLES"],"locked":false,"kill":false}]});
        let dolls = prepare(&json!({"public_state":state})).unwrap();
        let options = dolls.decision.obs.event_options.as_ref().unwrap();
        assert!(options.iter().all(|option| option.relics_gained == Some(1)));
        assert!(options.iter().all(|option| option.cards_gained == Some(0)));
        assert_eq!(
            dolls.decision.responses[1],
            json!({"type":"choose_event_option","option_index":7})
        );
        state["run"]["relic_ids"] = json!(["BURNING_BLOOD", "BOOK_OF_FIVE_RINGS"]);
        state["run"]["relic_stacks"] = json!([null, 4]);
        state["run"]["relic_is_melted"] = json!([false, false]);
        state["event"] = json!({"id":"GRAVE_OF_THE_FORGOTTEN","options":[
            {"i":11,"text_key":"GRAVE_OF_THE_FORGOTTEN.pages.INITIAL.options.CONFRONT","label":"面对真相","description":"获得腐朽并附魔一张牌。","locked":false,"kill":false},
            {"i":15,"text_key":"GRAVE_OF_THE_FORGOTTEN.pages.INITIAL.options.ACCEPT","label":"接受","description":"获得遗忘之魂。","locked":false,"kill":false}]});
        let grave = prepare(&json!({"public_state":state})).unwrap();
        let options = grave.decision.obs.event_options.as_ref().unwrap();
        assert_eq!(options[0].cards_gained, Some(1));
        assert_eq!(options[0].hp_delta, Some(20));
        assert_eq!(options[1].relics_gained, Some(1));
        assert_eq!(options[1].cards_gained, Some(0));
        state["run"]["relic_stacks"][1] = Value::Null;
        assert!(prepare(&json!({"public_state":state})).is_err());
    }

    /// Actual Regent413 trade bindings preserve original potion-loss tokens and reject missing identities.
    #[test]
    fn future_of_potions_public_trade_preserves_original_effect_and_identity() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["run"]["character_id"] = json!("REGENT");
        state["run"]["potions"] = json!([
            {"potion_id":"POTION_SHAPED_ROCK"}, {"potion_id":"DUPLICATOR"}]);
        state["event"] = json!({"id":"TheFutureOfPotions","options":[
            {"i":0,"text_key":"THE_FUTURE_OF_POTIONS.pages.INITIAL.options.POTION",
                "label":"Insert Token Potion","description":"Lose POTION_SHAPED_ROCK.title. Obtain an Upgraded Common Skill.",
                "vars":{"Potion":"POTION_SHAPED_ROCK.title","Rarity":"Common","Type":"Skill"},"locked":false,"kill":false},
            {"i":1,"text_key":"THE_FUTURE_OF_POTIONS.pages.INITIAL.options.POTION",
                "label":"Insert Uncommon Potion","description":"Lose DUPLICATOR.title. Obtain an Upgraded Uncommon Attack.",
                "vars":{"Potion":"DUPLICATOR.title","Rarity":"Uncommon","Type":"Attack"},"locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        let options = prepared.decision.obs.event_options.as_ref().unwrap();
        assert!(options.iter().all(|option| option.potions_lost == Some(1)));
        assert!(options.iter().all(|option| option.cards_gained == Some(0)));
        let encoded = encoder::featurize(&prepared.decision.obs, &prepared.decision.actions);
        assert!(encoded.act_cont.iter().all(|row| row[encoder::C_POTION_LOSS] == 1.0));
        state["event"]["options"][0]["vars"]["Potion"] = Value::Null;
        assert_eq!(prepare(&json!({"public_state":state})).err().unwrap(),
            "ppo_future_of_potions_public_potion_identity_missing");
        state["event"]["options"][0]["vars"]["Potion"] = json!("NOT_HELD.title");
        assert_eq!(prepare(&json!({"public_state":state})).err().unwrap(),
            "ppo_future_of_potions_public_potion_not_presented");
    }

    /// Tea prices and relic quantities come from the original fixed event rather than incomplete prose.
    #[test]
    fn tea_master_public_rule_preserves_fixed_costs_and_relic_gains() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["run"]["gold"] = json!(200);
        state["event"] = json!({"id":"TEA_MASTER","options":[
            {"i":0,"text_key":"TEA_MASTER.pages.INITIAL.options.BONE_TEA","label":"骨茶","description":"获得骨茶遗物。","locked":false,"kill":false},
            {"i":1,"text_key":"TEA_MASTER.pages.INITIAL.options.EMBER_TEA","label":"余烬茶","description":"获得余烬茶遗物。","locked":false,"kill":false},
            {"i":2,"text_key":"TEA_MASTER.pages.INITIAL.options.TEA_OF_DISCOURTESY","label":"无礼之茶","description":"免费获得遗物。","locked":false,"kill":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        let options = prepared.decision.obs.event_options.as_ref().unwrap();
        assert_eq!(
            options
                .iter()
                .map(|option| option.gold_delta)
                .collect::<Vec<_>>(),
            vec![Some(-50), Some(-150), Some(0)]
        );
        assert_eq!(
            options
                .iter()
                .map(|option| option.relics_gained)
                .collect::<Vec<_>>(),
            vec![Some(1), Some(1), Some(1)]
        );
    }

    /// Deferred relic declines cannot abandon an unassessed row, and duplicate resource identities stay explicit.
    #[test]
    fn reward_continuation_preserves_other_rows_and_each_neural_step() {
        let mut state = source();
        state["screen"] = json!("REWARD");
        state["available_actions"] = json!(["claim_reward", "proceed"]);
        state["reward"] = json!({"pending_card_choice":false,"can_proceed":true,"rewards":[
            {"i":3,"reward_type":"Relic","relic_id":"VAJRA","claimable":true},
            {"i":9,"reward_type":"Relic","relic_id":"ANCHOR","claimable":true}]});
        let carry = reward_context(
            &state,
            &["RELIC:VAJRA".into(), "RELIC:ANCHOR".into()],
            &["RELIC:ANCHOR".into()],
        );
        let mut request = uncertain_neow_request();
        request["public_state"] = state.clone();
        request["selection_context"] = carry;
        let result = policy_evaluate(&request).unwrap();
        assert_eq!(result["ok"], true);
        assert_eq!(rows(&result, "policy_steps").len(), 1);
        assert_eq!(result["policy_steps"][0]["reward_row"], "RELIC:VAJRA");
        assert_eq!(result["policy_steps"][0]["executed"], false);
        if result["best_action"]["type"] == "proceed" {
            assert!(rows(&result, "logical_declines_pending").contains(&json!("RELIC:VAJRA")));
            assert!(rows(&result, "logical_declines_pending").contains(&json!("RELIC:ANCHOR")));
        } else {
            assert_eq!(
                result["best_action"],
                json!({"type":"claim_reward","option_index":3})
            );
        }
        state["reward"]["rewards"][1]["relic_id"] = json!("VAJRA");
        request["public_state"] = state;
        let repeated = policy_evaluate(&request).unwrap();
        assert_eq!(repeated["ok"], false);
        assert_eq!(
            repeated["errors"][0],
            "ppo_reward_repeated_identity_without_stable_handle"
        );
        request["selection_context"] = Value::Null;
        request["public_state"]["reward"]["rewards"] = json!([{ "i":4,"reward_type":"Potion","potion_id":"BLOCK_POTION",
            "claimable":true,"potion_procurement_allowed":false,"potion_slot_available":true,"potion_can_receive":false}]);
        let veto = policy_evaluate(&request).unwrap();
        assert_eq!(veto["best_action"], json!({"type":"proceed"}));
        assert_eq!(veto["candidates"].as_array().unwrap().len(), 1);
        assert!(veto["value"].is_null());
        assert_eq!(veto["policy_steps"][0]["neural_inference"], false);
    }

    /// The two native replacement actions retain the original compressed held-slot index and exact reward row.
    #[test]
    fn potion_swap_keeps_native_slots_and_observed_claim_followup() {
        let mut state = source();
        state["screen"] = json!("REWARD");
        state["available_actions"] = json!(["claim_reward", "discard_potion", "proceed"]);
        state["run"]["potions"] = json!([{"i":4,"potion_id":"FIRE_POTION","occupied":true,"discard":true},
            {"i":8,"potion_id":"DEXTERITY_POTION","occupied":true,"discard":true}]);
        state["reward"] = json!({"pending_card_choice":false,"can_proceed":true,"rewards":[
            {"i":3,"reward_type":"Potion","potion_id":"STRENGTH_POTION","claimable":true,"potion_slot_available":false,"potion_procurement_allowed":true,"potion_can_receive":false},
            {"i":9,"reward_type":"Potion","potion_id":"BLOCK_POTION","claimable":true,"potion_slot_available":false,"potion_procurement_allowed":true,"potion_can_receive":false}]});
        let page: Vec<String> = vec![
            "POTION:STRENGTH_POTION".into(),
            "POTION:BLOCK_POTION".into(),
        ];
        let deferred = vec![page[0].clone()];
        let carry = reward_context(&state, &page, &deferred);
        let remaining = vec![(page[1].clone(), state["reward"]["rewards"][1].clone())];
        let request = json!({"public_state":state});
        let prepared =
            prepare_reward_row(&request, &state["reward"]["rewards"][1], &remaining, &carry)
                .unwrap();
        assert_eq!(
            prepared.decision.actions,
            vec![
                ActionDesc::PotionSwap { discard_slot: None },
                ActionDesc::PotionSwap {
                    discard_slot: Some(0)
                },
                ActionDesc::PotionSwap {
                    discard_slot: Some(1)
                }
            ]
        );
        assert_eq!(
            prepared.decision.responses[2],
            json!({"type":"discard_potion","option_index":8})
        );
        let pending = prepared.contexts[2].clone();
        state["run"]["potions"][1] =
            json!({"i":8,"potion_id":null,"occupied":false,"discard":false});
        for row in state["reward"]["rewards"].as_array_mut().unwrap() {
            row["potion_slot_available"] = json!(true);
            row["potion_can_receive"] = json!(true);
        }
        let followup =
            policy_evaluate(&json!({"public_state":state,"selection_context":pending})).unwrap();
        assert_eq!(
            followup["best_action"],
            json!({"type":"claim_reward","option_index":9})
        );
        assert!(followup["next_context"]["claim_after_discard"].is_null());
        assert!(followup["value"].is_null());
    }

    /// Native full-belt readiness is proven by public reward status even while its claim button is disabled.
    #[test]
    fn disabled_full_belt_reward_requires_visible_unclaimed_native_status() {
        let mut state = source();
        state["screen"] = json!("REWARD");
        state["available_actions"] = json!(["discard_potion", "proceed"]);
        state["run"]["potions"] =
            json!([{"i":6,"potion_id":"FIRE_POTION","occupied":true,"discard":true}]);
        let mut row = json!({"i":9,"reward_type":"Potion","potion_id":"BLOCK_POTION","claimable":false,
            "visible":true,"successfully_selected":false,"potion_slot_available":false,"potion_procurement_allowed":true});
        let request = json!({"public_state":state});
        let prepared = prepare_reward_row(&request, &row, &[], &Value::Null).unwrap();
        assert_eq!(
            prepared.decision.responses[1],
            json!({"type":"discard_potion","option_index":6})
        );
        row["successfully_selected"] = json!(true);
        assert!(!pending_reward_row(&row));
        assert!(prepare_reward_row(&request, &row, &[], &Value::Null).is_err());
        row["successfully_selected"] = Value::Null;
        assert!(prepare_reward_row(&request, &row, &[], &Value::Null).is_err());
        row["successfully_selected"] = json!(false);
        row["visible"] = json!(false);
        assert!(!pending_reward_row(&row));
    }

    /// Queued native targets change only the hypothetical next observation and never the input snapshot.
    #[test]
    fn mandatory_batch_projects_selected_upgrade_and_removal_before_native_confirm() {
        for operation in ["upgrade", "remove"] {
            let mut state = source();
            state["screen"] = json!("CARD_SELECTION");
            state["available_actions"] = json!(["select_deck_card"]);
            state["run"]["deck"] = json!([
                {"card_id":"STRIKE_IRONCLAD","upgraded":false,"card_count":1},
                {"card_id":"DEFEND_IRONCLAD","upgraded":false,"card_count":1},
                {"card_id":"ANGER","upgraded":false,"card_count":1}]);
            state["selection"] = json!({"kind":if operation=="upgrade"{"deck_upgrade_select"}else{"deck_card_select"},
                "operation":operation,"min":2,"max":2,"selected":1,"confirm":false,"cards":[
                    {"i":4,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":true},
                    {"i":8,"card_id":"DEFEND_IRONCLAD","upgraded":false,"selected":false},
                    {"i":15,"card_id":"ANGER","upgraded":false,"selected":false}]});
            let request = json!({"public_state":state});
            let observed = request.clone();
            let prepared = prepare(&request).unwrap();
            assert_eq!(request, observed);
            assert_eq!(
                prepared.decision.responses,
                vec![
                    json!({"type":"select_deck_card","option_index":8}),
                    json!({"type":"select_deck_card","option_index":15})
                ]
            );
            assert_eq!(
                prepared.queued_effects["scope"],
                "hypothetical_until_native_confirm"
            );
            assert_eq!(
                prepared.queued_effects["applied_original_deck_indices"],
                json!([0])
            );
            if operation == "upgrade" {
                assert_eq!(prepared.decision.obs.deck.len(), 3);
                assert!(prepared.decision.obs.deck[0].1);
                assert_eq!(
                    prepared.decision.obs.upgrade_select_deck_indices,
                    Some(vec![1, 2])
                );
                assert_eq!(
                    prepared.decision.actions,
                    vec![
                        ActionDesc::UpgradeDeckCard { idx: 0 },
                        ActionDesc::UpgradeDeckCard { idx: 1 }
                    ]
                );
            } else {
                assert_eq!(prepared.decision.obs.deck.len(), 2);
                assert_eq!(
                    prepared.queued_effects["projected_to_original_deck_indices"],
                    json!([1, 2])
                );
                assert_eq!(
                    prepared
                        .decision
                        .obs
                        .card_removal_options
                        .as_ref()
                        .unwrap()
                        .len(),
                    2
                );
            }
            let mut complete = request;
            complete["public_state"]["selection"]["selected"] = json!(2);
            complete["public_state"]["selection"]["confirm"] = json!(true);
            complete["public_state"]["available_actions"] = json!(["confirm_selection"]);
            assert_eq!(
                prepare(&complete).unwrap().decision.responses,
                vec![json!({"type":"confirm_selection"})]
            );
        }
    }

    /// A visible enchantment ID does not prove the amount needed to simulate an already-selected card.
    #[test]
    fn mandatory_enchant_batch_preserves_unknown_amount_and_known_public_origin() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_enchant_select","operation":"enchant","min":2,"max":2,
            "selected":1,"confirm":false,"enchantment_id":"SHARP","cards":[
                {"i":3,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":true},
            {"i":9,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":false},
            {"i":15,"card_id":"BASH","upgraded":true,"selected":false}]});
        let mut request = json!({"public_state":state});
        assert_eq!(
            prepare(&request).err().unwrap(),
            "ppo_batch_pending_enchantment_amount_not_public"
        );
        let mut carry = context(&state, "event_enchant", &Value::Null, 0);
        carry["pending_enchantment_id"] = json!("SHARP");
        carry["pending_enchantment_amount"] = json!(2);
        carry["public_source"] = json!({"scope":"synthetic_verified_fixed_descriptor_test"});
        request["selection_context"] = carry.clone();
        let prepared = prepare(&request).unwrap();
        assert_eq!(
            prepared.decision.obs.deck_enchantments.as_ref().unwrap()[0],
            Some(("SHARP".into(), 2))
        );
        assert_eq!(
            prepared.decision.obs.enchant_select_deck_indices,
            Some(vec![1, 2])
        );
        assert_eq!(
            prepared.decision.responses,
            vec![
                json!({"type":"select_deck_card","option_index":9}),
                json!({"type":"select_deck_card","option_index":15})
            ]
        );
        assert_eq!(prepared.contexts[0], carry);
        assert!(same_card(
            &json!({"id":"StrikeIronclad","upgraded":false}),
            &json!({"id":"STRIKE_IRONCLAD","upgraded":false})
        ));
        assert!(!same_card(
            &json!({"id":"StrikeIronclad","upgraded":false}),
            &json!({"id":"STRIKE_SILENT","upgraded":false})
        ));
    }

    /// A public relic pickup proves all three enchant amounts through the original handler, not a prompt guess.
    #[test]
    fn public_pickup_origin_recovers_the_real_multi_enchantment_queue() {
        let mut state = source();
        state["run"]["deck_size"] = json!(5);
        state["run"]["deck"] =
            json!([{"card_id":"STRIKE_IRONCLAD","upgraded":false,"card_count":5}]);
        let wire = base_wire(&json!({"public_state":state})).unwrap();
        let carry =
            public_relic_selection_context(&state, "GNARLED_HAMMER", &wire, CharacterId::Ironclad)
                .unwrap();
        assert_eq!(carry["required_count"], 3);
        assert_eq!(
            carry["pending_enchantment_id"]
                .as_str()
                .and_then(sts2_core::cards::EnchantmentId::from_wire_name),
            Some(sts2_core::cards::EnchantmentId::Sharp)
        );
        assert_eq!(carry["pending_enchantment_amount"], 3);
        state["run"]["relic_ids"] = json!(["BURNING_BLOOD", "GNARLED_HAMMER"]);
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_enchant_select","min":3,"max":3,"selected":1,"confirm":false,
            "cards":(0..5).map(|i|json!({"i":i+11,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":i==0})).collect::<Vec<_>>()});
        let prepared = prepare(&json!({"public_state":state,"selection_context":carry})).unwrap();
        assert_eq!(
            prepared.decision.obs.deck_enchantments.as_ref().unwrap()[0],
            Some(("SHARP".into(), 3))
        );
        assert_eq!(prepared.decision.responses.len(), 4);
        assert!(prepared
            .decision
            .responses
            .iter()
            .all(|response| response["option_index"] != 11));
    }

    /// Fixed one-card relic sources expose their original queue identity before any native preview exists.
    #[test]
    fn fixed_single_enchant_relic_origins_preserve_original_amounts() {
        use sts2_core::cards::EnchantmentId;
        for (relic, card_id, enchantment, amount) in [
            (
                "PUNCH_DAGGER",
                "STRIKE_IRONCLAD",
                EnchantmentId::Momentum,
                5,
            ),
            (
                "ELECTRIC_SHRYMP",
                "DEFEND_IRONCLAD",
                EnchantmentId::Imbued,
                1,
            ),
        ] {
            let mut state = source();
            state["run"]["deck"] = json!([{"card_id":card_id,"upgraded":false,"card_count":3}]);
            let wire = base_wire(&json!({"public_state":state})).unwrap();
            let carry = public_relic_selection_context(&state, relic, &wire, CharacterId::Ironclad)
                .unwrap();
            assert_eq!(carry["required_count"], 1);
            assert_eq!(
                carry["pending_enchantment_id"]
                    .as_str()
                    .and_then(EnchantmentId::from_wire_name),
                Some(enchantment)
            );
            assert_eq!(carry["pending_enchantment_amount"], amount);
            assert_eq!(carry["public_source"]["relic_id"], relic);
            state["screen"] = json!("CARD_SELECTION");
            state["available_actions"] = json!(["select_deck_card"]);
            state["selection"] = json!({"kind":"deck_enchant_select","min":1,"max":1,"selected":0,"enchantment_id":null,
                "cards":[{"i":7,"card_id":card_id,"upgraded":false},{"i":19,"card_id":card_id,"upgraded":false}]});
            let prepared =
                prepare(&json!({"public_state":state,"selection_context":carry})).unwrap();
            assert_eq!(
                prepared
                    .decision
                    .obs
                    .pending_enchantment
                    .as_ref()
                    .unwrap()
                    .as_deref()
                    .and_then(EnchantmentId::from_wire_name),
                Some(enchantment)
            );
            assert_eq!(
                prepared.decision.responses[1],
                json!({"type":"select_deck_card","option_index":19})
            );
        }
    }

    /// The last mandatory target needs no hidden transform outcome or synthetic critic value.
    #[test]
    fn sole_remaining_mandatory_target_is_forced_and_ignores_disabled_rows() {
        let mut state = source();
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_transform_select","min":2,"max":2,"selected":1,"confirm":false,"cards":[
            {"i":14,"card_id":"STRIKE_IRONCLAD","selected":true,"selectable":true},
            {"i":11,"card_id":"STRIKE_IRONCLAD","selected":false,"selectable":true},
            {"i":20,"card_id":"BASH","selected":false,"selectable":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            prepared.decision.responses,
            vec![json!({"type":"select_deck_card","option_index":11})]
        );
        assert_eq!(
            prepared.queued_effects["projection_not_needed_for_forced_action"],
            true
        );
        assert!(prepared.queued_effects["projected_effects_available"] == false);
        let result = score_prepared(&json!({"public_state":state}), &prepared);
        assert_eq!(result["status"], "forced_public_action");
        assert!(result["value"].is_null());
    }

    /// Darv's revealed relics use the original trained boss-relic head and their real native indices.
    #[test]
    fn darv_relic_offers_use_the_corrected_original_ancient_surface() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"DARV","options":[
            {"i":7,"text_key":"DARV.pages.INITIAL.options.RELIC_1","label":"遗物一","description":"公开遗物效果",
                "preview_relic_ids":["YUMMY_COOKIE"],"locked":false},
            {"i":19,"text_key":"DARV.pages.INITIAL.options.RELIC_2","label":"遗物二","description":"公开遗物效果",
                "preview_relic_ids":["BIIIG_HUG"],"locked":false},
            {"i":42,"text_key":"DARV.pages.INITIAL.options.RELIC_3","label":"遗物三","description":"公开遗物效果",
                "preview_relic_ids":["SEAL_OF_GOLD"],"locked":false}]});
        let prepared = prepare(&json!({"public_state":state})).unwrap();
        assert_eq!(
            prepared.decision.actions,
            vec![
                ActionDesc::PickBossRelic { idx: 0 },
                ActionDesc::PickBossRelic { idx: 1 },
                ActionDesc::PickBossRelic { idx: 2 }
            ]
        );
        assert_eq!(
            prepared.decision.obs.boss_relic_options,
            Some(vec![
                "YUMMY_COOKIE".into(),
                "BIIIG_HUG".into(),
                "SEAL_OF_GOLD".into()
            ])
        );
        assert!(prepared.decision.obs.event_options.is_none());
        assert_eq!(
            prepared.decision.responses[1],
            json!({"type":"choose_event_option","option_index":19})
        );
    }

    /// An original lethal-choice veto leaves a forced event action without inventing random reward features.
    #[test]
    fn original_public_veto_can_force_an_uncertain_event_without_any_prior() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["run"]["hp"] = json!("6/80");
        state["event"] = json!({"id":"THIS_OR_THAT","options":[
            {"i":12,"text_key":"THIS_OR_THAT.pages.INITIAL.options.PLAIN","label":"Plain","description":"Lose 6 HP. Gain 48 Gold.","locked":false,"kill":null},
            {"i":31,"text_key":"THIS_OR_THAT.pages.INITIAL.options.ORNATE","label":"Ornate","description":"Add Clumsy. Obtain a random Relic.","locked":false,"kill":null}]});
        let result=policy_evaluate(&json!({"public_state":state,"public_uncertainty":{"worlds":32,"full_unlock_catalogue":false}})).unwrap();
        assert_eq!(
            result["best_action"],
            json!({"type":"choose_event_option","option_index":31})
        );
        assert_eq!(result["status"], "forced_public_action");
        assert_eq!(result["neural_inference"], false);
        assert_eq!(
            result["probability_meaning"],
            "forced_public_action_not_network_inference"
        );
        assert!(result["value"].is_null());
        assert!(result["uncertainty"].is_null());
    }

    /// Independent queued transforms retain every world and mask selected native targets without reading the actual seed.
    #[test]
    fn public_transform_prior_keeps_native_targets_and_visible_hook_state() {
        let mut state = source();
        state["run"]["gold"] = json!(150);
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"MORPHIC_GROVE","options":[{"i":7,
            "text_key":"MORPHIC_GROVE.pages.INITIAL.options.GROUP","label":"Group","description":"Lose all Gold. Transform 2 cards.","locked":false}]});
        let event = prepare(&json!({"public_state":state})).unwrap();
        let carry = event.contexts[0].clone();
        assert_eq!(carry["intent"], "event_transform");
        assert_eq!(carry["required_count"], 2);
        state["run"]["gold"] = json!(0);
        state["screen"] = json!("CARD_SELECTION");
        state["available_actions"] = json!(["select_deck_card"]);
        state["selection"] = json!({"kind":"deck_transform_select","min":2,"max":2,"selected":1,"confirm":false,"cards":[
            {"i":4,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":true},
            {"i":8,"card_id":"STRIKE_IRONCLAD","upgraded":false,"selected":false},
            {"i":15,"card_id":"BASH","upgraded":true,"selected":false}]});
        let mut request = json!({"public_state":state,"selection_context":carry,
            "public_uncertainty":{"worlds":4,"full_unlock_catalogue":true}});
        assert!(prepare(&request)
            .err()
            .unwrap()
            .starts_with(TRANSFORM_UNCERTAINTY_REQUIRED));
        let result = policy_evaluate(&request).unwrap();
        assert_eq!(result["ok"], true, "{result}");
        assert_eq!(result["uncertainty"]["scored_worlds"], 4);
        assert!(result["obs"].is_null());
        assert!(rows(&result, "candidates")
            .iter()
            .all(|candidate| candidate["action"]["option_index"] != 4));
        for world in rows(&result["uncertainty"], "worlds") {
            assert_eq!(world["obs"]["gold"], 0);
            assert_eq!(
                world["queued_selection_effects"]["candidate_space"],
                "native_unselected_targets_only"
            );
        }
        request["public_state"]["run"]["seed"] = json!("PRIVATE_REAL_SEED_MUST_NOT_ENTER_PRIOR");
        request["public_state"]["run"]["rng"] = json!({"cursor":98765});
        assert_eq!(
            policy_evaluate(&request).unwrap()["uncertainty"],
            result["uncertainty"]
        );
        request["public_uncertainty"]["full_unlock_catalogue"] = json!(false);
        let unknown = policy_evaluate(&request).unwrap();
        assert_eq!(unknown["ok"], false);
        assert_eq!(unknown["uncertainty"]["failed_worlds"], 4);
        assert_eq!(rows(&unknown["uncertainty"], "worlds").len(), 4);
        request["public_uncertainty"]["full_unlock_catalogue"] = json!(true);
        request["public_state"]["run"]["hp"] = json!("40/80");
        request["public_state"]["run"]["relic_ids"] =
            json!(["BURNING_BLOOD", "BOOK_OF_FIVE_RINGS"]);
        request["public_state"]["run"]["relic_stacks"] = json!([null, 4]);
        request["public_state"]["run"]["relic_is_melted"] = json!([false, false]);
        let public_counter = policy_evaluate(&request).unwrap();
        assert_eq!(public_counter["ok"], true, "{public_counter}");
        assert!(rows(&public_counter["uncertainty"], "worlds")
            .iter()
            .all(|world| world["obs"]["hp"] == 60));
        request["public_state"]["run"]["relic_is_melted"] = json!([false, true]);
        request["public_state"]["run"]["relic_stacks"] = json!([null, null]);
        let melted = policy_evaluate(&request).unwrap();
        assert_eq!(melted["ok"], true, "{melted}");
        assert!(rows(&melted["uncertainty"], "worlds")
            .iter()
            .all(|world| world["obs"]["hp"] == 40));
        request["public_state"]["run"]["relic_is_melted"] = Value::Null;
        let unknown_active = policy_evaluate(&request).unwrap();
        assert_eq!(unknown_active["ok"], false);
        assert_eq!(unknown_active["uncertainty"]["failed_worlds"], 4);
    }

    /// This or That conditions the displayed roll while every future relic remains an independent hypothesis.
    #[test]
    fn this_or_that_worlds_keep_displayed_gold_without_using_actual_seed() {
        let mut state = source();
        state["screen"] = json!("EVENT");
        state["available_actions"] = json!(["choose_event_option"]);
        state["event"] = json!({"id":"THIS_OR_THAT","options":[
            {"i":0,"text_key":"THIS_OR_THAT.pages.INITIAL.options.PLAIN","label":"This","description":"Lose 6 HP. Gain 48 Gold.","locked":false,"kill":false},
            {"i":1,"text_key":"THIS_OR_THAT.pages.INITIAL.options.ORNATE","label":"That","description":"Add Clumsy. Obtain a random Relic.","locked":false,"kill":false}]});
        let mut request = json!({"public_state":state,"public_uncertainty":{"worlds":32,"full_unlock_catalogue":true}});
        let original = public_uncertainty_parameters(&request).unwrap();
        request["public_state"]["run"]["seed"] = json!("AN_ACTUAL_PRIVATE_SEED");
        assert_eq!(original, public_uncertainty_parameters(&request).unwrap());
        for seed in [7, 31, 99] {
            let prepared = prepare_with_world(
                &request,
                Some(NeowWorld {
                    seed,
                    full_unlock: true,
                }),
            )
            .unwrap();
            assert_eq!(
                prepared.decision.obs.event_options.as_ref().unwrap()[0].gold_delta,
                Some(48)
            );
        }
        request["public_state"]["event"]["options"][0]["description"] = json!("Gain ~55 gold.");
        assert_eq!(
            public_this_or_that_gold(&request["public_state"]["event"])
                .err()
                .unwrap(),
            "ppo_this_or_that_displayed_gold_not_recoverable"
        );
    }
}

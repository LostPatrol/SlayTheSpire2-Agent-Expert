/** Verify dashboard usage, session switching, and explicit controls without a live server. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
// A minimal DOM records rendered values and explicit client event handlers.
const nodes = new Map();

/** Supply the small DOM surface used by the usage panel. */
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent: "", value: "", style: {
    /** Record the slider's paint-only CSS variables. */
    setProperty(name, value) { this[name] = value; },
  }, dataset: {}, scrollTop: 0, scrollHeight: 0,
    classList: {toggle() {}}, replaceChildren(...children) { this.children = children; },
    append(...children) { this.children = [...(this.children || []), ...children]; },
    setAttribute(name, value) { this[name] = value; },
    handlers: new Map(),
    /** Record actual client handlers for explicit interaction tests. */
    addEventListener(name, handler) { this.handlers.set(name, handler); },
    querySelector(selector) { return node(`${String(id)}-${selector}`); }});
  return nodes.get(id);
}

// Evaluate the shipped UI functions while omitting network initialization.
const context = vm.createContext({console, AbortController, crypto: {randomUUID: () => "f1111111-1111-4111-8111-111111111111"},
  setTimeout: () => 1, clearTimeout() {}, setInterval() {}, window: {addEventListener() {}},
  document: {getElementById: node, createElement: () => node(Symbol()), querySelectorAll: () => [], addEventListener() {}}});
// Load the actual shipped client so display behavior cannot drift from the test.
const source = fs.readFileSync(path.join(__dirname, "../src/sts2_agent/web/app.js"), "utf8");
vm.runInContext(source.replace(/initialize\(\);\s*$/, "") +
  "\nglobalThis.usage = {state, renderLiveUsage, renderRequests, renderDecision, renderMetrics, renderSimulation, simulationHorizon, tokenCalibration, estimateTokens, generationMilliseconds, acceptSnapshot, renderControls, renderSession, startGame, initialize, refreshSnapshot, selectNetworkPreset, saveSimulationSettings, submitInstruction};", context);
const ui = context.usage;
// API-counted responses can calibrate a new request before its model identity arrives.
const historical = {model_request_id: "old", requested_model: "flash", actual_model: "flash-v1",
  status: "completed", content: "a".repeat(400), usage: {completion_tokens: 200}};
// The provider reports identity on completion, after live calibration is already needed.
const active = {model_request_id: "new", requested_model: "flash", actual_model: "unknown", status: "generating"};
assert.equal(ui.tokenCalibration(active, [historical, active]), 2);
assert.equal(ui.estimateTokens({...active, reasoning: "a".repeat(40)}, 2), 20);
assert.equal(ui.tokenCalibration(active, [{...historical, status: "failed"}]), 1);

// Generation excludes 8 seconds of prefill; the separate request speed still includes it.
ui.state.snapshot = {model_requests: [{...historical, duration_ms: 10000, generation_ms: 2000, response_streamed: true}]};
ui.renderLiveUsage();
assert.equal(node("token-speed").textContent, "100");
assert.equal(node("speed-state").textContent, "已结束");
assert.equal(node("token-speed").title, "按解码时长计算（首字→完成）");

// Legacy records and nonstream responses must not masquerade as measured generation.
ui.state.snapshot.model_requests = [{...historical, duration_ms: 10000}];
ui.renderLiveUsage();
assert.equal(node("token-speed").textContent, "20");
assert.match(node("token-speed").title, /含首字等待/);
assert.equal(ui.generationMilliseconds({response_streamed: false, generation_ms: 1}), null);
ui.state.snapshot.model_requests = [active];
ui.renderLiveUsage();
assert.equal(node("token-speed").textContent, "—");
assert.equal(node("speed-state").textContent, "等待首字");
// Cancellation suppresses the live estimate but keeps the curve continuous.
for (const status of ["generating", "cancelling", "cancelled"]) {
  ui.state.snapshot = {generation_cancelling: status === "generating", request_pending: status !== "cancelled",
    model_requests: [{...historical, status, duration_ms: 10000, generation_ms: 2000, response_streamed: true}]};
  ui.state.speedHistory = [{time: Date.now(), value: 90}];
  ui.renderLiveUsage();
  assert.equal(node("token-speed").textContent, "—");
  assert.equal(node("speed-state").textContent, status === "cancelled" ? "已取消" : "正在停止输出");
  assert.equal(ui.state.speedHistory.some(point => point.value === 90), true);
  ui.renderDecision(null);
  assert.equal(node("decision-status").textContent, status === "cancelled" ? "本轮生成已取消" : "正在停止输出");
  ui.renderRequests(ui.state.snapshot.model_requests);
  assert.equal(node("request-status").textContent, status === "cancelled" ? "已取消" : "正在停止输出");
}
// The new request can be cancelled before its first text without hiding the preceding output.
ui.state.snapshot = {generation_cancelling: true, request_pending: true, model_requests: [
  {...historical, reasoning: "previous reasoning"}, {...active, status: "cancelling"}]};
ui.renderRequests(ui.state.snapshot.model_requests);
assert.equal(node("stream-text").textContent, "previous reasoning");
assert.equal(node("request-status").textContent, "正在停止输出 · 保留上一轮");
assert.match(node("model-activity").textContent, /^正在停止输出/);
ui.state.snapshot.generation_cancelling = false;
ui.state.snapshot.request_pending = false;
ui.state.snapshot.model_requests[1].status = "cancelled";
ui.renderRequests(ui.state.snapshot.model_requests);
assert.equal(node("request-status").textContent, "已取消 · 保留上一轮");
assert.match(node("model-activity").textContent, /^已取消/);
ui.state.snapshot = {model_requests: [active]};
// Estimated stream output never fills missing billed usage or claims a zero bill.
ui.renderMetrics({unknown_usage_requests: 2});
assert.equal(node("total-tokens").textContent, "未知");
assert.equal(node("completion-tokens").textContent, "未知");
assert.match(node("total-tokens").title, /未知用量请求：2/);
ui.renderMetrics({total_tokens: 123, completion_tokens: 0, unknown_usage_requests: 1});
assert.equal(node("total-tokens").textContent, "123");
assert.equal(node("completion-tokens").textContent, "0");
assert.equal(node("cache-hit-tokens").textContent, "未知");
// A failed backend must remain visible instead of looking like an empty candidate list.
ui.renderSimulation({status: "unavailable", simulation_advice: {
  policy_status: null, candidates: null, errors: ["upstream_mirror_request_failed"]}});
assert.equal(node("side-candidate-meta").textContent, "推演失败");
assert.match(node("bay-candidates").children[1].children[0].textContent, /upstream_mirror_request_failed/);
ui.renderSimulation({status: "unavailable", diagnostics: [{code: "upstream_mirror_request_failed",
  reason: "upstream_mirror_request_failed", error_kind: "ValueError"}]});
assert.match(node("bay-candidates").children[1].children[0].textContent, /upstream_mirror_request_failed.*ValueError/);
ui.renderSimulation({status: "advisory", simulation_advice: {policy_status: "ready", candidates: [
  {action: {type: "choose_reward_card", option_index: 2}, probability: 0.7, selected: true},
  {action: {type: "skip_reward_cards"}, probability: 0.3, selected: false}]}});
assert.equal(node("sim-candidate-count").textContent, "2");
assert.match(node("bay-candidates").children[1].children[0].children[2].textContent, /70.0%.*已选/);
// Persisted action labels can still contain legacy icon paths after the source is fixed.
ui.renderDecision({action: {type: "play_card", card_index: 0},
  action_label: "打出 放血：失去3点生命，获得res://images/ironclad_energy_icon.pngres://images/ironclad_energy_icon.png。"});
assert.equal(node("decision-action").textContent, "打出 放血：失去3点生命，获得⚡⚡。");
// Cleared scene memory outranks the last visible decision's historical scene text.
ui.state.snapshot.scene_plan = "";
ui.renderDecision({action: {type: "end_turn"}, scene_plan: "上一场战斗的目标"});
assert.equal(node("scene-plan").textContent, "—");

// A fresh connected paused run can start automatic play; prior budget use means resume.
const fresh = {seq: 2, run_id: "new-run", status: "paused", desired_state: "paused", connection: {connected: true},
  game_launch: {status: "ready", message: "游戏已就绪，自动游玩保持暂停。"}, metrics: {requests_used: 0, actions_completed: 0}, model_requests: []};
// Both pause and stop explain the remaining worker shutdown without claiming continued generation.
for (const status of ["paused", "stopped"]) {
  ui.renderSession({...fresh, status, request_pending: true, generation_cancelling: true});
  assert.equal(node("phase-detail").hidden, false);
  assert.match(node("phase-detail").textContent, /正在停止模型输出/);
  assert.doesNotMatch(node("phase-detail").textContent, /仍在生成/);
}
ui.state.apiAvailable = true;
ui.state.snapshot = fresh;
ui.renderControls();
assert.equal(node("start").disabled, false);
assert.equal(node("run-toggle").disabled, false);
assert.equal(node("run-toggle").querySelector("span").textContent, "开始");
ui.state.snapshot = {...fresh, budget_started_utc: "2026-10-28T00:00:00Z"};
ui.renderControls();
assert.equal(node("run-toggle").querySelector("span").textContent, "继续");
assert.equal(node("run-toggle").dataset.mode, "resume");
ui.state.snapshot = {...fresh, status: "running", desired_state: "running"};
ui.renderControls();
assert.equal(node("run-toggle").dataset.mode, "pause");
assert.equal(node("run-toggle").querySelector("span").textContent, "暂停");
assert.equal(node("run-toggle").disabled, false);
assert.equal(node("step").disabled, true);
ui.state.snapshot = {...fresh, status: "running", desired_state: "paused", inflight: {action_id: "pending"}};
ui.renderControls();
assert.equal(node("run-toggle").querySelector("span").textContent, "暂停中");
assert.equal(node("run-toggle").disabled, true);

// Starting, disconnected, and terminal snapshots never allow automatic play.
for (const snapshot of [
  {...fresh, game_launch: {status: "starting", message: "正在准备新会话。"}},
  {...fresh, connection: {connected: false}},
  {...fresh, status: "game_over", terminal_evidence: {game_over: {victory: false}}},
]) {
  ui.state.snapshot = snapshot;
  ui.renderControls();
  assert.equal(node("run-toggle").disabled, true);
  assert.equal(node("step").disabled, true);
  assert.equal(node("start").hidden, false);
  assert.equal(node("start").disabled, snapshot.game_launch.status === "starting");
}
ui.renderSession(ui.state.snapshot);
assert.equal(node("phase-detail").hidden, true);
ui.state.snapshot = {...fresh, game_launch: {status: "error", message: "启动失败：未找到游戏程序。"}};
ui.renderSession(ui.state.snapshot);
assert.equal(node("game-launch-banner").hidden, false);
assert.equal(node("game-launch-banner").role, "alert");
assert.equal(node("toast").textContent, "启动失败：未找到游戏程序。");
assert.equal(node("toast").className, "toast error");

// A new run with a lower sequence must clear every old session-bound view cache.
ui.state.runId = "old-run";
ui.state.lastSeq = 900;
ui.state.lastSnapshotSeq = 800;
ui.state.selectedRequest = "old-request";
ui.state.requestPinned = true;
ui.state.decisionCards.set("old-decision", {action: {type: "end_turn"}, reason: "旧会话说明"});
ui.state.historySignature = "old-history";
ui.state.speedHistory = [{time: 1, value: 100}];
ui.state.streamDragging = true;
ui.state.follow = false;
node("history-list").scrollTop = 100;
// The old SSE subscription must not advance the new run's cursor.
let sourceClosed = false;
ui.state.source = {close() { sourceClosed = true; }};
ui.state.streamConnected = true;
ui.acceptSnapshot(fresh);
assert.equal(ui.state.snapshot.run_id, "new-run");
assert.equal(ui.state.lastSeq, 2);
assert.equal(ui.state.lastSnapshotSeq, 2);
assert.equal(ui.state.selectedRequest, "");
assert.equal(ui.state.requestPinned, false);
assert.equal(ui.state.decisionCards.size, 0);
// The old run's curve is cleared; the new run's idle sample may already be added.
assert.equal(ui.state.speedHistory.some(point => point.time === 1), false);
assert.equal(ui.state.streamDragging, false);
assert.equal(ui.state.follow, true);
assert.equal(node("history-list").scrollTop, 0);
assert.equal(sourceClosed, true);
assert.equal(ui.state.source, null);
assert.equal(node("decision-reason").textContent, "完整决策到达后，说明会保留在这里。");
assert.equal(node("stream-text").textContent, "");
ui.acceptSnapshot({...fresh, seq: 1, plan: "过期方案"});
assert.equal(ui.state.lastSnapshotSeq, 2);
assert.equal(ui.state.snapshot.plan, undefined);

// Projection horizon and coverage are displayed explicitly, never as a survival percentage.
assert.equal(ui.simulationHorizon({horizon: "current_player_turn_before_end_turn_effects"}), "当前回合出牌后");
assert.equal(ui.simulationHorizon({horizon: "next_player_turn_before_draw"}), "下一回合抽牌前");
assert.equal(ui.simulationHorizon({horizon: "after_first_action_before_combat_rollouts"}), "首动作后继（后续战斗见统计）");
ui.renderSimulation({status: "partial", backend: "mentor", elapsed_ms: 40, simulation_advice: {
  strategies: {beam: {status: "advisory", first_action: {type: "end_turn"}, score: 10,
    endpoint: {horizon: "next_player_turn_before_draw", player: {hp: "20/80", energy: 3, block: 0}}}}}});
assert.equal(node("sim-badge").textContent, "部分可用");
assert.equal(node("sim-meta").textContent, "mentor · 0.04s");
assert.equal(node("sim-results").children[0].children[1].textContent, "结束回合");
assert.match(node("sim-results").children[0].children[2].textContent, /下一回合抽牌前.*HP 20 \/ 80/);
ui.renderSimulation({status: "unavailable", prediction: {deterministic_prefix: {
  checkpoints: [{stage: "before_next_player_draw", state: {horizon: "before_next_player_draw", player: {hp: "20/80", energy: 3}}}]}}});
assert.equal(node("sim-badge").textContent, "分阶段可用");
assert.match(node("sim-results").children[0].children[1].textContent, /下一回合抽牌前.*HP 20 \/ 80/);

// Root totals and per-first-action rollouts have separate labels and all candidates stay visible.
ui.renderSimulation({status: "partial", simulation_advice: {strategies: {mcts: {
  status: "advisory", first_action: {type: "end_turn"}, score: 1.2,
  mcts: {visits: 40, wins: 20, losses: 10, cutoffs: 10, mean: 1.2},
  search_mcts: {visits: 100, wins: 60, losses: 25, cutoffs: 15, simulations_completed: 100, simulations_requested: 250},
  alternatives: [{first_action: {type: "end_turn"}, score: 0.8, mcts: {visits: 60, wins: 40, losses: 15, cutoffs: 5, mean: 0.8}}]
}}}});
assert.match(node("sim-meta").textContent, /搜索总量 100.*250.*跨候选自适应分配/);
assert.equal(node("sim-results").children.length, 2);
assert.match(node("sim-results").children[0].children[3].textContent, /以该首动作为条件.*40.*20.*10/);

// PPO keeps grouped selection distinct from the largest individual probability and critic value.
ui.renderSimulation({status: "advisory", simulation_advice: {policy_status: "public_approximation",
  candidates: [{label: "A", action: {type: "choose_event_option", option_index: 2}, probability: 0.6, selected: false},
    {label: "B", action: {type: "choose_event_option", option_index: 7}, probability: 0.4, selected: true}],
  value: 0.4321, uncertainty: {completed: 32, world_count: 32, failed: 0}}});
assert.equal(node("sim-badge").textContent, "PPO 建议");
assert.match(node("sim-meta").textContent, /critic 估值 0.432（非胜率）.*32 \/ 32.*失败 0/);
assert.equal(node("sim-results").children[1].children[0].children[1].textContent, "已选");
assert.match(node("sim-results").children[1].children[2].textContent, /策略概率 40.0%（非胜率）/);
ui.renderSimulation({status: "unavailable", simulation_advice: {policy_status: "unsupported",
  candidates: [], value: null, errors: ["unknown_reward_pool"],
  uncertainty: {completed: 0, world_count: 32, failed: 32}}});
assert.doesNotMatch(node("sim-meta").textContent, /critic/);
assert.match(node("sim-meta").textContent, /0 \/ 32.*失败 32/);
assert.match(node("sim-results").children[0].children[1].textContent, /unknown_reward_pool/);

// The official entry keeps the full stream workflow and live inspectors.
const shell = fs.readFileSync(path.join(__dirname, "../src/sts2_agent/web/index.html"), "utf8");
assert.match(shell, /data-layout="stream"/);
assert.match(shell, /id="stream-list"/);
assert.match(shell, /id="simulation-settings"/);
assert.match(shell, /id="drawer-reasoning"/);
assert.match(shell, /id="drawer-guide"/);
assert.match(shell, /id="drawer-knowledge"/);
assert.doesNotMatch(shell, /静态演示|未连接任何后端|决策 d-118|12,480,392/);

/** Verify user-triggered launch transport and read-only initialization. */
async function verifyTransport() {
  // Fake API requests use actual client serialization and never reach a game or model.
  const calls = [];
  context.fetch = async (route, options) => {
    calls.push({route, options});
    return {ok: true, json: async () => route === "/api/game/start"
      ? {ok: true, receipt: {status: "accepted", message: "启动请求已接收。"}} : fresh};
  };
  context.EventSource = class {
    constructor() {}
    addEventListener() {}
    close() {}
  };
  ui.state.snapshot = {...fresh, status: "game_over", game_launch: {status: "idle"}};
  ui.renderControls();
  await ui.startGame();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].route, "/api/game/start");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {command_id: "f1111111-1111-4111-8111-111111111111"});
  assert.equal(ui.state.launchPending, true);
  assert.equal(node("run-toggle").disabled, true);
  assert.equal(node("start").disabled, true);
  assert.equal(node("toast").textContent, "启动请求已接收。");
  await ui.startGame();
  assert.equal(calls.length, 1);
  ui.acceptSnapshot({...fresh, seq: 3, game_launch: {status: "starting", message: "正在启动游戏。"}});
  assert.equal(ui.state.launchPending, false);
  assert.equal(node("run-toggle").disabled, true);
  ui.acceptSnapshot({...fresh, seq: 4});
  assert.equal(node("run-toggle").disabled, false);

  calls.length = 0;
  // Settings stay editable before launch; saving must precede native preparation.
  const launchSettings = {character: "IRONCLAD", ascension: 0, editable: true,
    choices: [{id: "IRONCLAD", label: "铁甲战士"}, {id: "SILENT", label: "静默猎手"}], max_ascension: 10};
  ui.state.launchPending = false;
  ui.state.snapshot = {...fresh, status: "paused", game_launch: {status: "ready"}, launch_settings: launchSettings};
  ui.renderControls();
  assert.equal(node("char-select").value, "IRONCLAD");
  assert.equal(node("asc-value").textContent, "A0");
  node("char-select").value = "SILENT";
  node("ascension").value = "3";
  ui.state.launchSettingsDirty = true;
  ui.renderControls();
  assert.equal(node("char-select").value, "SILENT");
  assert.equal(node("asc-value").textContent, "A3");
  context.fetch = async (route, options) => {
    calls.push({route, options});
    return {ok: true, json: async () => route === "/api/game/settings"
      ? {ok: true, launch_settings: {...launchSettings, character: "SILENT", ascension: 3}}
      : {ok: true, receipt: {status: "accepted"}}};
  };
  await ui.startGame();
  assert.deepEqual(calls.map(call => call.route), ["/api/game/settings", "/api/game/start"]);
  assert.deepEqual(JSON.parse(calls[0].options.body), {character: "SILENT", ascension: 3});
  ui.state.launchPending = false;
  ui.state.snapshot.launch_settings.editable = false;
  ui.renderControls();
  assert.equal(node("char-select").disabled, true);
  assert.equal(node("ascension").disabled, true);

  ui.state.snapshot.simulation_settings = {read_access: "internal", candidate_source: "model", search_strength: 3, editable: true};
  ui.renderControls();
  assert.equal(node("sim-read-access").value, "internal");
  assert.equal(node("simulation-combat-mode").value, "llm");
  assert.equal(node("simulation-run-mode").value, "llm");
  assert.match(node("simulation-strength-value").textContent, /1,?000.*4,?000/);
  assert.equal(node("simulation-settings-save").disabled, false);
  const requestsBeforePreset = calls.length;
  ui.selectNetworkPreset("upstream");
  assert.equal(node("simulation-combat-mode").value, "upstream");
  assert.equal(node("simulation-run-mode").value, "upstream");
  assert.equal(node("simulation-value").value, "texel");
  assert.equal(node("simulation-reward-pool").value, "full_unlock");
  assert.match(node("simulation-decision-note").textContent, /不调用大模型/);
  ui.renderControls();
  assert.equal(node("simulation-combat-mode").value, "upstream");
  assert.equal(calls.length, requestsBeforePreset);
  // The confirmed assist preset enables both advice stages and preserves the current pool.
  ui.selectNetworkPreset("assist");
  assert.equal(node("simulation-combat-mode").value, "assist");
  assert.equal(node("simulation-run-mode").value, "assist");
  assert.equal(node("simulation-reward-pool").value, "full_unlock");
  assert.equal(calls.length, requestsBeforePreset);

  // A settings save preserves the persisted continuous strength without display rounding.
  const strength = 2.99976417875251;
  node("simulation-strength").value = String(strength);
  context.fetch = async (route, options) => {
    calls.push({route, options});
    return {ok: true, json: async () => ({ok: true, simulation_settings: {...JSON.parse(options.body), editable: true}})};
  };
  await ui.saveSimulationSettings({preventDefault() {}});
  const saved = calls.at(-1);
  assert.equal(saved.route, "/api/simulation/settings");
  assert.equal(saved.options.method, "POST");
  assert.deepEqual(JSON.parse(saved.options.body), {read_access: "internal", combat_decision_mode: "assist",
    run_decision_mode: "assist", rollout_policy: "upstream", action_prior: "upstream", value_model: "texel",
    search_profile: "upstream", reward_pool: "full_unlock", candidate_source: "mcts", search_strength: strength});
  assert.equal(Number(node("simulation-strength").value), strength);
  ui.state.snapshot.simulation_settings.editable = false;
  ui.renderControls();
  assert.equal(node("simulation-settings-save").disabled, true);

  calls.length = 0;
  context.fetch = async (route, options) => {
    calls.push({route, options});
    return {ok: true, json: async () => fresh};
  };
  ui.initialize();
  await new Promise(resolve => setImmediate(resolve));
  await ui.refreshSnapshot();
  assert.equal(calls.length, 2);
  assert.equal(calls.every(call => call.route === "/api/snapshot" && !call.options.method), true);

  // One combined control submits the displayed command, without optimistic game state changes.
  calls.length = 0;
  let resolveCommand;
  context.fetch = (route, options) => {
    calls.push({route, options});
    return new Promise(resolve => { resolveCommand = resolve; });
  };
  ui.state.snapshot = {...fresh, status: "running", desired_state: "running"};
  ui.state.launchPending = false;
  ui.renderControls();
  const toggle = node("run-toggle").handlers.get("click");
  const pause = toggle();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].route, "/api/commands");
  assert.equal(JSON.parse(calls[0].options.body).command, "pause");
  assert.equal(ui.state.snapshot.status, "running");
  assert.equal(node("run-toggle").disabled, true);
  await toggle();
  assert.equal(calls.length, 1);
  resolveCommand({ok: true, json: async () => ({ok: true, receipt: {status: "accepted"}})});
  await pause;
  assert.equal(ui.state.snapshot.status, "running");

  ui.acceptSnapshot({...fresh, seq: 20});
  const resume = toggle();
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[1].options.body).command, "resume");
  assert.equal(ui.state.snapshot.status, "paused");
  resolveCommand({ok: true, json: async () => ({ok: true, receipt: {status: "accepted"}})});
  await resume;

  // Backend rejection and unknown transport outcome retain the user's unsent guidance.
  const guidance = "保留药水，优先考虑格挡。";
  node("instruction-text").value = guidance;
  node("instruction-scope").value = "combat";
  context.fetch = async (route, options) => {
    calls.push({route, options});
    return {ok: false, status: 409, json: async () => ({detail: "当前不能接收指导"})};
  };
  await ui.submitInstruction("next_decision");
  assert.equal(node("instruction-text").value, guidance);
  assert.equal(calls.at(-1).route, "/api/instructions");
  assert.equal(JSON.parse(calls.at(-1).options.body).scope, "combat");
  assert.equal(JSON.parse(calls.at(-1).options.body).timing, "next_decision");
  assert.match(node("toast").textContent, /当前不能接收指导/);
  context.fetch = async () => { throw new Error("connection lost"); };
  await ui.submitInstruction("redecide");
  assert.equal(node("instruction-text").value, guidance);
  assert.equal(ui.state.busy, false);
  context.fetch = async () => ({ok: true, json: async () => ({ok: true, receipt: {status: "accepted"}})});
  await ui.submitInstruction("next_decision");
  assert.equal(node("instruction-text").value, "");
  console.log("Dashboard usage and daily workflow behavior verified.");
}

verifyTransport().catch(error => { console.error(error); process.exitCode = 1; });

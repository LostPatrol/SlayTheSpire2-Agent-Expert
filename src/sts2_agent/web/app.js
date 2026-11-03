/** Local dashboard client: observe snapshots, follow SSE, and submit explicit controls. */
"use strict";

/** Translate display text while retaining the Chinese default in older clients. */
function tr(source, params = {}) {
  if (typeof source !== "string") return source;
  return window.ObservatoryI18n ? window.ObservatoryI18n.t(source, params)
    : source.replace(/\{(\w+)\}/g, (_, key) => String(params[key] ?? `{${key}}`));
}


// Snapshot refreshes coalesce streaming events without delaying control commands.
const SNAPSHOT_DELAY_MS = 280;
// Quiet paused sessions still accrue backend wall time without emitting SSE events.
const QUIET_SNAPSHOT_INTERVAL_MS = 1000;
// Reconnects back off while preserving the last visible snapshot and event cursor.
const RECONNECT_BASE_MS = 1500;
// Large public observations remain inspectable without flooding the main console.
const MAX_TIMELINE_ROWS = 100;
// The live throughput curve always shows the last ten seconds.
const SPEED_WINDOW_MS = 10000;
// Dense curve samples keep the live edge moving; rendering adds no API requests.
const SPEED_SAMPLE_MS = 120;
// Speed text decays during quiet network intervals without making another API request.
const SPEED_REFRESH_MS = 500;
// Recent same-model API counts calibrate text estimates without altering billing.
const SPEED_CALIBRATION_REQUESTS = 8;
// Keep short responses from dominating the learned token-to-character ratio.
const SPEED_CALIBRATION_MIN_TOKENS = 128;
// Local history supplements older controllers that retain only recent requests.
const MAX_DECISION_CARDS = 40;
// Keep the tool inspector useful without duplicating an entire run on every paint.
const MAX_TOOL_CARDS = 40;
// Match the independent controller settings; candidate_source remains a legacy alias.
const SIMULATION_SELECTS = {
  read_access: "sim-read-access", combat_decision_mode: "simulation-combat-mode",
  run_decision_mode: "simulation-run-mode", rollout_policy: "simulation-rollout",
  action_prior: "simulation-prior", value_model: "simulation-value", search_profile: "simulation-profile",
  reward_pool: "simulation-reward-pool",
};
// Labels describe native actions without exposing protocol details as the main UI.
const ACTION_LABELS = {
  play_card: "打出卡牌", end_turn: "结束回合", use_potion: "使用药水", discard_potion: "丢弃药水",
  choose_map_node: "选择路线", claim_reward: "领取奖励", choose_reward_card: "选择奖励卡牌",
  skip_reward_cards: "跳过选牌", skip_reward: "跳过奖励", proceed: "继续前进",
  choose_event_option: "选择事件选项", choose_rest_option: "选择营火行动", select_deck_card: "选择卡牌",
  buy_card: "购买卡牌", buy_relic: "购买遗物", buy_potion: "购买药水", remove_card: "移除卡牌",
  choose_treasure_relic: "选择遗物", open_chest: "打开宝箱", confirm_selection: "确认选择", skip_selection: "跳过选择",
  choose_capstone_option: "选择先古之民选项", select_character: "选择角色", embark: "开始冒险",
};
// A proposal remains distinct from an observed execution or an invalidated decision.
const DECISION_STATUS_LABELS = {
  proposed: "模型提案", ready: "方案就绪", validated: "已校验", pending: "待提交",
  submitted: "已提交", completed: "已执行", executed: "已执行", accepted: "游戏已接受",
  discarded: "已失效", invalidated: "已失效", rejected: "未通过校验", failed: "执行失败",
  historical_proposal: "历史提案",
};
// Labels describe program stages, never an inferred game outcome.
const PHASE_LABELS = {
  idle: "未开始", created: "会话就绪", starting: "正在创建会话", observing: "读取公开观测",
  observation: "读取公开观测", thinking: "模型正在思考", generating: "模型正在生成",
  model: "模型正在生成", model_request: "模型正在生成", deciding: "正在形成决策", upstream_generating: "Mentor 正在决策",
  calculating: "有限计算中", evaluating: "有限计算中", model_generating: "模型正在生成",
  decision_ready: "方案就绪", executing: "正在提交动作", submitting: "正在提交动作",
  settling: "等待游戏结算", waiting: "等待稳定状态", waiting_settlement: "等待游戏结算",
  paused: "已暂停", pausing: "正在等待安全点", pause_pending: "正在等待安全点",
  budget_stop: "预算已用尽", game_over: "游戏已结束", running: "运行中", stopped: "已停止",
  completed: "本次运行已完成", error: "控制器发生错误", failed: "控制器发生错误",
  reconciling: "正在核对动作结果", stopping: "正在停止", ready: "方案就绪",
};
// Instruction and request states come directly from backend lifecycle fields.
const INSTRUCTION_LABELS = {
  received: "已收到", queued: "排队中", injected: "已注入", consumed: "已消费",
  expired: "已过期", revoked: "已撤销", replaced: "已替代", rejected: "已拒绝",
};
const SCOPE_LABELS = { next_decision: "下一次完整决策", combat: "本场战斗", run: "本局" };
const REQUEST_LABELS = {
  pending: "请求中", streaming: "生成中", generating: "生成中", running: "生成中",
  completed: "已完成", complete: "已完成", success: "已完成", failed: "失败",
  error: "失败", cancelling: "正在停止输出", cancelled: "已取消", invalidated: "已失效", stale: "已失效",
};
const SCREEN_LABELS = {
  COMBAT: "战斗", MAP: "地图", REWARD: "奖励", REWARDS: "奖励", SHOP: "商店",
  REST: "营火", EVENT: "事件", MAIN_MENU: "主菜单", CHARACTER_SELECT: "角色选择",
  CARD_REWARD: "卡牌奖励", CARD_SELECT: "卡牌选择", GAME_OVER: "终局", UNKNOWN: "加载中",
};
// Exact event names take precedence over broad stage matching below.
const EVENT_PRESENTATIONS = {
  action_completed: ["execution", "动作已完成"],
  action_rejected: ["execution", "动作被拒绝"],
  action_submitted: ["execution", "程序提交动作"],
  decision: ["model", "模型决策已形成"],
  decision_discarded: ["model", "旧决策已失效"],
  decision_rejected: ["model", "决策校验未通过"],
  instruction: ["observe", "人工指令已更新"],
  instruction_queued: ["observe", "人工指令已排队"],
  instruction_revoked: ["observe", "人工指令已撤销"],
  instructions_injected: ["observe", "人工指令已注入"],
  budget_stop: ["observe", "预算已用尽，运行停止"],
  game_over: ["execution", "游戏已结束"],
  tool_call_started: ["tool", "调用工具"],
  tool_call_completed: ["tool", "工具已返回"],
  tool_call_failed: ["tool", "工具调用失败"],
};
// The client never starts or resumes the controller during initialization or reconnect.
const state = {
  snapshot: null, lastSeq: 0, lastSnapshotSeq: -1, snapshotBusy: false,
  snapshotAgain: false, refreshTimer: null, retryTimer: null, source: null,
  reconnectAttempt: 0, apiAvailable: false, streamConnected: false, busy: false,
  selectedRequest: "", requestPinned: false, stream: "reasoning", follow: true,
  toastTimer: null, lastSnapshotAt: null, decisionCards: new Map(),
  speedHistory: [], speedPointAt: 0, historySignature: "",
  streamDragging: false, runId: "", launchPending: false, launchError: "",
  toolSignature: "", simulationSignature: "", launchSettingsDirty: false, launchChoicesSignature: "", simulationSettingsDirty: false,
};

/** Find a fixed shell element. */
function byId(id) { return document.getElementById(id); }

/** Render untrusted values only as text. */
function setText(id, value) { byId(id).textContent = printable(value); }

/** Preserve useful public objects while avoiding implicit object strings. */
function printable(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

/** Read legacy native tooltip images as text without exposing their texture paths. */
function publicRulesText(value, fallback = "—") {
  return printable(value, fallback)
    .replace(/(?:res|uid):\/\/[^\s\[\]<>"。！？；，]+?\.(?:png|webp|svg|jpg)(?:\?[^\s\[\]<>"。！？；，]*)?/gi, resource => {
      const name = resource.split("/").at(-1).split("?")[0];
      return /_energy_icon\.png$/i.test(name) ? "⚡" : tr("〔图标:{p0}〕", {p0: name.replace(/\.[^.]+$/, "")});
    })
    .replace(/\[\/?(?:[a-z_]+)(?:[= ][^\]]+)?\]/gi, "");
}

/** Accept only real finite numeric measurements. */
function numeric(value) { return typeof value === "number" && Number.isFinite(value); }

/** Format a measured count; missing usage stays visibly unknown. */
function count(value, fallback = tr("未知")) { return numeric(value) ? value.toLocaleString(window.ObservatoryI18n?.locale || "zh-CN") : fallback; }

/** Format controller wall time without inventing a running client clock. */
function duration(value) {
  if (!numeric(value)) return "—";
  const seconds = Math.max(0, Math.floor(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds % 60}s`;
}

/** Convert a backend timestamp to local time with its machine-readable original. */
function localTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString(window.ObservatoryI18n?.locale || "zh-CN", { hour12: false });
}

/** Construct safe text nodes for dynamic cards and timeline rows. */
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = printable(text);
  return node;
}

/** Fill a compact collection with a visible empty state. */
function replaceCollection(id, nodes, message) {
  byId(id).replaceChildren(...(nodes.length ? nodes : [element("p", "empty-note", message)]));
}

/** Clamp visual meters while leaving the measured count intact. */
function setMeter(id, used, maximum) {
  const percent = numeric(used) && numeric(maximum) && maximum > 0 ? Math.min(100, Math.max(0, used / maximum * 100)) : 0;
  byId(id).style.width = `${percent}%`;
}

/** Normalize common public health representations. */
function healthParts(value, maximum) {
  if (value && typeof value === "object") return [value.current ?? value.hp ?? value.value, value.max ?? value.maximum ?? value.max_hp ?? maximum];
  if (typeof value === "string" && /^\d+\s*\/\s*\d+$/.test(value)) return value.split("/").map(Number);
  return [value, maximum];
}

/** Display known health and its public maximum when available. */
function healthText(value, maximum) {
  const [current, max] = healthParts(value, maximum);
  return max !== undefined && max !== null ? `${printable(current)} / ${printable(max)}` : printable(current);
}

/** Keep transport failures explicit while retaining the authoritative last snapshot. */
function renderConnection(message) {
  const online = state.apiAvailable && state.streamConnected;
  byId("connection").className = `conn ${online ? "online" : "offline"}`;
  setText("connection-text", online ? tr("本地控制器已连接") : state.snapshot ? tr("连接恢复中") : tr("等待连接"));
  const banner = byId("connection-banner");
  banner.hidden = online;
  if (!online) banner.textContent = message || (state.snapshot ? tr("事件连接中断，保留最后快照；正在自动重连。") : tr("正在连接本地控制器，页面不会自动启动 Agent。"));
  renderControls();
}

/** Recognize terminal game evidence even if a stale pause state remains present. */
function isGameOver(snapshot) {
  return String(snapshot?.status).toLowerCase() === "game_over"
    || String(snapshot?.phase).toLowerCase() === "game_over"
    || Boolean(snapshot?.terminal_evidence?.game_over);
}

/** Keep launch progress and backend recovery instructions visible independently. */
function renderGameLaunch(snapshot) {
  const launch = snapshot?.game_launch || {};
  const status = String(launch.status || "idle").toLowerCase();
  const awaiting = state.launchPending && status !== "starting";
  const fallback = status === "error" ? tr("游戏启动失败，请检查程序执行时间线后重试。")
    : status === "starting" ? tr("正在准备游戏；自动游玩保持暂停。")
    : status === "ready" ? tr("游戏已就绪，自动游玩保持暂停。") : "";
  const message = awaiting ? tr("启动请求已收到，正在准备游戏；自动游玩保持暂停。")
    : isGameOver(snapshot) && status === "ready" ? tr("本局已结束，点击启动游戏准备下一局。")
    : tr(printable(launch.message, fallback));
  const banner = byId("game-launch-banner");
  banner.hidden = !state.launchPending && status !== "starting" && status !== "error";
  banner.textContent = message;
  banner.setAttribute("role", status === "error" && !awaiting ? "alert" : "status");
  if (!awaiting) {
    if (status === "error" && message !== state.launchError) toast(message, true);
    state.launchError = status === "error" ? message : "";
  }
}

/** Read cancellation from the active worker signal and the latest request lifecycle. */
function generationCancellation(snapshot = state.snapshot) {
  const status = snapshot?.model_requests?.at(-1)?.status;
  return snapshot?.generation_cancelling || status === "cancelling" ? "cancelling" : status === "cancelled" ? "cancelled" : "";
}

/** Show measured phases and distinguish terminal results from pause transitions. */
function renderSession(snapshot) {
  const status = String(snapshot.status || "idle").toLowerCase();
  const phase = String(snapshot.phase || status).toLowerCase();
  const wantsPause = ["paused", "pause"].includes(snapshot.desired_state);
  const hasInflight = Boolean(snapshot.inflight);
  const modelPending = Boolean(snapshot.request_pending);
  const cancellation = generationCancellation(snapshot);
  const safePaused = status === "paused" && !hasInflight;
  const gameOver = isGameOver(snapshot);
  // Terminal status outranks the requested pause, which may remain set after exit.
  const terminalStates = ["error", "failed", "budget_stop", "stopped", "completed"];
  const terminal = terminalStates.includes(status) ? status : terminalStates.includes(phase) ? phase : null;
  const evidenceVictory = snapshot.terminal_evidence?.game_over?.victory;
  const victory = typeof snapshot.victory === "boolean" ? snapshot.victory : evidenceVictory;
  const label = gameOver ? victory === true ? tr("已通关") : victory === false ? tr("本局失败") : tr("游戏已结束")
    : terminal ? tr(PHASE_LABELS[terminal])
    : wantsPause && !safePaused ? tr("正在等待安全点") : safePaused ? tr("已暂停") : tr(PHASE_LABELS[phase]) || tr(PHASE_LABELS[status]) || printable(snapshot.phase || snapshot.status);
  setText("phase-label", label);
  let detail = tr("开始自动游玩后，Agent 会持续行动；单步只放行一个原生合法动作。");
  if (gameOver) detail = tr("{p0}点击启动游戏准备新会话，旧局记录会保留。", {p0: typeof victory === "boolean" ? tr("结果已保存。") : tr("结果已保存，胜负尚未确认。")});
  else if (["error", "failed"].includes(terminal)) detail = tr(printable(snapshot.error || snapshot.last_error, tr("请查看程序执行时间线中的错误信息。")));
  else if (cancellation === "cancelling") detail = hasInflight ? tr("正在停止模型输出；已提交动作仍需等待结算。") : tr("正在停止模型输出，等待请求退出。");
  else if (terminal) detail = tr("本次控制循环已结束，累计用量与事件仍可查看。");
  else if (!snapshot.connection?.connected) detail = tr("点击启动游戏，等待游戏桥连接就绪后点击开始自动游玩。");
  else if (wantsPause && !safePaused) detail = hasInflight ? tr("已禁止新动作提交，等待已提交动作结算。") : tr("暂停请求已收到，等待控制器进入安全点。");
  else if (safePaused && modelPending) detail = tr("游戏动作已暂停，等待模型请求退出。");
  else if (safePaused) detail = snapshot.budget_started_utc ? tr("动作提交已暂停，已有记忆、方案与预算保持保留。") : tr("游戏已连接，点击开始自动游玩；当前不会调用模型或提交动作。");
  else if (hasInflight) detail = tr("动作已提交，等待游戏回执与自然结算。");
  else if (modelPending) detail = tr("正在接收模型响应，完整校验后才能提交游戏动作。");
  if (state.launchPending) detail = tr("启动请求已收到，正在准备游戏；自动游玩保持暂停。");
  else if (snapshot.game_launch?.status === "starting") detail = tr(printable(snapshot.game_launch?.message, tr("正在准备游戏；自动游玩保持暂停。")));
  const showDetail = ["error", "failed", "budget_stop"].includes(terminal) || hasInflight || modelPending || cancellation === "cancelling";
  setText("phase-detail", showDetail ? detail : "");
  byId("phase-detail").hidden = !showDetail;
  let orbClass = "running";
  if (["error", "failed"].includes(terminal) || (gameOver && victory === false)) orbClass = "error";
  else if (gameOver || terminal || status === "idle") orbClass = "";
  else if (safePaused || wantsPause) orbClass = "paused";
  byId("phase-orb").className = `orb ${orbClass}`;
  const connection = snapshot.connection || {};
  setText("run-identity", snapshot.run_id ? tr("本局 {p0}", {p0: snapshot.run_id}) : tr("等待会话"));
  setText("version", tr("游戏 {p0} · Mod {p1} · PID {p2}", {p0: printable(connection.game_version), p1: printable(connection.mod_version), p2: printable(connection.pid)}));
  byId("connection").title = connection.connected ? tr("控制器已连接游戏桥") : tr(printable(connection.error, tr("游戏桥尚未连接，等待公开观测")));
  if (state.apiAvailable && state.streamConnected && connection.connected === false) {
    byId("connection-banner").hidden = false;
    setText("connection-banner", tr("控制器已连接，游戏桥尚未就绪。{p0}", {p0: printable(connection.error, tr("等待游戏连接。"))}));
  }
  renderGameLaunch(snapshot);
}

/** Render only the reviewed public observation, including optional noncombat detail. */
function renderGame(observation) {
  const observationState = observation?.state || {};
  const combat = observationState.combat || {};
  const run = observationState.run || observation?.run || {};
  const player = combat.player || {};
  const hp = player.hp ?? run.hp;
  const maxHp = player.max_hp ?? player.maxHp ?? run.max_hp ?? run.maxHp;
  const [currentHp, maximumHp] = healthParts(hp, maxHp);
  const screen = observationState.screen || observation?.screen;
  setText("screen-label", screen ? tr(SCREEN_LABELS[String(screen).toUpperCase()]) || screen : tr("等待观测"));
  setText("hp-value", currentHp);
  setText("hp-max", maximumHp);
  setMeter("hp-bar", currentHp, maximumHp);
  setText("floor-value", run.floor ?? observationState.floor);
  // act_id is the bridge's zero-based native act index, not a display name.
  const actIndex = run.act_id === null || run.act_id === undefined ? NaN : Number(run.act_id);
  setText("act-value", Number.isInteger(actIndex) ? actIndex + 1 : run.act_id);
  setText("hero-name", run.character);
  setText("hero-asc", numeric(run.ascension) ? `A${run.ascension}` : "—");
  window.Observatory?.applyCharacter(run.character || state.snapshot?.launch_settings?.character);
  const meter = byId("hp-bar").parentElement;
  if (meter) {
    meter.setAttribute("aria-valuenow", numeric(Number(currentHp)) ? currentHp : 0);
    meter.setAttribute("aria-valuemax", numeric(Number(maximumHp)) ? maximumHp : 100);
    meter.setAttribute("aria-valuetext", healthText(hp, maxHp));
  }
  setText("obs-screen", screen ? tr(SCREEN_LABELS[String(screen).toUpperCase()]) || screen : "—");
  setText("obs-energy", player.energy);
  setText("obs-block", player.block);
  setText("obs-gold", run.gold);
  setText("obs-enemies", Array.isArray(combat.enemies) ? combat.enemies.length : "—");
  setText("obs-hand", Array.isArray(combat.hand) ? combat.hand.length : "—");
  renderAssets(run);
  setText("obs-json", observation ? JSON.stringify(observation, null, 2) : tr("尚无公开观测"));
}

/** Show reported run assets without counting aggregated deck rows as individual cards. */
function renderAssets(run) {
  const relics = Array.isArray(run.relics) ? run.relics : [];
  const potions = Array.isArray(run.potions) ? run.potions.filter(item => item.occupied || item.potion_id) : [];
  const deck = Array.isArray(run.deck) ? run.deck : [];
  setText("assets-count", tr("{p0} 遗物 · {p1} 药水", {p0: Array.isArray(run.relics) ? relics.length : "—", p1: Array.isArray(run.potions) ? potions.length : "—"}));
  replaceCollection("asset-relics", relics.map((item, index) => {
    const chip = element("b", "", typeof item === "string" ? item : item.name || item.relic_id);
    chip.title = publicRulesText(run.relic_descriptions?.[index] || item.description, "");
    return chip;
  }), Array.isArray(run.relics) ? tr("无遗物") : tr("尚无遗物记录"));
  replaceCollection("asset-potions", potions.map(item => {
    const chip = element("b", "", publicRulesText(item.name || item.line || item.potion_id));
    chip.title = publicRulesText(item.description, "");
    return chip;
  }), Array.isArray(run.potions) ? tr("无药水") : tr("尚无药水记录"));
  const total = numeric(run.deck_size) ? run.deck_size : deck.length && deck.every(item => numeric(item.card_count)) ? deck.reduce((sum, item) => sum + item.card_count, 0) : null;
  setText("asset-deck-count", numeric(total) ? tr("{p0} 张", {p0: total}) : deck.length ? tr("{p0} 条牌组记录 · 总张数未提供", {p0: deck.length}) : tr("尚无牌组记录"));
  replaceCollection("asset-deck", deck.map(item => element("b", "", publicRulesText(typeof item === "string" ? item : item.name || item.line || item.card_id))), tr("尚无牌组记录"));
}

/** Render cumulative budgets and measured token usage without zero-filling gaps. */
function renderMetrics(metrics) {
  setText("action-budget", count(metrics.actions_completed, "—"));
  setText("time-budget", duration(metrics.elapsed_seconds));
  setText("total-tokens", count(metrics.total_tokens));
  setText("completion-tokens", count(metrics.completion_tokens));
  setText("cache-hit-tokens", count(metrics.prompt_cache_hit_tokens));
  setText("cache-miss-tokens", count(metrics.prompt_cache_miss_tokens));
  setText("budget-actions", `${count(metrics.actions_completed)} / ${count(metrics.max_actions)}`);
  setText("budget-time", `${duration(metrics.elapsed_seconds)} / ${duration(metrics.max_wall_seconds)}`);
  setText("budget-requests", `${count(metrics.requests_used)} / ${count(metrics.max_requests)}`);
  setMeter("budget-action-meter", metrics.actions_completed, metrics.max_actions);
  setMeter("budget-time-meter", metrics.elapsed_seconds, metrics.max_wall_seconds);
  setMeter("budget-request-meter", metrics.requests_used, metrics.max_requests);
  // Composition uses measured total tokens and never substitutes live estimates.
  for (const [id, value] of [["mix-hit", metrics.prompt_cache_hit_tokens], ["mix-miss", metrics.prompt_cache_miss_tokens], ["mix-out", metrics.completion_tokens]]) {
    setMeter(id, value, metrics.total_tokens);
  }
  byId("total-tokens").title = numeric(metrics.unknown_usage_requests) && metrics.unknown_usage_requests > 0 ? tr("未知用量请求：{p0}", {p0: metrics.unknown_usage_requests}) : "";
}

/** Keep historical requests separate and follow the newest unless explicitly pinned. */
function renderRequests(requests) {
  const list = Array.isArray(requests) ? requests : [];
  if (!state.requestPinned || !list.some(request => request.model_request_id === state.selectedRequest)) {
    // Keep the preceding text visible during the next request's first-token wait.
    const visible = list.findLast(request => request[state.stream]) || list.at(-1);
    state.selectedRequest = visible?.model_request_id || "";
    state.requestPinned = false;
  }
  const select = byId("request-select");
  select.replaceChildren();
  if (!list.length) select.append(element("option", "", tr("等待模型请求")));
  list.slice().reverse().forEach((request, index) => {
    const option = element("option", "", tr("{p0}请求 {p1} · {p2}", {p0: index === 0 ? tr("最新 · ") : "", p1: list.length - index, p2: tr(REQUEST_LABELS[request.status]) || printable(request.status)}));
    option.value = request.model_request_id;
    option.title = request.model_request_id;
    select.append(option);
  });
  select.disabled = !list.length;
  select.value = state.selectedRequest;
  renderStream();
}

/** Replace streamed text safely and preserve the reader's scroll position. */
function renderStream() {
  const requests = state.snapshot?.model_requests || [];
  const request = requests.find(item => item.model_request_id === state.selectedRequest);
  const cancellation = generationCancellation();
  const text = request ? printable(request[state.stream], "") : "";
  const box = byId("stream-box");
  const previousScroll = box.scrollTop;
  const output = byId("stream-text");
  if (output.textContent !== text) output.textContent = text;
  output.hidden = !text;
  byId("stream-empty").hidden = Boolean(text);
  const empty = byId("stream-empty");
  const latest = requests.at(-1);
  const selectedCancellation = request === latest && cancellation ? cancellation : ["cancelling", "cancelled"].includes(request?.status) ? request.status : "";
  empty.querySelector("strong").textContent = selectedCancellation ? tr(REQUEST_LABELS[selectedCancellation]) : request ? state.stream === "reasoning" ? tr("暂无推理文本") : tr("等待最终响应") : tr("等待下一次思考");
  empty.querySelector("p").textContent = selectedCancellation ? tr("该请求的已有输出保留在记录中，不会作为完整决策提交。") : request ? state.stream === "reasoning" ? tr("该请求尚未返回推理文本，或服务端未提供此字段。") : tr("完整响应到达后显示；流中的半成品不会提交到游戏。") : tr("这里展示 API 返回的推理文本。启动游戏后仍保持暂停。");
  const retaining = !state.requestPinned && latest && latest !== request && !latest[state.stream];
  setText("request-status", retaining ? tr("{p0} · 保留上一轮", {p0: cancellation ? tr(REQUEST_LABELS[cancellation]) : tr("等待新输出")}) : selectedCancellation ? tr(REQUEST_LABELS[selectedCancellation]) : request ? tr(REQUEST_LABELS[request.status]) || request.status : tr("未开始"));
  setText("model-activity", cancellation ? tr("{p0} · API 原始输出", {p0: tr(REQUEST_LABELS[cancellation])}) : state.snapshot?.request_pending ? tr("正在生成 · API 原始输出") : text ? tr("已有输出 · API 原始文本") : tr("查看推理与 JSON"));
  const metadata = [];
  if (request?.model) metadata.push(String(request.model));
  if (request?.decision_id) metadata.push(tr("决策 {p0}", {p0: request.decision_id}));
  if (request?.decision_epoch !== undefined) metadata.push(`epoch ${request.decision_epoch}`);
  if (request?.error) metadata.push(printable(request.error));
  setText("request-meta", metadata.length ? metadata.join(" · ") : tr("API 推理文本不代表全部内部计算"));
  setText("stream-length", request ? tr("{p0} 字符", {p0: text.length.toLocaleString(window.ObservatoryI18n?.locale || "zh-CN")}) : "—");
  if (state.follow) box.scrollTop = box.scrollHeight;
  else box.scrollTop = previousScroll;
}

/** Present the actual decision source separately from native receipts and execution. */
function renderDecision(decision) {
  const previous = [...state.decisionCards.values()].at(-1);
  const visible = decision?.action ? decision : previous;
  const thinking = Boolean(state.snapshot?.request_pending || state.snapshot?.decision_pending);
  const cancellation = generationCancellation();
  const localPending = state.snapshot?.phase === "upstream_generating";
  const upstream = String(visible?.decision_source || "").startsWith("upstream_");
  const request = (state.snapshot?.model_requests || []).findLast(item => item.decision_id === visible?.decision_id);
  setText("decision-engine", localPending ? "Mentor" : upstream
    ? visible.decision_source === "upstream_ppo" ? "Mentor PPO" : "Mentor MCTS"
    : visible?.decision_source === "automatic" ? tr("自动流程") : request?.actual_model && request.actual_model !== "unknown" ? request.actual_model : request?.requested_model || (visible ? tr("大模型") : "—"));
  setText("decision-status", cancellation === "cancelling" ? tr("正在停止输出") : thinking ? localPending ? tr("Mentor 正在决策") : tr("正在思考下一步")
    : cancellation === "cancelled" ? tr("本轮生成已取消")
    : visible?.decision_source === "automatic" ? tr("自动流程")
    : upstream && visible.status === "proposed" ? tr("Mentor 提案")
    : visible ? tr(DECISION_STATUS_LABELS[visible.status]) || tr("最近完整决策") : tr("等待决策"));
  byId("decision-status").classList.toggle("generating", thinking && cancellation !== "cancelling");
  setText("decision-identity", visible?.decision_id ? tr("决策 {p0}", {p0: visible.decision_id.slice(0, 8)}) : "—");
  byId("decision-identity").title = visible?.decision_id || "";
  setText("decision-time", thinking && visible ? tr("保留上一条完整说明") : visible?.timestamp_utc ? localTime(visible.timestamp_utc) : visible ? tr("完整方案") : tr("尚无有效方案"));
  const label = visible?.action_label !== visible?.action?.type ? visible?.action_label : null;
  // Completed actions belong to an earlier root; live indices may already refer to other cards.
  const currentRoot = decision?.action && ["proposed", "ready", "pending", "validated"].includes(visible?.status);
  setText("decision-action", publicRulesText(label || actionLabel(visible?.action, currentRoot ? state.snapshot?.observation : null)));
  const target = visible?.action?.target_index;
  const targetEnemy = currentRoot ? state.snapshot?.observation?.state?.combat?.enemies?.find(enemy => enemy.i === target) : null;
  setText("decision-target", target === undefined ? "" : tr("目标：{p0}", {p0: targetEnemy?.name || tr("公开目标 #{p0}", {p0: target})}));
  byId("decision-target").hidden = target === undefined;
  setText("decision-json", visible?.action ? JSON.stringify(visible.action, null, 2) : tr("尚无完整动作"));
  setText("decision-reason", visible?.reason || tr("完整决策到达后，说明会保留在这里。"));
  setText("decision-plan", state.snapshot?.build_plan ?? state.snapshot?.plan ?? visible?.build_plan ?? visible?.plan ?? "—");
  setText("scene-plan", state.snapshot?.scene_plan ?? visible?.scene_plan ?? "—");
}

/** Describe an action using only its associated current public observation. */
function actionLabel(action, observation) {
  if (!action) return tr("等待模型给出完整动作");
  const label = tr(ACTION_LABELS[action.type]) || action.type;
  if (action.type === "play_card") {
    const card = observation?.state?.combat?.hand?.find(item => item.i === action.card_index);
    const name = card?.name || card?.line?.split(/[\[：:]/)[0].trim();
    return `${label} · ${name || tr("手牌 #{p0}", {p0: action.card_index})}`;
  }
  return action.option_index === undefined ? label : tr("{p0} · 选项 #{p1}", {p0: label, p1: action.option_index});
}

/** Retain complete explanations without treating raw responses as game receipts. */
function collectDecisions(snapshot) {
  const items = [];
  const history = snapshot.decision_history || [];
  // Persisted history owns ordering and execution status once the controller supplies it.
  if (history.length) state.decisionCards.clear();
  for (const request of history.length ? [] : snapshot.model_requests || []) {
    if (request.status !== "completed" || !request.content) continue;
    try {
      const proposal = JSON.parse(request.content);
      if (proposal.action) items.push({ ...proposal, decision_id: request.decision_id || request.model_request_id,
        timestamp_utc: request.completed_utc, status: "proposed" });
    } catch { /* Formatting repairs may leave an earlier complete response invalid. */ }
  }
  items.push(...history);
  if (snapshot.decision?.action) items.push(snapshot.decision);
  for (const item of items) {
    const key = item.decision_id || JSON.stringify([item.action, item.reason, item.plan]);
    state.decisionCards.set(key, { ...state.decisionCards.get(key), ...item });
  }
  while (state.decisionCards.size > MAX_DECISION_CARDS) state.decisionCards.delete(state.decisionCards.keys().next().value);
}

/** Approximate uncalibrated text units across reasoning and final output. */
function textTokenUnits(request) {
  const text = `${request?.reasoning || ""}${request?.content || ""}`;
  const wide = text.match(/[\u2e80-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/g)?.length || 0;
  return wide + (text.length - wide) / 4;
}

/** Learn the local model's text ratio from complete, API-counted responses. */
function tokenCalibration(request, requests) {
  const completed = (requests || []).filter(item => item.model_request_id !== request?.model_request_id
    && item.status === "completed" && item.requested_model === request?.requested_model
    && (!request?.actual_model || request.actual_model === "unknown" || item.actual_model === request.actual_model)
    && numeric(item.usage?.completion_tokens) && item.usage.completion_tokens >= SPEED_CALIBRATION_MIN_TOKENS
    && textTokenUnits(item) > 0).slice(-SPEED_CALIBRATION_REQUESTS);
  const units = completed.reduce((sum, item) => sum + textTokenUnits(item), 0);
  return units ? completed.reduce((sum, item) => sum + item.usage.completion_tokens, 0) / units : 1;
}

/** Estimate live tokens using recent actual usage, keeping billing totals untouched. */
function estimateTokens(request, calibration = 1) {
  return Math.ceil(textTokenUnits(request) * calibration);
}

/** Measure first-to-last text arrival, excluding prefill and final usage delivery. */
function generationMilliseconds(request) {
  if (request?.response_streamed === false || request?.stream === false) return null;
  if (numeric(request?.generation_ms) && request.generation_ms > 0) return request.generation_ms;
  const elapsed = Date.parse(request?.last_delta_utc) - Date.parse(request?.first_delta_utc);
  return Number.isFinite(elapsed) && elapsed > 0 ? elapsed : null;
}

/** Decode-only rate, matching the DSH tok/s metric: tokens over the decode span. */
function decodeRate(tokens, startMs, endMs) {
  const span = endMs - startMs;
  return numeric(tokens) && Number.isFinite(span) && span > 0 ? tokens / (span / 1000) : null;
}

/** DSH display parity: whole numbers from ten up, one decimal below. */
function formatRate(value) {
  const clamped = Math.max(0, value);
  return clamped >= 10 ? String(Math.round(clamped)) : String(Math.round(clamped * 10) / 10);
}

/** Measure decode-only speed for the newest request; prefill and first-token wait stay excluded. */
function measureSpeed(request, now) {
  const cancellation = generationCancellation();
  const active = !cancellation && request && ["generating", "streaming", "running", "pending"].includes(request.status);
  const calibration = tokenCalibration(request, state.snapshot?.model_requests);
  const estimated = estimateTokens(request, calibration);
  const billedOutput = request?.usage?.completion_tokens;
  const generationMs = generationMilliseconds(request);
  const measured = !active && !cancellation && numeric(billedOutput) && numeric(generationMs);
  const totalMeasured = !active && !cancellation && !measured && numeric(billedOutput) && numeric(request?.duration_ms) && request.duration_ms > 0;
  const firstDelta = Date.parse(request?.first_delta_utc);
  let speed = null;
  if (active && estimated && Number.isFinite(firstDelta)) speed = decodeRate(estimated, firstDelta, now);
  else if (measured) speed = billedOutput / (generationMs / 1000);
  else if (totalMeasured) speed = billedOutput / (request.duration_ms / 1000);
  if (cancellation) speed = null;
  return {cancellation, active, estimated, measured, totalMeasured, speed};
}

/** Record one bounded-rate curve sample; idle samples fall to zero. */
function sampleSpeed(now, speed, active) {
  if (now - state.speedPointAt < SPEED_SAMPLE_MS) return;
  state.speedPointAt = now;
  state.speedHistory.push({time: now, value: active && numeric(speed) ? Math.max(0, speed) : 0});
  // Keep one sample beyond the window so the curve's left edge can be interpolated.
  state.speedHistory = state.speedHistory.filter(point => point.time >= now - SPEED_WINDOW_MS - SPEED_SAMPLE_MS * 2);
}

/** Draw the curve, optionally with the continuously updated live value at its right edge. */
function drawSpeed(now, speed, active) {
  window.Observatory?.renderSpeed(state.speedHistory, now, SPEED_WINDOW_MS,
    active && numeric(speed) ? Math.max(0, speed) : null);
}

// Identifies the animation loop so visibility changes and request ends cannot stack frames.
let speedFrameId = 0;

/** Advance the live curve at display cadence without touching the API or the text panel. */
function speedFrame() {
  speedFrameId = 0;
  const now = Date.now();
  const request = state.snapshot?.model_requests?.at(-1);
  const view = measureSpeed(request, now);
  sampleSpeed(now, view.speed, view.active);
  drawSpeed(now, view.speed, view.active);
  if (view.active && !document.hidden) speedFrameId = window.requestAnimationFrame(speedFrame);
}

/** Start the curve loop only while a request is streaming. */
function startSpeedLoop() {
  if (speedFrameId || typeof window.requestAnimationFrame !== "function" || document.hidden) return;
  speedFrameId = window.requestAnimationFrame(speedFrame);
}

/** Stop the curve loop when no request is active. */
function stopSpeedLoop() {
  if (!speedFrameId) return;
  if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(speedFrameId);
  speedFrameId = 0;
}

/** Show calibrated rolling generation and distinguish API generation from request latency. */
function renderLiveUsage() {
  const request = state.snapshot?.model_requests?.at(-1);
  const now = Date.now();
  const view = measureSpeed(request, now);
  const speed = view.speed;
  sampleSpeed(now, speed, view.active);
  setText("token-speed", numeric(speed) ? formatRate(speed) : "—");
  setText("speed-state", view.cancellation ? tr(REQUEST_LABELS[view.cancellation]) : view.active ? view.estimated ? tr("生成中") : tr("等待首字") : request ? tr("已结束") : tr("待机"));
  byId("speed-state").classList.toggle("active", Boolean(view.active));
  byId("token-speed").title = view.cancellation ? tr("取消的请求不显示生成速率") : view.measured ? tr("按解码时长计算（首字→完成）") : view.totalMeasured ? tr("含首字等待") : view.active ? tr("按解码时长计算（首字→当前）") : tr("尚无生成记录");
  byId("speed-state").dataset.state = view.cancellation || (view.active ? "generating" : "paused");
  if (view.active) startSpeedLoop(); else stopSpeedLoop();
  drawSpeed(now, speed, view.active);
}

/** Render instruction lifecycle records and revoke only active instructions. */
function renderInstructions(instructions) {
  const list = Array.isArray(instructions) ? instructions : [];
  setText("guide-count", list.filter(item => !["consumed", "expired", "revoked", "replaced", "rejected"].includes(item.status)).length);
  setText("instruction-count", tr("{p0} 条", {p0: list.length}));
  replaceCollection("instruction-list", list.slice().reverse().map(instruction => {
    const node = element("div", "instruction-card");
    const meta = element("div", "instruction-meta");
    const inactive = ["consumed", "expired", "revoked", "replaced", "rejected"].includes(instruction.status);
    const status = element("span", `instruction-state${instruction.status === "injected" ? " injected" : inactive ? " inactive" : ""}`, tr(INSTRUCTION_LABELS[instruction.status]) || instruction.status);
    meta.append(status, element("span", "", tr(SCOPE_LABELS[instruction.scope]) || instruction.scope));
    const bottom = element("div", "instruction-bottom");
    const association = instruction.decision_id ? tr("决策 {p0}", {p0: instruction.decision_id}) : tr("指令 {p0}", {p0: printable(instruction.id)});
    const label = element("span", "", association);
    label.title = association;
    bottom.append(label);
    if (!inactive && instruction.id !== undefined) {
      const revoke = element("button", "revoke-button", tr("撤销"));
      revoke.type = "button";
      revoke.disabled = state.busy || !state.apiAvailable;
      revoke.addEventListener("click", () => revokeInstruction(instruction.id));
      bottom.append(revoke);
    }
    node.append(meta, element("p", "", instruction.text), bottom);
    return node;
  }), tr("还没有人工指令。"));
}

/** Build a compact expandable inspector without interpreting result text as markup. */
function resultInspector(title, value) {
  const details = element("details", "inspector");
  details.append(element("summary", "", title), element("pre", "", printable(value)));
  return details;
}

/** Summarize the returned tool payload while keeping its full response inspectable. */
function toolSummary(call) {
  if (call.error) return printable(call.error);
  const result = call.result;
  if (result === null || result === undefined) return call.status === "started" || call.status === "running" ? tr("正在执行…") : tr("尚无返回内容");
  if (typeof result === "string") return result.slice(0, 220);
  if (result.summary || result.message || result.error) return printable(result.summary || result.message || result.error).slice(0, 220);
  const entries = result.entries || result.results || result.events || result.matches;
  if (Array.isArray(entries)) return tr("返回 {p0} 条记录{p1}", {p0: entries.length, p1: result.status ? ` · ${result.status}` : ""});
  const candidates = result.candidates || result.result?.candidates;
  if (Array.isArray(candidates)) return tr("已评估 {p0} 个候选{p1}", {p0: candidates.length, p1: result.status ? ` · ${result.status}` : ""});
  return result.status ? printable(result.status) : tr("已返回结果");
}

/** Label only an explicitly reported projection boundary, never inferred survival. */
function simulationHorizon(endpoint) {
  const horizon = String(endpoint?.horizon || "");
  const stages = {player_turn_end: tr("我方回合末"), enemy_actions_end: tr("敌方行动结束"),
    enemy_turn_end: tr("敌方回合末"), before_next_player_draw: tr("下一回合抽牌前"),
    next_player_turn_before_draw: tr("下一回合抽牌前"),
    after_first_action_before_combat_rollouts: tr("首动作后继（后续战斗见统计）")};
  if (stages[horizon]) return stages[horizon];
  if (/next_player|next_turn|enemy_turn_resolved|after_enemy/.test(horizon)) return tr("回合结算后");
  if (/before|current_player_turn/.test(horizon)) return tr("当前回合出牌后");
  return horizon || tr("局部推演");
}

/** Candidate ranking metric: policy probability or search/score value, never merged. */
function candidateMetric(row) {
  if (row.policy_candidate) return numeric(row.probability) ? {value: row.probability, kind: "probability"} : null;
  if (numeric(row.score)) return {value: row.score, kind: "score"};
  if (numeric(row.mcts?.mean)) return {value: row.mcts.mean, kind: "score"};
  return null;
}

/** Min-max scale per metric kind so near-ties still get distinguishable bars. */
function candidateScale(rows) {
  const groups = {score: [], probability: []};
  for (const row of rows) {
    const metric = candidateMetric(row);
    if (metric) groups[metric.kind].push(metric.value);
  }
  const result = {};
  for (const [kind, values] of Object.entries(groups)) {
    if (!values.length) continue;
    const min = Math.min(...values);
    const max = Math.max(...values);
    result[kind] = {min, max, span: max - min};
  }
  return result;
}

/** Relative 0-100 fill; identical values share a full bar because no ordering exists. */
function candidateFill(metric, scale) {
  const group = scale?.[metric.kind];
  if (!group) return 0;
  if (!(group.span > 0)) return 100;
  return Math.max(0, Math.min(1, (metric.value - group.min) / group.span)) * 100;
}

/** Format enough significant digits to separate near-identical candidate values. */
function formatMetric(metric) {
  if (metric.kind === "probability") {
    const percent = metric.value * 100;
    // Preserve near-certain vs certain instead of rounding both to 100.0%.
    if (percent >= 99.95) return `${percent.toFixed(3)}%`;
    if (percent >= 1) return `${percent.toFixed(1)}%`;
    if (percent >= 0.01) return `${percent.toFixed(3)}%`;
    return `${percent.toPrecision(3)}%`;
  }
  const text = metric.value.toPrecision(5);
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

/** Build one relative value bar shared by cards, drawer rows, and telemetry rows. */
function candidateMeter(metric, scale, className = "candidate-meter") {
  const meter = element("div", className);
  meter.title = tr("相对当前候选范围；非实战胜率");
  const fill = element("i");
  fill.style.width = `${candidateFill(metric, scale).toFixed(1)}%`;
  meter.append(fill);
  return meter;
}

/** Present candidate resources and support gaps without treating a score as a win probability. */
function renderSimulation(simulation) {
  const data = simulation || {};
  const signature = JSON.stringify(data);
  if (state.simulationSignature === signature) return;
  state.simulationSignature = signature;
  const advice = data.simulation_advice || data;
  const previews = advice.bounded_action_previews || {};
  const strategies = advice.strategies || {};
  const rows = Object.entries(strategies).flatMap(([name, value]) => {
    const best = {name: name === "mcts" ? tr("MCTS 推荐候选") : name, ...value};
    return name === "mcts" ? [best, ...(value.alternatives || []).map((item, index) =>
      ({...item, name: tr("其他候选 {p0}", {p0: index + 1}), status: "advisory"}))] : [best];
  });
  const runPolicy = Object.hasOwn(advice, "policy_status");
  if (runPolicy) {
    // Preserve the upstream grouped decoder's choice instead of ranking raw probabilities.
    rows.push(...(advice.candidates || []).map(item => ({...item,
      name: `PPO${item.selected ? tr(" 推荐") : ""} · ${item.label || tr("动作")}`,
      status: "advisory", policy_candidate: true})));
    if (!rows.length) rows.push({name: tr("PPO 整局策略"), status: "unavailable",
      reason: [...(advice.errors || []), ...(advice.support?.reasons || [])].join(" · ") || tr("当前公开状态不可用")});
  }
  rows.push(...(previews.candidates || []).map(item => ({...item, name: item.id, preview: true})));
  const prefix = data.prediction?.deterministic_prefix;
  for (const point of prefix?.checkpoints || []) {
    if (["player_turn_end", "enemy_actions_end", "enemy_turn_end", "before_next_player_draw"].includes(point.stage)) {
      rows.push({name: simulationHorizon(point.state), status: "completed", endpoint: point.state});
    }
  }
  const hypotheses = data.prediction?.uncertainty_evaluation?.candidates || [];
  rows.push(...hypotheses.map(item => ({...item, name: tr("抽牌后假想"), preview: true})));
  if (!runPolicy && rows.length && rows.every(row => ["unavailable", "unsupported", "skipped"].includes(row.status))) {
    const reasons = [...new Set(rows.flatMap(row => [row.reason, ...(row.support?.reasons || row.search_support_reasons || [])]).filter(Boolean))];
    rows.splice(0, rows.length, {name: tr("搜索"), status: "unavailable", reason: tr("当前局面无法比较出牌"),
      support: {reasons}, strategies});
  }
  const available = rows.some(row => row.status === "advisory" || row.status === "completed");
  const labels = {available: tr("可用"), advisory: tr("PPO 建议"), partial: tr("部分可用"), unavailable: tr("不可用"), skipped: advice.status === "model_candidates" ? tr("等待模型提案") : tr("未启用"), disabled: tr("未启用"), model_candidates: tr("等待模型提案")};
  setText("sim-badge", prefix?.checkpoints?.length ? tr("分阶段可用")
    : labels[data.status] || (available ? tr("有候选") : data.status || tr("等待局面")));
  const meta = [data.backend || advice.backend, numeric(data.elapsed_ms) ? `${(data.elapsed_ms / 1000).toFixed(2)}s` : ""].filter(Boolean);
  const total = strategies.mcts?.search_mcts;
  if (total) meta.push(tr("搜索总量 {p0} / 目标 {p1} 次", {p0: total.simulations_completed ?? total.visits ?? "—", p1: total.simulations_requested ?? "—"}),
    tr("总胜 {p0} · 总败 {p1} · 总未终局 {p2}（跨候选自适应分配）", {p0: total.wins ?? "—", p1: total.losses ?? "—", p2: total.cutoffs ?? "—"}));
  if (runPolicy && numeric(advice.value)) meta.push(tr("critic 估值 {p0}（非胜率）", {p0: advice.value.toFixed(3)}));
  if (runPolicy && advice.uncertainty) {
    const worlds = advice.uncertainty;
    meta.push(tr("公开假想世界 {p0} / {p1} · 失败 {p2}", {p0: worlds.completed ?? "—", p1: worlds.world_count ?? "—", p2: worlds.failed ?? "—"}));
  }
  setText("sim-meta", meta.join(" · "));
  const scale = candidateScale(rows);
  const cards = rows.map(row => {
    const card = element("article", "simulation-card");
    const top = element("div", "simulation-card-top");
    top.append(element("strong", "", row.name), element("span", "small-badge", row.policy_candidate && row.selected ? tr("已选") : row.status === "advisory" ? tr("候选") : row.status || tr("未知")));
    card.append(top);
    if (row.first_action || row.action) card.append(element("p", "simulation-action", actionLabel(row.first_action || row.action, state.snapshot?.observation)));
    const endpoint = row.endpoint || row.illustrative_sample?.endpoint;
    if (endpoint) {
      const player = endpoint.player || {};
      const facts = [simulationHorizon(endpoint)];
      if (player.hp !== undefined) facts.push(`HP ${healthText(player.hp, player.max_hp)}`);
      if (player.block !== undefined) facts.push(tr("格挡 {p0}", {p0: player.block}));
      if (player.energy !== undefined) facts.push(tr("能量 {p0}", {p0: player.energy}));
      card.append(element("p", "simulation-facts", facts.join(" · ")));
    }
    const metric = candidateMetric(row);
    if (row.policy_candidate) {
      card.append(element("p", "simulation-facts", metric
        ? tr("策略概率 {p0}（非胜率）", {p0: formatMetric(metric)}) : tr("原生唯一动作 · 无网络评分")));
      if (metric) card.append(candidateMeter(metric, scale));
    } else if (metric) {
      const score = element("span", "simulation-score", `${row.mcts ? tr("搜索价值") : tr("战术评分")} ${formatMetric(metric)}`);
      score.title = tr("精确值 {p0}；条形为当前候选内的相对位置，非实战胜率", {p0: metric.value});
      card.append(score, candidateMeter(metric, scale));
    }
    if (row.mcts) {
      const stats = row.mcts;
      card.append(element("p", "simulation-facts", tr("以该首动作为条件：模拟访问 {p0} · 胜 {p1} · 败 {p2} · 未终局 {p3}", {p0: stats.visits ?? "—", p1: stats.wins ?? "—", p2: stats.losses ?? "—", p3: stats.cutoffs ?? "—"})));
      if (numeric(stats.mean)) card.append(element("p", "simulation-facts", tr("模拟均值 {p0}（非实战胜率）", {p0: formatMetric({value: stats.mean, kind: "score"})})));
    }
    if (numeric(row.sample_count)) card.append(element("p", "simulation-facts", tr("采样完成 {p0} / {p1}", {p0: row.completed_samples ?? "—", p1: row.sample_count})));
    if (row.reason) card.append(element("p", "simulation-gap", tr(row.reason)));
    const gaps = row.support?.reasons || row.search_support_reasons;
    if (Array.isArray(gaps) && gaps.length) card.append(element("p", "simulation-gap", [...new Set(gaps)].slice(0, 3).map(gap => tr(gap)).join(" · ")));
    card.append(resultInspector(tr("候选详情"), row));
    return card;
  });
  if (!cards.length && data.diagnostics?.length) cards.push(element("p", "simulation-gap", data.diagnostics.map(item => item.code || tr(item.message)).filter(Boolean).join(" · ")));
  replaceCollection("sim-results", cards, tr("暂无推演。"));
  setText("simulation-json", simulation ? JSON.stringify(simulation, null, 2) : "—");
  renderCandidateSummary(rows, total, data);
}

/** Reuse reported candidate order and selection in the compact telemetry bay. */
function renderCandidateSummary(rows, total, data) {
  const candidates = rows.filter(row => row.first_action || row.action);
  const scale = candidateScale(candidates);
  const empty = simulationEmptyState(data, rows);
  const recommended = candidates.find(row => row.selected) || candidates.find(row => !row.policy_candidate);
  setText("sim-summary-time", numeric(data.elapsed_ms) ? `${(data.elapsed_ms / 1000).toFixed(2)}s` : "—");
  setText("sim-summary-count", candidates.length);
  setText("sim-candidate-count", candidates.length);
  setText("sim-summary-best", recommended ? actionLabel(recommended.first_action || recommended.action, state.snapshot?.observation) : "—");
  setText("sim-summary-search", total ? `${count(total.simulations_completed ?? total.visits)} / ${count(total.simulations_requested)}` : "—");
  setText("sim-summary-outcomes", total ? `${count(total.wins)} / ${count(total.losses)} / ${count(total.cutoffs)}` : "—");
  setText("side-candidate-meta", candidates.length ? tr("{p0} 个候选 · 非实战胜率", {p0: candidates.length}) : empty.label);
  setText("side-candidate-best", recommended ? candidateValue(recommended) : "—");
  replaceCollection("decision-candidate-list", candidates.map(row => {
    const node = element("div", "decision-candidate");
    const top = element("div", "decision-candidate-top");
    top.append(element("b", "", actionLabel(row.first_action || row.action, state.snapshot?.observation)), element("span", "", candidateValue(row)));
    node.append(top);
    const metric = candidateMetric(row);
    if (metric) node.append(candidateMeter(metric, scale, "decision-candidate-bar"));
    node.append(element("p", "decision-candidate-facts", row.selected ? tr("PPO 选定动作") : row.mcts ? tr("模拟访问 {p0}", {p0: count(row.mcts.visits)}) : tr(row.reason) || row.name));
    return node;
  }), empty.message);
  const head = element("div", "bay-head");
  head.append(element("span", "", tr("推演候选")), element("b", "", candidates.length ? `${Math.min(3, candidates.length)} / ${candidates.length}` : empty.label));
  const all = element("button", "bay-all", tr("全部候选"));
  all.type = "button";
  all.addEventListener("click", () => window.Observatory?.openDrawer("sim"));
  head.append(all);
  const list = element("ol", "bay-list");
  for (const [index, row] of candidates.slice(0, 3).entries()) {
    const item = element("li", "bay-row");
    item.append(element("span", "bay-rank", String(index + 1).padStart(2, "0")), element("span", "bay-name", actionLabel(row.first_action || row.action, state.snapshot?.observation)), element("span", "bay-score", candidateValue(row)));
    const metric = candidateMetric(row);
    if (metric) item.style.setProperty("--v", `${candidateFill(metric, scale).toFixed(1)}%`);
    item.title = row.policy_candidate ? tr("策略概率，非实战胜率") : tr("原生搜索价值，非实战胜率；条形为当前候选内的相对位置");
    list.append(item);
  }
  if (!candidates.length) list.append(element("li", "empty-state", empty.message));
  byId("bay-candidates").replaceChildren(head, list);
}

/** Distinguish unavailable advice from an idle step using reported backend diagnostics. */
function simulationEmptyState(data, rows) {
  const advice = data.simulation_advice || data;
  const reasons = [...new Set([
    ...(advice.errors || []), ...(advice.support?.reasons || []),
    ...(data.diagnostics || []).map(item => [...new Set([item.code || item.message, item.reason, item.error_kind].filter(Boolean))].join(" · ")),
    ...rows.flatMap(row => [row.reason, ...(row.support?.reasons || row.search_support_reasons || [])]),
  ].filter(Boolean).map(item => printable(item)))];
  const screen = state.snapshot?.observation?.state?.screen;
  if (reasons.includes("ppo_public_hp_missing") && ["MAIN_MENU", "CHARACTER_SELECT"].includes(screen)) {
    return {label: tr("当前场景不适用"), message: tr("进入对局后提供推演候选。")};
  }
  if (data.status === "unavailable" || reasons.includes("upstream_mirror_request_failed")) {
    const message = reasons.includes("upstream_mirror_request_failed")
      ? tr("推演进程请求失败（upstream_mirror_request_failed）")
      : reasons.join(" · ") || tr("后端未能完成本次推演。");
    return {label: tr("推演失败"), message};
  }
  if (advice.status === "model_candidates") return {label: tr("等待模型提案"), message: tr("本步由模型提出候选后再评估。")};
  if (["skipped", "disabled"].includes(data.status)) return {label: tr("本步未推演"), message: reasons.join(" · ") || tr("当前步骤未运行候选推演。")};
  return {label: tr("等待推演"), message: reasons.join(" · ") || tr("尚未收到本次推演结果。")};
}

/** Keep policy probability distinct from MCTS search value and absent measurements. */
function candidateValue(row) {
  const metric = candidateMetric(row);
  if (!metric) return "—";
  return `${formatMetric(metric)}${row.selected ? tr(" · 已选") : ""}`;
}

/** Classify factual program and tool events for the compact execution timeline. */
function eventPresentation(event) {
  const type = String(event.type || "event");
  const lower = type.toLowerCase();
  let category = "observe";
  let title = type.replaceAll("_", " ").replaceAll(".", " · ");
  if (/reasoning|content_delta|model.*delta/.test(lower)) return null;
  if (EVENT_PRESENTATIONS[lower]) [category, title] = EVENT_PRESENTATIONS[lower];
  else if (/calculat/.test(lower)) { category = "calculator"; title = tr("有限计算结果"); }
  else if (/receipt|settled|accepted|action.*result/.test(lower)) { category = "execution"; title = tr("游戏回执 / 结算"); }
  else if (/submit|dispatch|action_sent/.test(lower)) { category = "execution"; title = tr("程序提交动作"); }
  else if (/decision.*(ready|complete|valid)|decision_created/.test(lower)) { category = "model"; title = tr("模型决策已形成"); }
  else if (/model|provider|request/.test(lower)) { category = "model"; title = /complete|finish|success/.test(lower) ? tr("模型响应已完成") : /start|begin|sent/.test(lower) ? tr("发起模型请求") : tr("模型请求事件"); }
  else if (/observ/.test(lower)) title = tr("公开观测已更新");
  else if (/instruct/.test(lower)) title = tr("人工指令状态");
  else if (/command|control/.test(lower)) title = tr("控制命令回执");
  else if (/error|fail/.test(lower)) title = tr("程序错误");
  else if (/pause/.test(lower)) title = tr("控制器暂停状态");
  title = tr(title);
  const payload = event.payload || {};
  const summary = payload.summary ?? payload.message ?? payload.error ?? payload.reason;
  const parts = [];
  if (summary !== undefined) parts.push(tr(printable(summary)));
  if (payload.action) parts.push(tr("动作 {p0}", {p0: printable(payload.action)}));
  if (payload.status) parts.push(tr("状态 {p0}", {p0: payload.status}));
  if (payload.command) parts.push(tr("命令 {p0}", {p0: payload.command}));
  if (payload.model_request_id) parts.push(tr("请求 {p0}", {p0: payload.model_request_id}));
  if (payload.decision_id) parts.push(tr("决策 {p0}", {p0: payload.decision_id}));
  if (payload.result && category === "calculator") parts.push(printable(payload.result));
  if (category === "tool") parts.push(payload.name || payload.tool_name || tr("工具"));
  return { category, title, summary: (parts.join(" · ") || tr("事件 #{p0} · {p1}", {p0: printable(event.seq), p1: title})).slice(0, 700) };
}

/** Drop session-bound view state and reconnect with the new run's event cursor. */
function resetRunView() {
  state.lastSeq = 0;
  state.lastSnapshotSeq = -1;
  state.selectedRequest = "";
  state.requestPinned = false;
  state.decisionCards.clear();
  state.historySignature = "";
  state.speedHistory = [];
  state.speedPointAt = 0;
  state.streamDragging = false;
  state.toolSignature = "";
  state.simulationSignature = "";
  setFollowing(true);
  ["stream-box", "history-list", "timeline-list", "instruction-list"].forEach(id => { byId(id).scrollTop = 0; });
  if (state.source) {
    state.source.close();
    state.source = null;
    state.streamConnected = false;
  }
}

/** Compare sequence numbers only within the same authoritative run. */
function acceptSnapshot(snapshot, reset = false) {
  if (!snapshot || typeof snapshot !== "object") throw new Error(tr("控制器未返回有效快照"));
  window.ObservatoryI18n?.sync?.(snapshot.presentation_settings);
  const runId = snapshot.run_id || "";
  if (state.runId !== runId) {
    resetRunView();
    state.runId = runId;
  }
  const sequence = numeric(snapshot.seq) ? snapshot.seq : 0;
  if (!reset && sequence < state.lastSnapshotSeq) return;
  if (["starting", "ready", "error"].includes(snapshot.game_launch?.status)) state.launchPending = false;
  state.snapshot = snapshot;
  state.lastSnapshotSeq = sequence;
  state.lastSeq = reset ? sequence : Math.max(state.lastSeq, sequence);
  state.lastSnapshotAt = new Date().toISOString();
  renderConnection();
  renderSession(snapshot);
  renderGame(snapshot.observation);
  renderMetrics(snapshot.metrics || {});
  collectDecisions(snapshot);
  renderRequests(snapshot.model_requests);
  renderDecision(snapshot.decision);
  renderLiveUsage();
  renderInstructions(snapshot.instructions);
  renderSimulation(snapshot.simulation);
  window.ObservatoryTimeline?.render(snapshot);
  renderControls();
}

/** Fetch API JSON with a finite wait and useful backend error messages. */
async function api(path, options = {}) {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 12000);
  try {
    const response = await fetch(path, { cache: "no-store", ...options, signal: abort.signal });
    const body = await response.json().catch(() => null);
    if (!response.ok || body?.ok === false) throw new Error(tr(printable(body?.detail || body?.error || body?.message, tr("请求失败（HTTP {p0}）", {p0: response.status}))));
    if (body === null) throw new Error(tr("控制器返回了无法读取的响应"));
    return body;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(tr("本地请求超时；命令是否执行请以控制器后续快照为准。"));
    throw error;
  } finally { clearTimeout(timeout); }
}

/** Coalesce event bursts; snapshots remain authoritative for every rendered field. */
function scheduleSnapshot() {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(() => { state.refreshTimer = null; refreshSnapshot(); }, SNAPSHOT_DELAY_MS);
}

/** Refresh state and reconnect without ever starting another controller. */
async function refreshSnapshot(reset = false) {
  if (state.snapshotBusy) { state.snapshotAgain = true; return; }
  state.snapshotBusy = true;
  try {
    const snapshot = await api("/api/snapshot");
    state.apiAvailable = true;
    acceptSnapshot(snapshot, reset);
    if (!state.source) connectEvents();
  } catch (error) {
    state.apiAvailable = false;
    renderConnection(tr("无法读取本地控制器：{p0}{p1}", {p0: error.message, p1: state.snapshot ? tr(" 保留最后快照，正在自动重连。") : tr(" 正在自动重试。")}));
    scheduleReconnect();
  } finally {
    state.snapshotBusy = false;
    if (state.snapshotAgain) { state.snapshotAgain = false; scheduleSnapshot(); }
  }
}

/** Retry disconnected transports with a bounded backoff and the last event cursor. */
function scheduleReconnect() {
  if (state.retryTimer) return;
  const delay = Math.min(12000, RECONNECT_BASE_MS * 2 ** Math.min(state.reconnectAttempt++, 3));
  state.retryTimer = setTimeout(() => {
    state.retryTimer = null;
    refreshSnapshot();
    if (state.snapshot && !state.source) connectEvents();
  }, delay);
}

/** Subscribe once, deduplicate event sequence numbers, and recover reset gaps. */
function connectEvents() {
  if (state.source) return;
  const source = new EventSource(`/api/events?after=${encodeURIComponent(state.lastSeq)}`);
  state.source = source;
  source.onopen = () => {
    if (state.source !== source) return;
    state.streamConnected = true;
    state.reconnectAttempt = 0;
    renderConnection();
    if (state.snapshot) renderSession(state.snapshot);
    scheduleSnapshot();
  };
  source.addEventListener("event", message => {
    if (state.source !== source) return;
    try {
      const event = JSON.parse(message.data);
      if (!numeric(event.seq) || event.seq <= state.lastSeq) return;
      state.lastSeq = event.seq;
      scheduleSnapshot();
    } catch { scheduleSnapshot(); }
  });
  source.addEventListener("reset", () => {
    if (state.source !== source) return;
    source.close();
    state.source = null;
    state.streamConnected = false;
    state.lastSeq = 0;
    state.lastSnapshotSeq = -1;
    refreshSnapshot(true);
  });
  source.onerror = () => {
    if (state.source !== source) return;
    source.close();
    state.source = null;
    state.streamConnected = false;
    renderConnection();
    scheduleReconnect();
  };
}

/** Preserve unsaved selections while rendering the server's authoritative edit lock. */
function renderLaunchSettings(available, launching) {
  const settings = state.snapshot?.launch_settings;
  const character = byId("char-select");
  const ascension = byId("ascension");
  const previousCharacter = character.value;
  if (settings?.choices?.length) {
    const signature = JSON.stringify(settings.choices);
    if (signature !== state.launchChoicesSignature) {
      state.launchChoicesSignature = signature;
      character.replaceChildren(...settings.choices.map(choice => {
        const option = element("option", "", tr(choice.label));
        option.value = choice.id;
        return option;
      }));
      character.value = previousCharacter;
    }
  }
  if (settings && (!state.launchSettingsDirty || settings.editable !== true)) {
    character.value = settings.character;
    ascension.value = String(settings.ascension);
    state.launchSettingsDirty = false;
  }
  character.disabled = ascension.disabled = !available || launching || settings?.editable !== true;
  byId("launch-settings").title = tr(settings?.reason) || tr("选择新一局的角色与难度，点击启动游戏后生效。");
  setText("launch-settings-note", tr(settings?.reason) || (settings?.editable ? tr("启动后仍保持暂停。") : tr("角色与难度暂不可修改。")));
  updateAscensionLabel();
}

/** Keep the slider's visible and accessible difficulty labels synchronized. */
function updateAscensionLabel() {
  const value = byId("ascension").value || "0";
  setText("asc-value", `A${value}`);
  byId("ascension").setAttribute("aria-valuetext", tr("进阶 {p0}", {p0: value}));
}

/** Describe rollout effort without treating it as a live outcome prediction. */
function updateSimulationStrength() {
  const slider = byId("simulation-strength");
  const value = Number(slider.value);
  // Interpolate the original five budgets; keep the input's full precision when saving.
  const counts = [250, 500, 1000, 2500, 5000];
  const index = Math.min(3, Math.floor(value) - 1);
  const simulations = Math.round(counts[index] + (counts[index + 1] - counts[index]) * (value - index - 1));
  const progress = (value - 1) / 4;
  const effects = byId("simulation-strength-effects");
  effects.style.setProperty("--strength-progress", `${progress * 100}%`);
  effects.style.setProperty("--strength-hue", String(190 - 280 * progress));
  effects.style.setProperty("--strength-energy", String(progress * progress));
  effects.style.setProperty("--strength-glow", `${2 + 18 * progress * progress}px`);
  effects.style.setProperty("--strength-speed", `${3 - 1.8 * progress}s`);
  effects.classList.toggle("charged", progress > 0.6);
  effects.classList.toggle("overdrive", progress > 0.85);
  byId("strength-fill").style.width = `${progress * 100}%`;
  const text = tr("{p0}{p1} · 目标普通 {p2} / Boss {p3} 次", {p0: value.toFixed(2), p1: value === 3 ? tr(" · Mentor 默认") : "", p2: simulations, p3: simulations * 4});
  setText("simulation-strength-value", text);
  slider.setAttribute("aria-valuetext", text);
}

/** Preserve in-progress form edits while displaying the saved session permission. */
function renderSimulationSettings(available) {
  const settings = state.snapshot?.simulation_settings;
  if (!settings) return;
  if (!state.simulationSettingsDirty || !settings.editable) {
    const modes = {combat_decision_mode: settings.candidate_source === "model" ? "llm" : "assist",
      run_decision_mode: "llm", rollout_policy: "upstream", action_prior: "upstream",
      value_model: "texel", search_profile: "upstream", reward_pool: "known", ...settings};
    for (const [field, id] of Object.entries(SIMULATION_SELECTS)) byId(id).value = modes[field];
    byId("simulation-strength").value = String(settings.search_strength);
    state.simulationSettingsDirty = false;
  }
  for (const id of [...Object.values(SIMULATION_SELECTS), "simulation-strength", "simulation-settings-save"]) byId(id).disabled = !available || !settings.editable;
  document.querySelectorAll("[data-network-preset]").forEach(button => { button.disabled = !available || !settings.editable; });
  setText("policy-tag", `NoSL · ${settings.read_access === "internal" ? tr("内部数据") : tr("公开信息")}`);
  byId("policy-tag").title = state.snapshot?.human_assisted ? tr("本局包含人工策略辅助") : tr("以控制器记录的公平性边界为准");
  setText("simulation-settings-note", settings.editable ? state.simulationSettingsDirty ? tr("有未保存的设置") : tr("设置已与控制器同步") : tr("暂停并等待在途工作完成后可修改"));
  updateSimulationStrength();
  updateDecisionModeNote();
}

/** Describe the selected execution owner without saving a draft or calling a model. */
function updateDecisionModeNote() {
  const combat = byId("simulation-combat-mode").value;
  const run = byId("simulation-run-mode").value;
  const direct = combat === "upstream" && run === "upstream";
  setText("simulation-decision-note", direct
    ? tr("两类决策均由Mentor执行，不调用大模型；{p0}保存后生效。", {p0: byId("simulation-reward-pool").value === "full_unlock" ? tr("随机奖励按全解锁池推演；") : tr("奖励池未知时会明确暂停；")})
    : tr("大模型仅参与选择了大模型的决策阶段；预设和选项保存后生效。"));
}

/** Fill an inspectable draft; even a no-LLM preset still requires the explicit save button. */
function selectNetworkPreset(preset) {
  const mode = preset === "base" ? "llm" : preset;
  byId("simulation-combat-mode").value = mode;
  byId("simulation-run-mode").value = mode;
  byId("simulation-rollout").value = preset === "base" ? "heuristic" : "upstream";
  byId("simulation-prior").value = preset === "base" ? "off" : "upstream";
  byId("simulation-value").value = "texel";
  byId("simulation-profile").value = preset === "base" ? "base" : "upstream";
  if (preset === "upstream") byId("simulation-reward-pool").value = "full_unlock";
  state.simulationSettingsDirty = true;
  updateDecisionModeNote();
}

/** Save both independent modes and effort only through the paused session gate. */
async function saveSimulationSettings(event) {
  event.preventDefault();
  const body = {...Object.fromEntries(Object.entries(SIMULATION_SELECTS).map(([field, id]) => [field, byId(id).value])),
    candidate_source: byId("simulation-combat-mode").value === "llm" ? "model" : "mcts",
    search_strength: Number(byId("simulation-strength").value)};
  try {
    const result = await api("/api/simulation/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    state.snapshot.simulation_settings = result.simulation_settings;
    state.simulationSettingsDirty = false;
    toast(tr("推演设置已保存，已消耗预算保留。"));
    renderControls();
  } catch (error) { toast(error.message, true); }
}

/** Enable explicit controls only after a current controller response. */
function renderControls() {
  const snapshot = state.snapshot;
  const status = String(snapshot?.status || "idle").toLowerCase();
  const gameOver = isGameOver(snapshot);
  const inactive = gameOver || !snapshot?.run_id || ["idle", "stopped", "completed", "failed", "error", "budget_stop"].includes(status);
  const paused = status === "paused" && !snapshot?.inflight;
  const available = state.apiAvailable && !state.busy && Boolean(snapshot);
  const launching = state.launchPending || snapshot?.game_launch?.status === "starting";
  const canPlay = available && !inactive && !launching && snapshot?.connection?.connected === true;
  const hasPlayed = Boolean(snapshot?.budget_started_utc) || (snapshot?.metrics?.requests_used || 0) > 0 || (snapshot?.metrics?.actions_completed || 0) > 0;
  renderLaunchSettings(available, launching);
  renderSimulationSettings(available);
  byId("start").hidden = false;
  setText("start-label", launching ? tr("启动中…") : tr("启动"));
  byId("start").disabled = !available || launching;
  const toggle = byId("run-toggle");
  const pausing = !paused && snapshot?.desired_state === "paused";
  const pauseMode = !inactive && !paused;
  toggle.dataset.mode = pauseMode ? "pause" : "resume";
  toggle.querySelector("span").textContent = pauseMode ? pausing ? tr("暂停中") : tr("暂停") : hasPlayed ? tr("继续") : tr("开始");
  toggle.querySelector("use").setAttribute("href", pauseMode ? "#i-pause" : "#i-play");
  toggle.disabled = pauseMode ? !available || pausing : !canPlay || !paused;
  toggle.title = pauseMode ? tr("暂停自动游玩，等待在途工作完成") : hasPlayed ? tr("继续自动游玩") : tr("开始自动游玩");
  toggle.setAttribute("aria-label", toggle.title);
  byId("step").disabled = !canPlay || !paused;
  byId("stop").disabled = !available || inactive;
  byId("queue").disabled = !available || inactive || !byId("instruction-text").value.trim();
  byId("redecide").disabled = !canPlay;
  document.querySelectorAll(".revoke-button").forEach(button => { button.disabled = !available; });
}

/** Show command receipts separately from program and game events. */
function toast(message, error = false) {
  const node = byId("toast");
  clearTimeout(state.toastTimer);
  node.textContent = tr(message);
  node.className = `toast${error ? " error" : ""}`;
  node.hidden = false;
  state.toastTimer = setTimeout(() => { node.hidden = true; }, error ? 9000 : 5000);
}

/** Post each unique user command once; an uncertain response is never replayed. */
async function postCommand(path, payload, successMessage) {
  if (state.busy) return false;
  state.busy = true;
  renderControls();
  try {
    const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command_id: crypto.randomUUID(), ...payload }) });
    if (result.snapshot) acceptSnapshot(result.snapshot);
    const receiptStatus = result.receipt?.status;
    if (["rejected", "failed", "error"].includes(receiptStatus)) {
      toast(printable(result.receipt?.message || result.receipt?.reason, tr("控制器拒绝了本次请求，请查看时间线。")), true);
      scheduleSnapshot();
      return false;
    }
    toast(printable(result.receipt?.message, successMessage));
    scheduleSnapshot();
    return true;
  } catch (error) {
    toast(tr("请求未确认：{p0}", {p0: error.message}), true);
    scheduleSnapshot();
    return false;
  } finally { state.busy = false; renderControls(); }
}

/** Start the game only after an explicit click, keeping automatic play paused. */
async function startGame() {
  if (byId("start").disabled) return;
  const selection = {character: byId("char-select").value, ascension: Number(byId("ascension").value)};
  const editable = state.snapshot?.launch_settings?.editable === true;
  state.launchPending = true;
  renderGameLaunch(state.snapshot);
  if (editable) {
    state.busy = true;
    renderControls();
    try {
      const result = await api("/api/game/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(selection)});
      state.snapshot.launch_settings = result.launch_settings;
      state.launchSettingsDirty = false;
    } catch (error) {
      state.launchPending = false;
      toast(tr("设置未保存：{p0}", {p0: error.message}), true);
      scheduleSnapshot();
      return;
    } finally {
      state.busy = false;
      renderControls();
      renderGameLaunch(state.snapshot);
    }
  }
  const accepted = await postCommand("/api/game/start", {}, tr("启动请求已收到，游戏就绪后仍保持暂停。"));
  if (!accepted) state.launchPending = false;
  renderControls();
  renderGameLaunch(state.snapshot);
}

/** Send only an explicit controller command chosen by the user. */
async function sendControl(command) {
  const messages = { start: tr("会话创建请求已收到；控制器将保持暂停。"), pause: tr("暂停请求已收到，等待安全点确认。"), resume: tr("继续请求已收到，请以运行状态为准。"), step: tr("单步请求已收到，仅放行一个原生合法动作。"), redecide: tr("重决策请求已收到，新方案生成后保持暂停。"), stop: tr("停止请求已收到，等待控制器确认。") };
  await postCommand("/api/commands", { command }, messages[command]);
}

/** Submit natural-language guidance with an explicit scope and decision timing. */
async function submitInstruction(timing) {
  const input = byId("instruction-text");
  const text = input.value.trim();
  if (!text) {
    if (timing === "redecide") await sendControl("redecide");
    return;
  }
  if (text.length > 3000) { toast(tr("指导内容最多 3000 字符。"), true); return; }
  const success = await postCommand("/api/instructions", { text, scope: byId("instruction-scope").value, timing }, timing === "redecide" ? tr("指导与重决策请求已收到，新方案生成后保持暂停。") : tr("指导已提交，将在下一次完整决策加入。"));
  if (success && input.value.trim() === text) {
    input.value = "";
    updateInput();
  }
}

/** Revoke future instruction use without claiming earlier model input is undone. */
async function revokeInstruction(id) {
  await postCommand(`/api/instructions/${encodeURIComponent(id)}/revoke`, {}, tr("撤销请求已收到，默认影响后续完整决策。"));
}

/** Update guidance character count and readiness as the user types. */
function updateInput() {
  setText("char-count", `${byId("instruction-text").value.length} / 3000`);
  renderControls();
}

/** Toggle stream following; scrolling away also suspends automatic following. */
function setFollowing(follow) {
  state.follow = follow;
  const button = byId("follow-button");
  button.classList.toggle("active", follow);
  button.setAttribute("aria-pressed", String(follow));
  button.textContent = follow ? tr("↓ 跟随输出") : tr("↓ 恢复跟随");
  if (follow) byId("stream-box").scrollTop = byId("stream-box").scrollHeight;
}

/** Bind local view interactions and begin read-only transport discovery. */
function initialize() {
  window.ObservatoryI18n?.initialize?.();
  window.Observatory?.initialize();
  window.ObservatoryTimeline?.initialize?.();
  byId("simulation-settings").addEventListener("submit", saveSimulationSettings);
  for (const id of [...Object.values(SIMULATION_SELECTS), "simulation-strength"]) byId(id).addEventListener("input", () => { state.simulationSettingsDirty = true; updateSimulationStrength(); updateDecisionModeNote(); });
  document.querySelectorAll("[data-network-preset]").forEach(button => button.addEventListener("click", () => selectNetworkPreset(button.dataset.networkPreset)));
  byId("start").addEventListener("click", startGame);
  byId("run-toggle").addEventListener("click", () => sendControl(byId("run-toggle").dataset.mode));
  byId("step").addEventListener("click", () => sendControl("step"));
  byId("stop").addEventListener("click", () => sendControl("stop"));
  byId("char-select").addEventListener("change", () => { state.launchSettingsDirty = true; });
  byId("ascension").addEventListener("input", () => { state.launchSettingsDirty = true; updateAscensionLabel(); });
  byId("instruction-form").addEventListener("submit", event => { event.preventDefault(); submitInstruction("next_decision"); });
  byId("redecide").addEventListener("click", () => submitInstruction("redecide"));
  byId("instruction-text").addEventListener("input", updateInput);
  byId("follow-button").addEventListener("click", () => setFollowing(!state.follow));
  byId("stream-box").addEventListener("wheel", event => { if (event.deltaY < 0) setFollowing(false); }, { passive: true });
  byId("stream-box").addEventListener("touchmove", () => setFollowing(false), { passive: true });
  byId("stream-box").addEventListener("keydown", event => { if (["ArrowUp", "PageUp", "Home"].includes(event.key)) setFollowing(false); });
  byId("stream-box").addEventListener("pointerdown", () => { state.streamDragging = true; });
  window.addEventListener("pointerup", () => { state.streamDragging = false; });
  byId("stream-box").addEventListener("scroll", () => {
    const box = byId("stream-box");
    // Text growth and collapsed details also emit scroll; only a user's drag pauses following.
    if (state.streamDragging && state.follow && box.scrollHeight - box.clientHeight - box.scrollTop > 50) setFollowing(false);
  });
  byId("request-select").addEventListener("change", event => {
    state.selectedRequest = event.target.value;
    state.requestPinned = state.selectedRequest !== state.snapshot?.model_requests?.at(-1)?.model_request_id;
    renderStream();
  });
  document.querySelectorAll("[data-stream]").forEach(button => button.addEventListener("click", () => {
    state.stream = button.dataset.stream;
    document.querySelectorAll("[data-stream]").forEach(tab => {
      const active = tab === button;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    byId("stream-text").setAttribute("aria-labelledby", button.id);
    renderRequests(state.snapshot?.model_requests);
  }));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleSnapshot(); });
  // This timer is read-only and reuses the existing coalesced, single-flight fetch.
  setInterval(() => {
    if (!document.hidden && state.apiAvailable && state.snapshot) scheduleSnapshot();
  }, QUIET_SNAPSHOT_INTERVAL_MS);
  setInterval(() => { if (!document.hidden && state.snapshot) renderLiveUsage(); }, SPEED_REFRESH_MS);
  renderControls();
  refreshSnapshot();
}

initialize();

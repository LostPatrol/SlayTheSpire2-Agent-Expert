/** Verify factual history association and view persistence without a game or network. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
// The renderer uses only text nodes, element attributes, and explicit event handlers.
class Element {
  /** Record the small DOM surface used by the production history renderer. */
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.listeners = {}; this.textContent = ""; }
  /** Retain child nodes to inspect grouping and redraw behavior. */
  append(...children) { children.forEach(child => { child.parentElement = this; }); this.children.push(...children); }
  /** Insert a local inspector without replacing its parent timeline. */
  after(child) { child.parentElement = this.parentElement; this.parentElement.children.splice(this.parentElement.children.indexOf(this) + 1, 0, child); }
  /** Detach only the inspector that the reader closed. */
  remove() { this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); }
  /** Replace a container exactly as a browser does during rendering. */
  replaceChildren(...children) { this.children = []; this.append(...children); }
  /** Support the production renderer's dynamically inserted history controls. */
  insertBefore(child, reference) {
    child.parentElement = this;
    this.children.splice(this.children.indexOf(reference), 0, child);
    if (child.id) nodes.set(child.id, child);
  }
  /** Record accessibility attributes and symbol references. */
  setAttribute(key, value) { this[key] = value; }
  /** Resolve the class-only ancestors used to retain raw inspector scroll positions. */
  closest(selector) {
    for (let item = this; item; item = item.parentElement) {
      if ((item.className || "").split(" ").includes(selector.slice(1))) return item;
    }
    return null;
  }
  /** Find the owning row immediately before a raw inspector. */
  get previousElementSibling() {
    const siblings = this.parentElement?.children || [];
    return siblings[siblings.indexOf(this) - 1] || null;
  }
  /** Expose only the production renderer's inspector scroll query. */
  querySelectorAll(selector) {
    assert.equal(selector, ".history-raw-inspector .json-block");
    const found = [];
    /** Collect matching descendants without inventing unrelated DOM capabilities. */
    function visit(item) {
      for (const child of item.children) {
        if ((child.className || "").split(" ").includes("json-block") && child.closest(".history-raw-inspector")) found.push(child);
        visit(child);
      }
    }
    visit(this);
    return found;
  }
  /** Expose real production handlers for simulated user interaction. */
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
// Use the exact v2 mount points; no hidden compatibility elements are supplied.
const ids = ["stream-list", "stream-count", "stream-tools", "stream-requests", "stream-events", "history-list", "history-count",
  "stream-average", "tool-average", "tools-list", "tool-count", "timeline-list", "pipeline-stream", "pipeline-tool-count", "pipeline-decision-count", "pipeline-event-count"];
const nodes = new Map(ids.map(id => [id, new Element("div")]));
const context = vm.createContext({window: {}, document: {
  getElementById: id => nodes.get(id) || null,
  createElement: tag => new Element(tag),
  createElementNS: (_namespace, tag) => new Element(tag),
}, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/sts2_agent/web/timeline.js"), "utf8"), context);
// All assertions exercise the shipped implementation, including its actual DOM listeners.
const timeline = context.window.ObservatoryTimeline;
const at = "2026-11-01T10:00:00+08:00";
const snapshot = {run_id: "run-one", decision_history: [
  {decision_id: "old", timestamp_utc: "2026-11-01T09:00:00+08:00", action: {type: "play_card", card_index: 4},
    action_label: "记录中的旧卡牌", reason: "<script>untrusted()</script>", status: "executed"},
  {decision_id: "new", timestamp_utc: at, action: {type: "end_turn"}, status: "submitted"},
], decision: {decision_id: "new", action: {type: "end_turn"}},
tool_calls: [
  {tool_call_id: "tool-a", decision_id: "old", name: "wiki_lookup", started_utc: at, status: "running"},
  {tool_call_id: "tool-a", decision_id: "old", name: "wiki_lookup", started_utc: at, status: "completed", result: {entries: [1]}, elapsed_ms: 0},
  {tool_call_id: "tool-b", decision_id: "pending", name: "simulation_evaluate", started_utc: at, status: "running"},
], model_requests: [
  {model_request_id: "request-a", decision_id: "old", requested_model: "flash", started_utc: at, status: "completed", duration_ms: 2300},
  {model_request_id: "request-a", decision_id: "old", requested_model: "flash", started_utc: at, status: "completed", duration_ms: 2400},
  {model_request_id: "request-b", decision_id: "pending", requested_model: "flash", started_utc: at, status: "generating"},
], events: [
  {seq: 1, timestamp_utc: at, type: "action_submitted", payload: {run_id: "run-one", action_id: "action-a", decision_id: "old"}},
  {seq: 2, timestamp_utc: at, type: "action_receipt", payload: {action_id: "action-a", status: "completed"}},
  {seq: 3, timestamp_utc: at, type: "observation", payload: {message: "No decision identity"}},
  {seq: 3, timestamp_utc: at, type: "observation", payload: {message: "No decision identity"}},
  {seq: 4, timestamp_utc: at, type: "model_reasoning_delta", payload: {decision_id: "old", text: "x"}},
  {seq: 5, timestamp_utc: at, type: "observation", payload: {run_id: "another-run", message: "Not this run"}},
], simulation: {simulation_advice: {strategies: {mcts: {score: 9999}}}},
observation: {state: {combat: {hand: [{i: 4, name: "当前其他卡牌"}]}}}};

// Equal timestamps never associate records; only explicit decision/action IDs may do so.
const data = timeline.buildGroups(snapshot);
const old = data.groups.find(group => group.decisionId === "old");
const current = data.groups.find(group => group.decisionId === "new");
const pending = data.groups.find(group => group.decisionId === "pending");
const session = data.groups.find(group => group.key === "session");
assert.equal(data.decisions.length, 2);
assert.equal(data.counts.tool, 2);
assert.equal(data.counts.request, 2);
assert.equal(data.counts.event, 3);
assert.equal(old.counts.tool, 1);
assert.equal(old.counts.request, 1);
assert.equal(old.counts.event, 2);
assert.equal(old.name, "记录中的旧卡牌");
assert.equal(old.children.find(row => row.kind === "tool").tail, "0.00s");
assert.equal(old.children.find(row => row.kind === "request").tail, "2.40s");
assert.equal(current.children.length, 0);
assert.equal(current.status, "submitted");
assert.equal(pending.status, "unresolved");
assert.equal(pending.record, null);
assert.equal(pending.counts.tool, 1);
assert.equal(session.counts.event, 1);
assert.equal(session.children[0].meta, "No decision identity");
assert.equal(old.score, undefined);
assert.equal(data.rows.some(row => row.name.includes("delta")), false);

// Request cancellation has a terminal group state without fabricating a decision or execution.
const cancelling = {run_id: "cancel-run", request_pending: true, generation_cancelling: true,
  model_requests: [{model_request_id: "cancel-request", decision_id: "cancel-decision", started_utc: at, status: "cancelling"}]};
assert.equal(timeline.buildGroups(cancelling).groups[0].status, "cancelling");
assert.match(timeline.buildGroups(cancelling).rows[0].meta, /^正在停止输出/);
assert.equal(timeline.buildGroups({...cancelling, model_requests: cancelling.model_requests.map(request => ({...request,
  status: "generating"}))}).groups[0].status, "cancelling");
const cancelled = {...cancelling, request_pending: false, generation_cancelling: false,
  model_requests: cancelling.model_requests.map(request => ({...request, status: "cancelled"})),
  events: [{seq: 20, timestamp_utc: at, type: "model_request_cancelled", payload: {
    decision_id: "cancel-decision", model_request_id: "cancel-request"}}]};
const cancelledData = timeline.buildGroups(cancelled);
assert.equal(cancelledData.groups[0].status, "cancelled");
assert.equal(cancelledData.groups[0].name, "本轮生成已取消");
assert.equal(cancelledData.decisions.length, 0);
assert.equal(cancelledData.events[0].status, "cancelled");
assert.equal(cancelledData.events[0].name, "模型请求已取消");
assert.equal(timeline.buildGroups({...cancelled, model_requests: []}).groups[0].status, "cancelled");
assert.equal(timeline.buildGroups({...cancelled, decision_history: [{decision_id: "cancel-decision",
  status: "pending", _history_complete: true}]}).groups[0].status, "cancelled");
assert.equal(timeline.buildGroups({...cancelled, decision_history: [{decision_id: "cancel-decision",
  status: "executed", action: {type: "end_turn"}}]}).groups[0].status, "executed");

// Live decision and execution facts classify a new row before any asynchronous archive response.
const eventOnly = timeline.buildGroups({run_id: "fresh", events: [
  {seq: 10, timestamp_utc: at, type: "decision", payload: {decision_id: "live-event", action: {type: "choose_rest_option"},
    action_label: "当前营火选择", reason: "公开决策理由"}},
  {seq: 11, timestamp_utc: at, type: "action_completed", payload: {decision_id: "live-event", action: {type: "choose_rest_option"}}},
]});
assert.equal(eventOnly.groups[0].type, "rest");
assert.equal(eventOnly.groups[0].status, "executed");
assert.equal(eventOnly.groups[0].name, "当前营火选择");
const freshAction = timeline.buildGroups({run_id: "fresh", decision_history: [{decision_id: "same", action: {type: "play_card"}, status: "proposed"}],
  decision: {decision_id: "same", action: {type: "use_potion"}, action_label: "最新公开动作", status: "executed"}});
assert.equal(freshAction.groups[0].type, "potion");
assert.equal(freshAction.groups[0].status, "executed");
assert.equal(freshAction.groups[0].name, "最新公开动作");

// Repeated data must retain DOM nodes, reader expansion, and explicit raw text safety.
let expanded = null;
timeline.render(snapshot, {onExpand: id => { expanded = id; }});
const firstList = nodes.get("stream-list").children;
assert.equal(firstList.every(card => card.dataset.open === "false"), true);
assert.equal(firstList.some(card => card.dataset.groupId === "session"), false);
const oldCard = firstList.find(card => card.dataset.groupId === "decision:old");
const currentCard = firstList.find(card => card.dataset.groupId === "decision:new");
assert.equal(currentCard.children[0].children.find(child => child.className === "stream-counts").textContent, "详情待载入");
assert.match(oldCard.children[0].children.find(child => child.className === "stream-counts").textContent, /^已载入/);
const oldHead = oldCard.children[0];
oldHead.listeners.click();
assert.equal(expanded, "old");
assert.equal(oldCard.dataset.open, "true");
assert.equal(oldHead["aria-expanded"], "true");
assert.equal(oldCard.children[1].children[0].textContent, "<script>untrusted()</script>");
assert.equal(nodes.get("stream-tools").textContent, "2");
assert.equal(nodes.get("stream-requests").textContent, "2");
timeline.render(snapshot);
assert.equal(nodes.get("stream-list").children, firstList);

// Streaming text is handled by the output drawer and must not repaint unchanged timeline rows.
const moreText = {...snapshot, model_requests: snapshot.model_requests.map(request => ({...request, content: "a new token"}))};
timeline.render(moreText);
assert.equal(nodes.get("stream-list").children, firstList);

// A real lifecycle update redraws content while preserving the expanded decision.
const updated = {...snapshot, decision_history: snapshot.decision_history.map(record => record.decision_id === "new" ? {...record, status: "executed"} : record)};
timeline.render(updated);
const updatedList = nodes.get("stream-list").children;
assert.notEqual(updatedList, firstList);
assert.equal(updatedList.find(card => card.dataset.groupId === "decision:old").dataset.open, "true");
assert.equal(updatedList.find(card => card.dataset.groupId === "decision:new").dataset.status, "executed");
assert.equal(updatedList.find(card => card.dataset.groupId === "decision:new").dataset.open, "false");

// Stream counters and timing exclude hidden session rows; split statistics retain them.
timeline.render({...snapshot, tool_calls: [...snapshot.tool_calls,
  {tool_call_id: "unassociated-tool", name: "wiki_search", status: "completed", started_utc: at, elapsed_ms: 10000}],
model_requests: [...snapshot.model_requests, {model_request_id: "unassociated-request", status: "completed", started_utc: at}]});
assert.equal(nodes.get("stream-tools").textContent, "2");
assert.equal(nodes.get("stream-requests").textContent, "2");
assert.equal(nodes.get("stream-events").textContent, "2");
assert.equal(nodes.get("stream-average").textContent, "0.00s");
assert.equal(nodes.get("tool-average").textContent, "5.00s");
assert.equal(nodes.get("tool-count").textContent, "3 次已载入");
assert.equal(nodes.get("pipeline-event-count").textContent, "3");

// Changing run identity clears old preferences and old records, even when IDs recur.
timeline.render({...updated, run_id: "run-two", events: []});
assert.equal(nodes.get("stream-list").children.find(card => card.dataset.groupId === "decision:old").dataset.open, "false");
assert.equal(nodes.get("stream-events").textContent, "0");
timeline.render({run_id: "empty"});
assert.equal(nodes.get("stream-count").textContent, "已加载 0 条决策记录");
assert.equal(nodes.get("stream-list").children.length, 1);
assert.equal(nodes.get("stream-tools").textContent, "0");
assert.equal(nodes.get("stream-average").textContent, "—");

console.log("Dashboard timeline: ID association, deduplication, missing evidence, rendering, and run reset passed.");

/** Keep large raw records complete without repeated serialization or whole-timeline replacement. */
function testLargeInspector() {
  let serializations = 0;
  const payload = {text: "x".repeat(11930) + "🧪".repeat(30) + "公开记录".repeat(1000000)};
  const result = {toJSON() { serializations += 1; return payload; }};
  const call = {tool_call_id: "large-tool", decision_id: "large", name: "simulation_evaluate", status: "completed", result};
  const sample = {run_id: "large", decision_history: [{decision_id: "large", status: "executed", action: {type: "end_turn"}}],
    tool_calls: [call]};
  timeline.render(sample);
  assert.equal(serializations, 0, "Folded tools and render signatures must not serialize raw results.");
  const list = nodes.get("stream-list").children;
  const group = list[0];
  group.children[0].listeners.click();
  const rows = group.children[1].children.find(child => child.className === "stream-rows");
  const button = rows.children[0].children[3].children[1];
  button.listeners.click();
  assert.equal(nodes.get("stream-list").children, list);
  assert.equal(serializations, 1);
  const inspector = rows.children[1];
  const [content, controls] = inspector.children;
  const [previous, label, next] = controls.children;
  const segments = [];
  do {
    assert.ok(content.textContent.length <= 12001);
    assert.ok(!/[\uD800-\uDBFF]$/.test(content.textContent));
    segments.push(content.textContent);
    if (next.disabled) break;
    next.listeners.click();
  } while (true);
  assert.equal(segments.join(""), JSON.stringify({...call, result: payload}, null, 2));
  assert.equal(serializations, 1, "Paging uses the same serialized record.");
  assert.equal(nodes.get("stream-list").children, list);
  const lastLabel = label.textContent;
  timeline.render(sample);
  assert.equal(nodes.get("stream-list").children, list);
  assert.equal(serializations, 1);
  previous.listeners.click();
  const savedPage = label.textContent;
  button.listeners.click();
  assert.equal(rows.children.length, 1);
  button.listeners.click();
  assert.equal(rows.children[1].children[1].children[1].textContent, savedPage);
  assert.notEqual(savedPage, lastLabel);
  console.log("Dashboard large inspector: bounded layout, lossless pages, local updates, and page retention passed.");
}

testLargeInspector();

/** Drain asynchronous JSON fetch completion without real timers or network access. */
async function settle() { for (let index = 0; index < 12; index += 1) await Promise.resolve(); }

/** Exercise real read-only pagination, lazy detail, indexing, and request races. */
async function testHistory() {
  const timers = new Map();
  let timerId = 0;
  const calls = [];
  let indexReady = false;
  let delayedResponse;
  const archived = {decision_id: "archive-new", action: {type: "end_turn"}, action_label: "较新历史动作",
    timestamp_utc: at, status: "executed", counts: {tool: 1, request: 1, event: 1}};
  const older = {decision_id: "archive-old", action: {type: "choose_map_node"}, action_label: "较早历史动作",
    timestamp_utc: "2026-11-01T09:00:00+08:00", status: "executed", counts: {tool: 0, request: 0, event: 0}};
  const orphan = {decision_id: "archive-orphan", action: {type: "play_card"}, action_label: "已查明的工具归属决策",
    timestamp_utc: "2026-11-01T08:00:00+08:00", status: "executed"};
  const parent = new Element("div");
  parent.append(nodes.get("stream-list"));
  Object.assign(context, {URLSearchParams,
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      calls.push({url, options});
      const parsed = new URL(url, "http://localhost");
      const run = parsed.searchParams.get("run_id");
      if (run === "race-old" && parsed.pathname === "/api/history") {
        return new Promise(resolve => { delayedResponse = resolve; });
      }
      let body;
      if (run === "network" && !indexReady) body = {run_id: run, indexing: true, decisions: []};
      else if (parsed.pathname === "/api/history") {
        body = {run_id: run, indexing: false, decisions: run === "network" ? [parsed.searchParams.has("before") ? older : archived] : [],
          total_decisions: run === "network" ? 2 : 0, next_before: parsed.searchParams.has("before") ? null : 1,
          has_more: run === "network" && !parsed.searchParams.has("before"), through_seq: 20,
          ...(run === "network" ? {error: "历史读取测试提示", issue_count: 5,
            issues: [{file: "dashboard-events.jsonl", offset: 10}], pending_tail: ["transitions.jsonl"]} : {})};
      } else if (parsed.pathname === "/api/history-events") {
        body = {run_id: run, events: run === "network" ? [{id: "transition:session", type: "context_reset", timestamp_utc: at,
          payload: {message: "独立会话事件"}}] : [], total_events: 1, next_before: null, has_more: false, through_seq: 20};
      } else {
        const isNew = parsed.pathname.endsWith("archive-new");
        body = {run_id: run, decision: isNew ? archived : parsed.pathname.endsWith("archive-orphan") ? orphan : older,
          complete: true, through_seq: 20,
          model_requests: isNew ? [{model_request_id: "full-request", decision_id: archived.decision_id, started_utc: at,
            requested_model: "flash", status: "completed", reasoning: "全部历史推理", content: "完整历史回答", messages: [{role: "user", content: "原始输入"}]}] : [],
          tool_calls: isNew ? [{tool_call_id: "full-tool", decision_id: archived.decision_id, started_utc: at,
            name: "wiki_lookup", status: "completed", arguments: {entity_id: "x"}, result: {text: "全部结果"}, elapsed_ms: 60}] : [],
          events: isNew ? [{id: "transition:action", timestamp_utc: at, type: "action_completed", payload: {decision_id: archived.decision_id}}] : []};
      }
      return {ok: true, json: async () => body};
    },
  });
  const live = {run_id: "network", seq: 10, events: [], tool_calls: [{tool_call_id: "orphan-tool",
    decision_id: "archive-orphan", name: "wiki_search", status: "completed", started_utc: at}]};
  timeline.render(live);
  await settle();
  assert.equal(calls.length, 2);
  assert.match(nodes.get("stream-history-controls").children[0].textContent, /索引/);
  indexReady = true;
  for (const [id, callback] of [...timers]) { timers.delete(id); callback(); }
  await settle();
  assert.equal(calls.filter(call => call.url.includes("/api/history/archive-new?")).length, 0);
  let groups = nodes.get("stream-list").children;
  assert.equal(groups.some(group => group.dataset.groupId === "session"), false);
  assert.equal(groups.every(group => group.dataset.open === "false"), true);
  assert.equal(nodes.get("timeline-list").children.some(row => row.children[2]?.children[1]?.textContent === "独立会话事件"), true);
  let latest = groups.find(group => group.dataset.groupId === "decision:archive-new");
  latest.children[0].listeners.click();
  await settle();
  assert.equal(calls.filter(call => call.url.includes("/api/history/archive-new?")).length, 1);
  groups = nodes.get("stream-list").children;
  latest = groups.find(group => group.dataset.groupId === "decision:archive-new");
  assert.equal(latest.dataset.open, "true");
  const controls = nodes.get("stream-history-controls").children;
  assert.equal(controls[0].textContent, "历史读取测试提示");
  assert.equal(controls[0].role, "alert");
  assert.equal(controls.find(child => child.className === "history-issues").children[0].textContent, "日志异常 5 项（1 个位置示例）");
  assert.equal(controls.some(child => child.textContent.includes("未写完记录")), true);
  assert.equal(controls.some(child => child.textContent === "统计范围：当前已加载记录"), false);
  assert.equal(controls.some(child => child.textContent.includes("完整日志可逐页查看")), false);
  const rows = latest.children[1].children.find(child => child.className === "stream-rows");
  const request = rows.children.find(row => row.dataset.recordId === "request:full-request");
  request.children[3].children.find(child => child.className.includes("history-inspect-button")).listeners.click();
  groups = nodes.get("stream-list").children;
  const rawRows = groups.find(group => group.dataset.groupId === "decision:archive-new").children[1].children
    .find(child => child.className === "stream-rows");
  const raw = rawRows.children.find(child => child.className?.includes("history-raw-inspector")).children[0];
  assert.match(raw.textContent, /全部历史推理/);
  assert.match(raw.textContent, /完整历史回答/);
  assert.match(raw.textContent, /原始输入/);
  assert.equal(raw.style.maxHeight, "420px");
  const beforeIdle = calls.length;
  timeline.render({...live, seq: 11, events: [{seq: 11, type: "model_reasoning_delta", payload: {text: "next"}}]});
  await settle();
  assert.equal(calls.length, beforeIdle);
  assert.equal(timers.size, 0);
  const olderButton = nodes.get("stream-history-controls").children.find(child => child.textContent === "加载更早决策");
  olderButton.listeners.click();
  await settle();
  assert.match(nodes.get("stream-count").textContent, /2 \/ 2/);
  const oldGroup = nodes.get("stream-list").children.find(group => group.dataset.groupId === "decision:archive-old");
  assert.equal(oldGroup.dataset.open, "false");
  assert.equal(calls.filter(call => call.url.includes("/api/history/archive-old?")).length, 0);
  oldGroup.children[0].listeners.click();
  await settle();
  assert.equal(calls.filter(call => call.url.includes("/api/history/archive-old?")).length, 1);
  const unresolvedGroup = nodes.get("stream-list").children.find(group => group.dataset.groupId === "decision:archive-orphan");
  assert.equal(unresolvedGroup.dataset.status, "unresolved");
  unresolvedGroup.children[0].listeners.click();
  await settle();
  assert.equal(calls.filter(call => call.url.includes("/api/history/archive-orphan?")).length, 1);
  const resolved = nodes.get("stream-list").children.find(group => group.dataset.groupId === "decision:archive-orphan");
  assert.equal(resolved.dataset.status, "executed");
  assert.equal(resolved.children[0].children.find(child => child.className === "stream-action").textContent, "已查明的工具归属决策");
  assert.equal(calls.every(call => !call.options.method || call.options.method === "GET"), true);

  // A response from a previous run must not repopulate a newly selected run.
  timeline.render({run_id: "race-old", seq: 1});
  await settle();
  timeline.render({run_id: "race-new", seq: 1});
  await settle();
  delayedResponse({ok: true, json: async () => ({run_id: "race-old", decisions: [archived], total_decisions: 1, has_more: false})});
  await settle();
  assert.equal(nodes.get("stream-list").children.some(group => group.dataset.groupId === "decision:archive-new"), false);
  assert.match(nodes.get("stream-count").textContent, /0 \/ 0/);
  assert.equal(nodes.get("stream-history-controls").hidden, true);
  assert.equal(nodes.get("stream-history-controls").children.length, 0);
  console.log("Dashboard full history: indexing, GET-only pagination, lazy complete text, idle polling, and run races passed.");
}

testHistory().catch(error => { console.error(error); process.exitCode = 1; });

/** Verify bilingual display without changing control codes or stored game/model content. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
// A minimal DOM records actual production display values and control attributes.
const nodes = new Map();

/** Supply only the DOM operations used by the production display functions. */
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    textContent: "", value: "", style: {setProperty() {}}, dataset: {}, children: [],
    classList: {toggle() {}}, scrollTop: 0, scrollHeight: 0,
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
    setAttribute(name, value) { this[name] = value; },
    addEventListener() {}, querySelector(selector) { return node(`${String(id)}-${selector}`); },
    querySelectorAll() { return []; },
  });
  return nodes.get(id);
}

// Production scripts are evaluated without networking, timers, or game controls.
const location = {search: "", href: "http://127.0.0.1:18766/", reload() {}};
const storage = new Map();
const context = vm.createContext({console, AbortController, URLSearchParams,
  location, localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
  setTimeout() {}, clearTimeout() {}, setInterval() {}, NodeFilter: {SHOW_TEXT: 4},
  window: {location, addEventListener() {}}, document: {
    documentElement: node("root"), body: node("body"),
    getElementById: node, createElement: () => node(Symbol()), querySelectorAll: () => [],
    createTreeWalker: () => ({nextNode: () => null}), addEventListener() {},
  }});
const web = path.join(__dirname, "../src/sts2_agent/web");
vm.runInContext(fs.readFileSync(path.join(web, "i18n.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(web, "app.js"), "utf8").replace(/initialize\(\);\s*$/, "") +
  "\nglobalThis.ui = {state, renderControls, renderDecision, renderMetrics, renderSession, renderSimulation, actionLabel};", context);
const ui = context.ui;
const language = context.window.ObservatoryI18n;
const fresh = {run_id: "run-one", status: "paused", desired_state: "paused", connection: {connected: true},
  metrics: {requests_used: 0, actions_completed: 0}, model_requests: []};

// Labels are translated at render time, including dictionaries initialized before sync.
language.sync({language: "en", editable: true});
ui.state.snapshot = fresh;
ui.state.apiAvailable = true;
ui.renderSession(fresh);
ui.renderControls();
assert.equal(node("phase-label").textContent, "Paused");
assert.equal(node("run-toggle-span").textContent, "Start");
assert.equal(node("run-toggle").dataset.mode, "resume");
assert.equal(node("run-toggle").disabled, false);
assert.equal(ui.actionLabel({type: "end_turn"}), "End turn");
assert.equal(ui.actionLabel({type: "choose_event_option", option_index: 2}), "Choose event option · Option #2");
ui.renderMetrics({total_tokens: 1234});
assert.equal(node("total-tokens").textContent, "1,234");
assert.equal(node("completion-tokens").textContent, "Unknown");

// Live game names and stored explanations are copied verbatim in either language.
ui.state.snapshot = {...fresh, build_plan: "Improve draw", scene_plan: "Block the next attack",
  observation: {state: {combat: {hand: [{i: 0, name: "Strike"}]}}}};
const action = {type: "play_card", card_index: 0};
ui.renderDecision({decision_id: "decision-one", status: "proposed", action, reason: "Play Strike now."});
assert.equal(node("decision-action").textContent, "Play card · Strike");
assert.equal(node("decision-reason").textContent, "Play Strike now.");
assert.equal(node("decision-plan").textContent, "Improve draw");
assert.equal(node("scene-plan").textContent, "Block the next attack");
assert.deepEqual(action, {type: "play_card", card_index: 0});
ui.renderDecision({status: "executed", action, action_label: "旧记录中的卡牌", reason: "旧记录说明"});
assert.equal(node("decision-action").textContent, "旧记录中的卡牌");
assert.equal(node("decision-reason").textContent, "旧记录说明");

// English rendering retains the same recommendation order, values, and explicit failures.
ui.renderSimulation({status: "unavailable", diagnostics: [{code: "upstream_mirror_request_failed", error_kind: "ValueError"}]});
assert.equal(node("side-candidate-meta").textContent, "Simulation failed");
assert.match(node("bay-candidates").children[1].children[0].textContent, /upstream_mirror_request_failed.*ValueError/);
ui.renderSimulation({status: "advisory", simulation_advice: {policy_status: "ready", candidates: [
  {action: {type: "end_turn"}, probability: 0.7, selected: true},
  {action: {type: "end_turn"}, probability: 0.3, selected: false},
]}});
assert.equal(node("sim-candidate-count").textContent, "2");
assert.equal(node("bay-candidates").children[1].children[0].children[2].textContent, "70.0% · Selected");

// Restoring Chinese does not rewrite enum values or the persisted explanations.
language.sync({language: "zh-CN", editable: true});
ui.state.snapshot = fresh;
ui.renderControls();
assert.equal(node("run-toggle-span").textContent, "开始");
assert.equal(node("run-toggle").dataset.mode, "resume");
assert.equal(ui.actionLabel({type: "end_turn"}), "结束回合");
assert.equal(node("decision-reason").textContent, "旧记录说明");
console.log("Dashboard bilingual display and unchanged control codes verified.");

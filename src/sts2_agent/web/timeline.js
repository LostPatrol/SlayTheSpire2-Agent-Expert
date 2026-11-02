/** Render retained dashboard history using explicit backend identities and factual statuses. */
"use strict";

(() => {
  // Native action types determine the visual category, independently of execution status.
  const ACTIONS = {play_card: "打出卡牌", end_turn: "结束回合", use_potion: "使用药水", discard_potion: "丢弃药水",
    choose_map_node: "选择路线", claim_reward: "领取奖励", choose_reward_card: "选择奖励卡牌", skip_reward_cards: "跳过选牌",
    proceed: "继续前进", choose_event_option: "选择事件选项", choose_rest_option: "营火行动", select_deck_card: "选择卡牌",
    buy_card: "购买卡牌", buy_relic: "购买遗物", buy_potion: "购买药水", remove_card: "移除卡牌",
    choose_treasure_relic: "选择遗物", open_chest: "打开宝箱", confirm_selection: "确认选择", skip_selection: "跳过选择"};
  // Labels retain the difference between a proposal, submission, and native completion.
  const STATUS = {proposed: "提案", pending: "决策中", submitted: "已提交", executed: "已执行", completed: "已完成",
    discarded: "已失效", rejected: "未通过", failed: "失败", running: "进行中", generating: "生成中", session: "会话",
    unresolved: "待查历史", no_decision: "未形成决策", historical_proposal: "历史提案",
    cancelling: "正在停止输出", cancelled: "已取消"};
  // Existing v2 symbols and classes are shared by both layouts.
  const GLYPHS = {card: "card", route: "stairs", reward: "gift", rest: "flame", potion: "flask", other: "clock"};
  // Rows remain distinct even when a lifecycle event also updates a tool or request summary.
  const KINDS = {tool: "工具", request: "请求", event: "事件", decision: "决策"};
  // Native lifecycle evidence can advance a proposal before the history endpoint refreshes.
  const DECISION_EVENTS = {decision: "proposed", action_submitted: "submitted", action_completed: "executed",
    action_rejected: "rejected", decision_rejected: "rejected", decision_discarded: "discarded"};
  // Older proposal summaries must not undo an already observed submission or terminal outcome.
  const STATUS_ORDER = {pending: 0, proposed: 0, submitted: 1, executed: 2, rejected: 2, discarded: 2};
  // Readable titles describe emitted facts rather than predicting execution outcomes.
  const EVENTS = {action_completed: ["execution", "动作已完成"], action_submitted: ["execution", "动作已提交"],
    action_rejected: ["execution", "动作被拒绝"], decision: ["model", "决策已形成"],
    decision_discarded: ["model", "方案已失效"], decision_rejected: ["model", "决策未通过校验"],
    model_request_started: ["model", "模型请求开始"], model_request_completed: ["model", "模型请求完成"],
    model_request_failed: ["model", "模型请求失败"], model_request_cancelled: ["model", "模型请求已取消"],
    tool_call_started: ["tool", "工具开始"],
    tool_call_completed: ["tool", "工具完成"], tool_call_failed: ["tool", "工具失败"],
    simulation: ["calculator", "推演更新"], calculation: ["calculator", "计算结果"],
    game_over: ["execution", "游戏结束"], budget_stop: ["observe", "预算已用尽"],
    observation: ["observe", "公开观测更新"], instructions_injected: ["observe", "人工指令已注入"]};
  // History indexing reports progress without blocking the live snapshot or control UI.
  const INDEX_RETRY_MS = 1000;
  // Coalesce lifecycle changes; token deltas never trigger history scans.
  const HISTORY_REFRESH_MS = 3000;
  // Load metadata in small pages while complete group contents remain lazy.
  const HISTORY_PAGE_SIZE = 30;
  // Bound browser line layout while keeping every character of large records accessible.
  const INSPECTOR_PAGE_CHARACTERS = 12000;
  // Give the browser time to begin a local Blob download before releasing its URL.
  const DOWNLOAD_RELEASE_MS = 1000;
  // View preferences are scoped to a run and survive snapshot refreshes and reconnects.
  const view = {runId: null, signature: "", expanded: new Map(), tools: new Map(), inspectors: new Map(), inspectorPages: new Map(), onExpand: null,
    snapshot: null, data: null, generation: 0, summaries: new Map(), details: new Map(), detailStatus: new Map(), watermarks: new Map(),
    sessionEvents: [], sessionLoaded: false, nextSession: null, moreSession: false, totalSession: null,
    historyLoaded: false, nextBefore: null, moreHistory: false, totalDecisions: null, historyBusy: false,
    sessionBusy: false, historyError: "", sessionError: "", indexing: false, refreshKey: "", timer: null, lastRead: 0,
    sessionWatermark: 0, issues: [], issueCount: 0, pendingTail: [], archiveError: ""};

  /** Convert public values to text without interpreting markup. */
  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  }

  /** Keep measured zero values while leaving missing durations unknown. */
  function elapsed(value) { return Number.isFinite(value) ? `${(value / 1000).toFixed(2)}s` : "—"; }

  /** Parse only actual backend timestamps; absent timestamps remain unsorted facts. */
  function timestamp(value) { const result = Date.parse(value); return Number.isFinite(result) ? result : 0; }

  /** Show local clock time while retaining the original ISO timestamp on the element. */
  function clock(value) { return timestamp(value) ? new Date(value).toLocaleTimeString("zh-CN", {hour12: false}) : "—"; }

  /** Create a safe element with optional visible text. */
  function el(tag, className = "", value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = text(value);
    return node;
  }

  /** Construct an existing local SVG symbol reference. */
  function icon(name, className = "ico") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    node.setAttribute("class", className);
    node.setAttribute("aria-hidden", "true");
    use.setAttribute("href", `#i-${name}`);
    node.append(use);
    return node;
  }

  /** Preserve backend identity on machine-readable timestamps. */
  function timeNode(value, className = "") {
    const node = el("time", className, clock(value));
    node.dateTime = value || "";
    node.title = text(value);
    return node;
  }

  /** Paint lifecycle status without deriving it from time or neighboring actions. */
  function seal(status) {
    const tone = ["executed", "completed"].includes(status) ? "verd"
      : ["discarded", "rejected", "failed"].includes(status) ? "blood" : "brass";
    return el("span", `seal seal-${tone}`, STATUS[status] || status);
  }

  /** Read the recorded action label without resolving old indices against current state. */
  function actionText(record) {
    if (record.action_label && record.action_label !== record.action?.type) return text(record.action_label);
    const action = record.action;
    if (!action) return "未形成完整决策";
    const label = ACTIONS[action.type] || text(action.type, "动作");
    if (action.card_index !== undefined) return `${label} · 手牌 #${action.card_index}`;
    if (action.option_index !== undefined) return `${label} · 选项 #${action.option_index}`;
    return label;
  }

  /** Map real native action types onto the approved v2 visual categories. */
  function actionType(action) {
    const kind = action?.type || "";
    if (kind === "play_card") return "card";
    if (kind.includes("potion")) return "potion";
    if (kind === "choose_map_node") return "route";
    if (kind.includes("reward") || kind.includes("treasure") || kind === "open_chest") return "reward";
    return kind === "choose_rest_option" ? "rest" : "other";
  }

  /** Keep the complete returned result available separately from a short factual summary. */
  function toolSummary(call) {
    const result = call.result;
    if (call.error) return text(call.error);
    if (result === undefined) return call.status === "running" ? "正在执行" : "尚无返回";
    if (typeof result === "string") return result;
    if (result?.summary || result?.message || result?.error) return text(result.summary || result.message || result.error);
    const entries = result?.entries || result?.results || result?.events || result?.matches || result?.candidates;
    return Array.isArray(entries) ? `返回 ${entries.length} 条记录` : text(result?.status, "已返回结果");
  }

  /** Classify explicit events while omitting token deltas from the human timeline. */
  function eventRow(event) {
    const type = text(event.type, "event");
    if (/_delta$/.test(type)) return null;
    const payload = event.payload || {};
    const [category, name] = EVENTS[type] || [/simulation|calculat/.test(type) ? "calculator"
      : /model|request|decision/.test(type) ? "model" : /tool/.test(type) ? "tool"
      : /action|receipt/.test(type) ? "execution" : "observe", type.replaceAll("_", " ")];
    const meta = [payload.summary ?? payload.message ?? payload.error ?? payload.reason,
      payload.status, payload.command].filter(value => value !== undefined && value !== null).map(value => text(value)).join(" · ");
    return {key: `event:${event.id || event.seq}`, kind: "event", category, name, meta,
      at: event.timestamp_utc, seq: event.seq, decisionId: payload.decision_id || "", actionId: payload.action_id || "",
      status: type === "model_request_cancelled" ? "cancelled" : /failed|rejected|discarded|error/.test(type) ? "failed" : text(payload.status, "recorded"),
      tail: event.seq === undefined ? "" : `#${event.seq}`, source: event};
  }

  /** Merge retained records by their own IDs; positions never imply causal identity. */
  function unique(records, field) {
    const items = new Map();
    records.forEach((record, index) => {
      const key = record[field] || `unidentified:${index}`;
      items.set(key, {...items.get(key), ...record});
    });
    return [...items.values()];
  }

  /** Recover action identity from live facts, then prefer the current snapshot's action fields. */
  function decisionRecords(snapshot, events, actionOwners) {
    const records = new Map();
    /** Merge one identified record without regressing its observed lifecycle. */
    function add(record, fallback) {
      const key = record.decision_id || fallback;
      const previous = records.get(key) || {};
      const merged = {...previous, ...record};
      if ((STATUS_ORDER[previous.status] ?? -1) > (STATUS_ORDER[record.status] ?? -1)) merged.status = previous.status;
      records.set(key, merged);
    }
    events.slice().sort((a, b) => timestamp(a.timestamp_utc) - timestamp(b.timestamp_utc)
      || (a.seq || 0) - (b.seq || 0)).forEach(event => {
      if (!Object.hasOwn(DECISION_EVENTS, event.type)) return;
      const payload = event.payload?.decision || event.payload || {};
      const id = payload.decision_id || actionOwners.get(payload.action_id);
      if (!id) return;
      const previous = records.get(id);
      const record = {decision_id: id, status: DECISION_EVENTS[event.type],
        timestamp_utc: previous?.timestamp_utc || event.timestamp_utc};
      for (const field of ["action", "action_label", "reason", "plan", "build_plan", "scene_plan", "decision_source",
        "decision_epoch", "decision_episode_id"]) if (Object.hasOwn(payload, field)) record[field] = payload[field];
      add(record, id);
    });
    (snapshot.decision_history || []).forEach((record, index) => add(record, `legacy-record:${index}`));
    if (snapshot.decision?.action) add(snapshot.decision, "current-decision");
    return [...records.values()];
  }

  /** Build retained decision chains without inventing missing requests, tools, or scores. */
  function buildGroups(snapshot) {
    const events = unique((snapshot.events || []).filter(event => !event.payload?.run_id || event.payload.run_id === snapshot.run_id)
      .map(event => ({...event, id: event.id || `dashboard:${event.seq}`})), "id");
    const tools = unique(snapshot.tool_calls || [], "tool_call_id");
    const requests = unique(snapshot.model_requests || [], "model_request_id");
    const groups = new Map();
    const actionOwners = new Map();
    // Some receipt events carry only action identity; use another explicit event to resolve it.
    events.forEach(event => {
      if (event.payload?.action_id && event.payload?.decision_id) actionOwners.set(event.payload.action_id, event.payload.decision_id);
    });
    const decisions = decisionRecords(snapshot, events, actionOwners);
    /** Create a stable decision bucket or an explicit unassociated session bucket. */
    function groupFor(id) {
      const key = id ? `decision:${id}` : "session";
      if (!groups.has(key)) groups.set(key, {key, decisionId: id, record: null, name: id ? "等待完整决策" : "会话事件",
        status: id ? "pending" : "session", type: "other", at: "", reason: "", plan: "", children: []});
      return groups.get(key);
    }
    decisions.forEach((record, index) => {
      // A legacy record without identity gets its own group but cannot acquire unrelated children.
      const group = groupFor(record.decision_id || `legacy-record:${index}`);
      Object.assign(group, {record, name: actionText(record), status: record.status || "proposed", type: actionType(record.action),
        at: record.timestamp_utc || record.completed_utc || "", reason: text(record.reason, ""),
        plan: text(record.build_plan ?? record.plan, "")});
    });
    const rows = [
      ...tools.map((call, index) => ({key: `tool:${call.tool_call_id || index}`, kind: "tool", category: "tool",
        name: text(call.name || call.tool_name, "工具"), meta: toolSummary(call), at: call.started_utc || "",
        tail: elapsed(call.elapsed_ms), status: call.status || "running", decisionId: call.decision_id || "", source: call})),
      ...requests.map((request, index) => ({key: `request:${request.model_request_id || index}`, kind: "request", category: "model",
        name: text(request.actual_model || request.model || request.requested_model, "模型请求"),
        meta: `${STATUS[request.status] || text(request.status)} · ${text(request.model_request_id)}`,
        at: request.started_utc || request.timestamp_utc || "", tail: elapsed(request.duration_ms),
        status: request.status || "generating", decisionId: request.decision_id || "", source: request})),
      ...events.map(eventRow).filter(Boolean),
    ];
    rows.forEach(row => {
      const id = row.decisionId || actionOwners.get(row.actionId) || "";
      const group = groupFor(id);
      group.children.push(row);
      if (!group.at || (!group.record && timestamp(row.at) > timestamp(group.at))) group.at = row.at;
    });
    const result = [...groups.values()];
    result.forEach(group => {
      group.children.sort((a, b) => timestamp(a.at) - timestamp(b.at) || (a.seq || 0) - (b.seq || 0) || a.key.localeCompare(b.key));
      group.counts = {tool: 0, request: 0, event: 0};
      group.children.forEach(row => { group.counts[row.kind] += 1; });
      group.countsComplete = Boolean(group.record?._history_counts || group.record?._history_complete);
      if (group.record?._history_counts) Object.keys(group.counts).forEach(kind => {
        group.counts[kind] = Math.max(group.counts[kind], Number(group.record._history_counts[kind]) || 0);
      });
      if (group.decisionId && (!group.record || group.status === "pending")) {
        const active = (snapshot.request_pending || snapshot.decision_pending) && group.children.some(row =>
          row.kind === "request" && ["generating", "running", "streaming", "pending"].includes(row.status));
        const latestRequest = group.children.findLast(row => row.kind === "request");
        const cancelling = latestRequest?.status === "cancelling" || (active && snapshot.generation_cancelling);
        const cancelled = latestRequest?.status === "cancelled" || (!latestRequest && group.children.some(row => row.status === "cancelled"));
        group.status = cancelling ? "cancelling" : active ? "pending" : cancelled ? "cancelled"
          : group.record?._history_complete ? "no_decision" : "unresolved";
        if (!group.record || group.status === "cancelling" || group.status === "cancelled") group.name = cancelling ? "正在停止模型输出"
          : cancelled && !active ? "本轮生成已取消" : active ? "正在形成决策" : "历史决策详情待载入";
      }
    });
    result.sort((a, b) => timestamp(b.at) - timestamp(a.at) || a.key.localeCompare(b.key));
    return {groups: result, decisions, tools, events: rows.filter(row => row.kind === "event"), rows,
      counts: result.reduce((counts, group) => {
        Object.keys(counts).forEach(kind => { counts[kind] += group.counts[kind]; });
        return counts;
      }, {tool: 0, request: 0, event: 0})};
  }

  /** Update optional shell counts without requiring split-only panels. */
  function setText(id, value) { const node = document.getElementById(id); if (node) node.textContent = text(value); }

  /** Page complete raw text so a multi-megabyte record never becomes one layout operation. */
  function rawInspector(value, key) {
    const full = text(value);
    const pages = Math.max(1, Math.ceil(full.length / INSPECTOR_PAGE_CHARACTERS));
    const inspector = el("div", "tool-detail history-raw-inspector");
    const content = el("pre", "json-block");
    content.style.maxHeight = "420px";
    content.style.overflow = "auto";
    const controls = el("div", "history-detail-pages");
    const previous = el("button", "link-button", "上一页");
    const next = el("button", "link-button", "下一页");
    const download = el("button", "link-button", "下载完整 JSON");
    const label = el("span");
    previous.type = next.type = download.type = "button";
    controls.append(previous, label, next, download);
    controls.hidden = pages === 1;
    /** Keep UTF-16 surrogate pairs together at adjacent page boundaries. */
    function offset(page) {
      const position = Math.min(page * INSPECTOR_PAGE_CHARACTERS, full.length);
      return /[\uD800-\uDBFF]/.test(full.charAt(position - 1))
        && /[\uDC00-\uDFFF]/.test(full.charAt(position)) ? position - 1 : position;
    }
    /** Replace only this inspector's bounded text, preserving the surrounding timeline. */
    function showPage(page) {
      page = Math.max(0, Math.min(page, pages - 1));
      view.inspectorPages.set(key, page);
      content.textContent = full.slice(offset(page), offset(page + 1));
      content.scrollTop = 0;
      label.textContent = `${page + 1} / ${pages}`;
      previous.disabled = page === 0;
      next.disabled = page === pages - 1;
    }
    previous.addEventListener("click", () => showPage((view.inspectorPages.get(key) || 0) - 1));
    next.addEventListener("click", () => showPage((view.inspectorPages.get(key) || 0) + 1));
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([full], {type: "application/json;charset=utf-8"}));
      const link = el("a");
      link.href = url;
      link.download = `${key.replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_RELEASE_MS);
    });
    inspector.append(content, controls);
    showPage(view.inspectorPages.get(key) || 0);
    return inspector;
  }

  /** Render a real child record and update its inspector without redrawing other rows. */
  function appendStreamRow(container, row) {
    const node = el("div", "stream-row");
    node.dataset.kind = row.kind;
    node.dataset.category = row.category;
    node.dataset.status = row.status;
    node.dataset.recordId = row.key;
    const name = el("b", "stream-row-name", row.name);
    const meta = el("span", "stream-row-meta");
    meta.append(el("span", "history-row-summary", row.meta || ""));
    name.title = row.name;
    meta.title = row.meta || "";
    let inspector = null;
    if (row.source) {
      const inspect = el("button", "link-button history-inspect-button", "详情");
      inspect.type = "button";
      inspect.setAttribute("aria-expanded", String(view.inspectors.get(row.key) === true));
      inspect.addEventListener("click", () => {
        const open = view.inspectors.get(row.key) !== true;
        view.inspectors.set(row.key, open);
        inspect.setAttribute("aria-expanded", String(open));
        if (open) {
          inspector = rawInspector(row.source, row.key);
          node.after(inspector);
        } else {
          inspector?.remove();
          inspector = null;
        }
      });
      meta.append(inspect);
    }
    node.append(timeNode(row.at, "stream-row-time"), el("span", "stream-row-kind", KINDS[row.kind]), name, meta,
      el("em", "stream-row-tail", row.tail));
    container.append(node);
    if (view.inspectors.get(row.key) && row.source) {
      inspector = rawInspector(row.source, row.key);
      container.append(inspector);
    }
  }

  /** Render stream cards with stable expansion state across real snapshot updates. */
  function renderStream(data) {
    const list = document.getElementById("stream-list");
    if (!list) return;
    // The list is rebuilt on every live update; keep the reader's scroll offset
    // inside an open raw inspector instead of snapping it back to the top.
    const inspectorScroll = new Map();
    list.querySelectorAll(".history-raw-inspector .json-block").forEach(content => {
      const row = content.closest(".history-raw-inspector")?.previousElementSibling;
      if (row?.dataset.recordId) inspectorScroll.set(row.dataset.recordId, content.scrollTop);
    });
    const groups = data.groups.filter(group => group.key !== "session");
    list.replaceChildren(...groups.map(group => {
      if (!view.expanded.has(group.key)) view.expanded.set(group.key, false);
      const item = el("li", "stream-group");
      item.dataset.open = String(view.expanded.get(group.key));
      item.dataset.status = group.status;
      item.dataset.type = group.type;
      item.dataset.groupId = group.key;
      const head = el("button", "stream-group-head");
      head.type = "button";
      head.title = group.decisionId || "未关联到具体决策的会话事件";
      head.setAttribute("aria-expanded", item.dataset.open);
      const glyph = el("span", "stream-glyph");
      glyph.append(icon(GLYPHS[group.type]));
      const counts = group.counts;
      const countText = `${counts.tool} 工具 · ${counts.request} 请求 · ${counts.event} 事件`;
      head.append(timeNode(group.at, "stream-time"), glyph, seal(group.status), el("b", "stream-action", group.name),
        el("span", "stream-excerpt", group.reason),
        el("span", "stream-counts", group.countsComplete ? countText
          : group.children.length ? `已载入 ${countText}` : "详情待载入"),
        el("span", "stream-score", ""), icon("chevron", "stream-chevron"));
      const detail = el("div", "stream-detail");
      if (group.reason) detail.append(el("p", "stream-reason", group.reason));
      if (group.plan) detail.append(el("p", "stream-plan", `当时的计划：${group.plan}`));
      const rows = el("div", "stream-rows");
      group.children.forEach(row => appendStreamRow(rows, row));
      const loading = view.detailStatus.get(group.decisionId);
      if (["loading", "indexing"].includes(loading)) rows.append(el("p", "empty-note", loading === "indexing"
        ? "正在建立完整历史索引…" : "正在读取完整历史详情…"));
      else if (loading?.startsWith("error:")) {
        rows.append(el("p", "empty-note", loading.slice(6)));
        const retry = el("button", "link-button", "重试读取详情");
        retry.type = "button";
        retry.addEventListener("click", () => loadDetail(group.decisionId, true));
        rows.append(retry);
      } else if (!group.children.length) rows.append(el("p", "empty-note", group.record && !view.details.has(group.decisionId)
        ? "展开后读取完整请求、工具和事件。" : "日志中没有可关联的请求、工具或事件。"));
      detail.append(rows);
      item.append(head, detail);
      head.addEventListener("click", () => {
        const open = item.dataset.open !== "true";
        view.expanded.set(group.key, open);
        item.dataset.open = String(open);
        head.setAttribute("aria-expanded", String(open));
        if (open && view.onExpand) view.onExpand(group.decisionId || null);
        if (open && group.decisionId) loadDetail(group.decisionId);
      });
      return item;
    }));
    if (!groups.length) list.append(el("li", "empty-note", "尚无决策记录。"));
    if (inspectorScroll.size) {
      list.querySelectorAll(".history-raw-inspector .json-block").forEach(content => {
        const row = content.closest(".history-raw-inspector")?.previousElementSibling;
        const offset = row?.dataset.recordId ? inspectorScroll.get(row.dataset.recordId) : 0;
        if (offset) content.scrollTop = offset;
      });
    }
    setText("stream-count", `已加载 ${data.decisions.length}${Number.isFinite(view.totalDecisions) ? ` / ${view.totalDecisions}` : ""} 条决策记录`);
    for (const kind of ["tool", "request", "event"]) {
      const count = groups.reduce((total, group) => total + group.counts[kind], 0);
      setText(`stream-${kind === "request" ? "requests" : `${kind}s`}`, count);
    }
    const measuredTools = groups.flatMap(group => group.children).filter(row => row.kind === "tool"
      && ["completed", "failed", "error"].includes(row.status) && Number.isFinite(row.source?.elapsed_ms));
    setText("stream-average", measuredTools.length
      ? elapsed(measuredTools.reduce((sum, row) => sum + row.source.elapsed_ms, 0) / measuredTools.length) : "—");
  }

  /** Render split history from the same records without repeating the current decision. */
  function renderHistory(data, snapshot) {
    const list = document.getElementById("history-list");
    if (!list) return;
    const records = data.groups.filter(group => group.record && group.record.decision_id !== snapshot.decision?.decision_id);
    list.replaceChildren(...records.map(group => {
      const item = el("li", "log-item");
      item.dataset.status = group.status;
      const body = el("div", "log-body");
      const head = el("div", "log-head");
      head.append(seal(group.status), el("h3", "", group.name));
      body.append(head, el("p", "", group.reason || "未提供说明"));
      if (group.plan) body.append(el("p", "history-plan", `当时的计划：${group.plan}`));
      item.append(timeNode(group.at, "log-time"), body);
      return item;
    }));
    if (!records.length) list.append(el("li", "empty-note", "暂无此前决策。"));
    setText("history-count", `此前 ${records.length} 条 · 当前决策见上方`);
  }

  /** Keep each tool's full arguments and returned payload inspectable in split mode. */
  function renderTools(data) {
    const list = document.getElementById("tools-list");
    if (!list) return;
    const rows = data.rows.filter(row => row.kind === "tool").sort((a, b) => timestamp(b.at) - timestamp(a.at));
    list.replaceChildren(...rows.map(row => {
      const card = el("article", "tool");
      const head = el("button", "tool-head");
      const opened = view.tools.get(row.key) === true;
      card.dataset.open = String(opened);
      head.type = "button";
      head.setAttribute("aria-expanded", String(opened));
      const right = el("span", "tool-right");
      right.append(el("span", "tool-time", row.tail), el("span", `tool-state ${row.status}`, STATUS[row.status] || row.status), icon("chevron", "ico tool-chevron"));
      head.append(icon("tool", "ico tool-icon"), el("span", "tool-name", row.name), el("span", "tool-summary", row.meta), right);
      const detail = el("div", "tool-detail");
      detail.hidden = !opened;
      /** Serialize split-view payloads only after an explicit expansion. */
      function fillDetail() {
        detail.append(el("span", "tool-label", "参数"), rawInspector(row.source.arguments, `${row.key}:arguments`),
          el("span", "tool-label", "返回值"), rawInspector(row.source.result ?? row.source.error ?? "尚无返回", `${row.key}:result`));
      }
      if (opened) fillDetail();
      head.addEventListener("click", () => {
        const open = card.dataset.open !== "true";
        view.tools.set(row.key, open);
        card.dataset.open = String(open);
        head.setAttribute("aria-expanded", String(open));
        detail.hidden = !open;
        if (open && !detail.children.length) fillDetail();
      });
      card.append(head, detail);
      return card;
    }));
    if (!rows.length) list.append(el("p", "empty-note", "暂无工具调用。"));
    setText("tool-count", `${rows.length} 次已载入`);
    const finished = data.tools.filter(call => ["completed", "failed", "error"].includes(call.status));
    const measured = finished.filter(call => Number.isFinite(call.elapsed_ms));
    const average = measured.length ? elapsed(measured.reduce((sum, call) => sum + call.elapsed_ms, 0) / measured.length) : "—";
    setText("tool-average", average);
    setText("tool-success-rate", finished.length ? `${(finished.filter(call => call.status === "completed").length / finished.length * 100).toFixed(1)}%` : "—");
  }

  /** Reuse factual event rows for the split program timeline and optional pipeline. */
  function renderEvents(data) {
    const list = document.getElementById("timeline-list");
    const events = data.events.slice().sort((a, b) => (b.seq || 0) - (a.seq || 0));
    if (list) {
      list.replaceChildren(...events.map(event => {
        const row = el("div", "timeline-row");
        row.dataset.category = event.category;
        const body = el("div");
        body.append(el("span", "timeline-title", event.name), el("span", "timeline-summary", event.meta));
        row.append(timeNode(event.at), el("i", `dot ${event.category}`), body);
        return row;
      }));
      if (!events.length) list.append(el("p", "empty-note", "暂无程序事件。"));
      if (view.moreSession) {
        const more = el("button", "link-button", view.sessionBusy ? "读取中…" : "加载更早的会话事件");
        more.type = "button";
        more.disabled = view.sessionBusy;
        more.addEventListener("click", () => loadSession(true));
        list.append(more);
      }
    }
    const pipeline = document.getElementById("pipeline-stream");
    if (pipeline) {
      const rows = [...data.rows, ...data.groups.filter(group => group.record).map(group => ({kind: "decision", at: group.at,
        name: group.name, meta: group.reason, status: group.status, category: "model"}))].sort((a, b) => timestamp(b.at) - timestamp(a.at));
      pipeline.replaceChildren(...rows.map(row => {
        const node = el("article", "pipe-row");
        node.dataset.kind = row.kind;
        node.dataset.status = row.status;
        node.dataset.category = row.category;
        const axis = el("span", "pipe-axis");
        axis.append(el("i"));
        const content = el("div", "pipe-content");
        const head = el("div", "pipe-head");
        head.append(el("span", "pipe-type", KINDS[row.kind]), el("b", "pipe-title", row.name),
          el("span", "pipe-meta", STATUS[row.status] || row.status));
        content.append(head, el("p", "pipe-detail", row.meta));
        node.append(timeNode(row.at, "pipe-time"), axis, content);
        return node;
      }));
    }
    setText("pipeline-tool-count", data.counts.tool);
    setText("pipeline-decision-count", data.decisions.length);
    setText("pipeline-event-count", data.counts.event);
    setText("timeline-count", `${events.length} 条已载入事件`);
    setText("event-count", `${events.length} 条已载入`);
    ["observe", "model", "calculator", "execution", "tool"].forEach(category => {
      const count = events.filter(event => event.category === category).length;
      setText(`event-stat-${category}`, events.length ? count : "—");
      const bar = document.getElementById(`event-bar-${category}`);
      if (bar) bar.style.width = `${events.length ? count / events.length * 100 : 0}%`;
    });
  }

  /** Merge paged archives with newer live facts while preserving complete historical text. */
  function combinedSnapshot() {
    const snapshot = view.snapshot || {};
    const requests = [];
    const tools = [];
    const events = [...view.sessionEvents];
    const records = new Map();
    view.details.forEach((detail, id) => {
      if (detail.decision) records.set(id, {...detail.decision, _history_complete: detail.complete === true});
    });
    view.summaries.forEach((record, id) => records.set(id, {...records.get(id), ...record,
      _history_counts: record.counts, _history_complete: view.details.get(id)?.complete === true}));
    const decisions = [...records.values()];
    view.details.forEach(detail => {
      requests.push(...(detail.model_requests || []));
      tools.push(...(detail.tool_calls || []));
      events.push(...(detail.events || []));
    });
    // Canonical archive events include raw receipts absent from the compact live window.
    (snapshot.events || []).forEach(event => {
      const id = event.payload?.decision_id;
      const covered = id ? view.watermarks.get(id) : view.sessionLoaded ? view.sessionWatermark : undefined;
      if (covered === undefined || event.seq > covered) events.push(event);
    });
    return {...snapshot, decision_history: unique([...decisions, ...(snapshot.decision_history || [])], "decision_id"),
      model_requests: unique([...requests, ...(snapshot.model_requests || [])], "model_request_id"),
      tool_calls: unique([...tools, ...(snapshot.tool_calls || [])], "tool_call_id"), events};
  }

  /** Add metadata pagination next to the existing stream list without changing its layout shell. */
  function renderHistoryControls() {
    const list = document.getElementById("stream-list");
    if (!list?.parentElement) return;
    let controls = document.getElementById("stream-history-controls");
    if (!controls) {
      controls = el("div", "stream-meta");
      controls.id = "stream-history-controls";
      list.parentElement.insertBefore(controls, list);
    }
    const status = view.historyError || view.sessionError || view.archiveError || (view.indexing ? "正在建立完整历史索引…"
      : view.historyBusy ? "正在读取历史…" : view.historyLoaded ? "" : "正在连接历史档案…");
    controls.replaceChildren();
    if (status) {
      const label = el("span", "", status);
      if (view.historyError || view.sessionError || view.archiveError) label.setAttribute("role", "alert");
      controls.append(label);
    }
    if (view.issueCount || view.issues.length) {
      const issues = el("details", "history-issues");
      issues.append(el("summary", "", `日志异常 ${view.issueCount || view.issues.length} 项（${view.issues.length} 个位置示例）`),
        el("pre", "json-block", text(view.issues)));
      controls.append(issues);
    }
    if (view.pendingTail.length) {
      const pending = el("span", "", `日志末尾有未写完记录（${view.pendingTail.length} 个文件），写完后继续载入。`);
      pending.title = view.pendingTail.join(" · ");
      controls.append(pending);
    }
    if (view.moreHistory) {
      const more = el("button", "link-button", "加载更早决策");
      more.type = "button";
      more.disabled = view.historyBusy;
      more.addEventListener("click", () => loadHistory(true));
      controls.append(more);
    }
    if (view.historyError || view.sessionError || view.archiveError) {
      const retry = el("button", "link-button", "重试历史读取");
      retry.type = "button";
      retry.addEventListener("click", () => { loadHistory(); loadSession(); });
      controls.append(retry);
    }
    controls.hidden = controls.children.length === 0;
  }

  /** Paint loaded content without coupling live controls to historical I/O. */
  function repaint() {
    if (!view.snapshot) return;
    const snapshot = combinedSnapshot();
    const data = buildGroups(snapshot);
    view.data = data;
    // The dedicated live-output drawer handles token text; unchanged rows keep their DOM identity.
    // Raw payloads can contain megabytes of tool results; only visible facts drive repainting.
    const signature = JSON.stringify({groups: data.groups.map(group => [group.key, group.status, group.type,
      group.name, group.at, group.reason, group.plan, group.counts, group.countsComplete]),
      rows: data.rows.map(row => [row.key, row.kind, row.category, row.status, row.at, row.name, row.meta, row.tail]),
      detailStatus: [...view.detailStatus], moreSession: view.moreSession, sessionBusy: view.sessionBusy,
      total: view.totalDecisions, current: snapshot.decision?.decision_id});
    renderHistoryControls();
    if (signature === view.signature) return;
    view.signature = signature;
    renderStream(data);
    renderHistory(data, snapshot);
    renderTools(data);
    renderEvents(data);
    data.groups.forEach(group => {
      if (!view.indexing && group.record && view.expanded.get(group.key) && view.summaries.has(group.decisionId)
          && !view.detailStatus.has(group.decisionId)) loadDetail(group.decisionId);
    });
  }

  /** Read only same-origin history APIs and expose failures beside historical content. */
  async function readHistory(path, query = {}) {
    const params = new URLSearchParams({run_id: view.runId, ...query});
    const response = await fetch(`${path}?${params}`, {headers: {Accept: "application/json"}, cache: "no-store"});
    if (!response.ok) {
      let message = `历史读取失败（HTTP ${response.status}）`;
      try { const body = await response.json(); message = text(body.detail || body.error, message); } catch { /* Keep the HTTP status. */ }
      throw new Error(message);
    }
    return response.json();
  }

  /** Preserve archive health separately from transport failures and sampled diagnostic locations. */
  function readMetadata(page) {
    if (Object.hasOwn(page, "issues")) view.issues = page.issues || [];
    if (Object.hasOwn(page, "issue_count")) view.issueCount = page.issue_count || 0;
    if (Object.hasOwn(page, "pending_tail")) view.pendingTail = page.pending_tail || [];
    if (Object.hasOwn(page, "error")) view.archiveError = text(page.error, "");
  }

  /** Poll an explicitly building index without retrying unrelated failures indefinitely. */
  function retryIndex() {
    if (view.timer !== null || typeof setTimeout !== "function") return;
    view.timer = setTimeout(() => {
      view.timer = null;
      loadHistory();
      loadSession();
    }, INDEX_RETRY_MS);
  }

  /** Load one summary page; newer pages update known statuses without discarding older pages. */
  async function loadHistory(older = false) {
    if (typeof fetch !== "function" || view.historyBusy || !view.runId) return;
    const generation = view.generation;
    view.historyBusy = true;
    view.historyError = "";
    view.lastRead = Date.now();
    repaint();
    try {
      const page = await readHistory("/api/history", {limit: HISTORY_PAGE_SIZE,
        ...(older && view.nextBefore !== null ? {before: view.nextBefore} : {})});
      if (generation !== view.generation || page.run_id !== view.runId) return;
      readMetadata(page);
      view.indexing = Boolean(page.indexing);
      if (page.indexing) { retryIndex(); return; }
      view.detailStatus.forEach((status, id) => { if (status === "indexing") view.detailStatus.delete(id); });
      const knownNewest = Math.max(0, ...[...view.summaries.values()].map(record => record.history_cursor || 0));
      const incomingOldest = Math.min(...page.decisions.map(record => record.history_cursor || 0));
      // A suspended browser may miss several pages; resume from the new gap before older pages.
      const gap = !older && knownNewest > 0 && incomingOldest > knownNewest;
      page.decisions.forEach(record => {
        const previous = view.summaries.get(record.decision_id);
        view.summaries.set(record.decision_id, record);
        if (previous && JSON.stringify([previous.status, previous.counts]) !== JSON.stringify([record.status, record.counts])
            && view.detailStatus.get(record.decision_id) === "loaded") view.detailStatus.delete(record.decision_id);
      });
      if (older || !view.historyLoaded || gap) {
        view.nextBefore = page.next_before ?? null;
        view.moreHistory = Boolean(page.has_more);
      }
      view.totalDecisions = page.total_decisions;
      if (view.summaries.size >= page.total_decisions) view.moreHistory = false;
      view.historyLoaded = true;
    } catch (error) {
      if (generation === view.generation) view.historyError = error.message;
    } finally {
      if (generation === view.generation) { view.historyBusy = false; repaint(); }
    }
  }

  /** Load unassociated events separately so no neighboring decision acquires them by inference. */
  async function loadSession(older = false) {
    if (typeof fetch !== "function" || view.sessionBusy || !view.runId) return;
    const generation = view.generation;
    const startingSeq = view.snapshot?.seq || 0;
    view.sessionBusy = true;
    view.sessionError = "";
    repaint();
    try {
      const page = await readHistory("/api/history-events", {limit: 100,
        ...(older && view.nextSession !== null ? {before: view.nextSession} : {})});
      if (generation !== view.generation || page.run_id !== view.runId) return;
      readMetadata(page);
      if (page.indexing) { view.indexing = true; retryIndex(); return; }
      view.sessionEvents = unique([...view.sessionEvents, ...(page.events || [])], "id");
      if (older || !view.sessionLoaded || (!older && page.total_events - (view.totalSession || 0) > 100)) {
        view.nextSession = page.next_before ?? null;
        view.moreSession = Boolean(page.has_more);
      }
      view.sessionWatermark = page.through_seq ?? startingSeq;
      view.totalSession = page.total_events;
      view.sessionLoaded = true;
    } catch (error) {
      if (generation === view.generation) view.sessionError = error.message;
    } finally {
      if (generation === view.generation) { view.sessionBusy = false; repaint(); }
    }
  }

  /** Resolve a complete decision chain only when its group is opened. */
  async function loadDetail(id, retry = false) {
    if (typeof fetch !== "function" || !id || id.startsWith("legacy-record:")) return;
    if (!retry && view.detailStatus.has(id)) return;
    const generation = view.generation;
    const startingSeq = view.snapshot?.seq || 0;
    view.detailStatus.set(id, "loading");
    repaint();
    try {
      const detail = await readHistory(`/api/history/${encodeURIComponent(id)}`);
      if (generation !== view.generation || detail.run_id !== view.runId) return;
      readMetadata(detail);
      if (detail.indexing) { view.indexing = true; view.detailStatus.set(id, "indexing"); retryIndex(); return; }
      view.details.set(id, detail);
      view.watermarks.set(id, detail.through_seq ?? startingSeq);
      view.detailStatus.set(id, "loaded");
    } catch (error) {
      if (generation === view.generation) view.detailStatus.set(id, `error:${error.message}`);
    } finally {
      if (generation === view.generation) repaint();
    }
  }

  /** Debounce actual lifecycle changes; repeated snapshots and token deltas cause no history reads. */
  function scheduleHistory(snapshot) {
    if (typeof fetch !== "function" || !view.runId) return;
    const key = JSON.stringify([snapshot.decision?.decision_id, snapshot.decision?.status,
      (snapshot.tool_calls || []).map(call => [call.tool_call_id, call.status]),
      (snapshot.model_requests || []).map(request => [request.model_request_id, request.status]),
      (snapshot.events || []).filter(event => !/_delta$/.test(event.type || "")).map(event => event.seq)]);
    if (key === view.refreshKey) return;
    view.refreshKey = key;
    if (!view.historyLoaded && !view.historyError && !view.indexing) { loadHistory(); loadSession(); return; }
    if (view.timer !== null || view.historyError) return;
    view.timer = setTimeout(() => {
      view.timer = null;
      loadHistory();
      loadSession();
    }, Math.max(0, HISTORY_REFRESH_MS - (Date.now() - view.lastRead)));
  }

  /** Bind one live snapshot and keep all loaded archive pages until the authoritative run changes. */
  function render(snapshot, options = {}) {
    if (!snapshot) return;
    view.onExpand = options.onExpand || null;
    if (view.runId !== snapshot.run_id) {
      view.runId = snapshot.run_id;
      view.generation += 1;
      if (view.timer !== null && typeof clearTimeout === "function") clearTimeout(view.timer);
      Object.assign(view, {signature: "", historyLoaded: false, sessionLoaded: false, nextBefore: null, nextSession: null,
        moreHistory: false, moreSession: false, totalDecisions: null, totalSession: null, historyBusy: false, sessionBusy: false,
        historyError: "", sessionError: "", indexing: false, refreshKey: "", timer: null, lastRead: 0, sessionEvents: [],
        issues: [], issueCount: 0, pendingTail: [], archiveError: ""});
      [view.expanded, view.tools, view.inspectors, view.inspectorPages, view.summaries, view.details, view.detailStatus, view.watermarks].forEach(map => map.clear());
    }
    view.snapshot = snapshot;
    repaint();
    scheduleHistory(snapshot);
  }

  window.ObservatoryTimeline = {render, buildGroups};
})();

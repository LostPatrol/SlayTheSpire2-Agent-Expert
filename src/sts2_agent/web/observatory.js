/** Appearance, responsive placement, accessible drawers, and local knowledge for the dashboard. */
"use strict";

(() => {
  /** Translate display prose while preserving the default Chinese client. */
  function tr(source, params = {}) {
    return window.ObservatoryI18n ? window.ObservatoryI18n.t(source, params)
      : source.replace(/\{(\w+)\}/g, (_, key) => String(params[key] ?? `{${key}}`));
  }

  // Appearance preferences are shared with the existing local knowledge page.
  const THEME_KEY = "sts2-agent-theme";
  // Panel folding is a presentation preference, independent of the controller.
  const FOLD_KEY = "spire-observatory-collapsed";
  // Theme metadata describes both the current mode and the next button action.
  const THEMES = {system: ["跟随系统", "auto"], light: ["浅色", "sun"], dark: ["深色", "moon"]};
  // Character colors follow the observed character, never a simulated run.
  const CHARACTER_THEMES = {IRONCLAD: "ironclad", SILENT: "silent", DEFECT: "defect", NECROBINDER: "necro", REGENT: "regent",
    "铁甲战士": "ironclad", "静默猎手": "silent", "故障机器人": "defect", "亡灵契约师": "necro", "储君": "regent"};
  // Drawer names are fixed local destinations and never backend-provided selectors.
  const DRAWERS = {sim: "drawer-sim", guide: "drawer-guide", reasoning: "drawer-reasoning", knowledge: "drawer-knowledge", obs: "drawer-obs"};
  // These categories match the imported local knowledge entity types.
  const KNOWLEDGE_KINDS = {cards: "卡牌", powers: "状态", monsters: "敌人", relics: "遗物", potions: "药水", events: "事件", encounters: "遭遇", keywords: "关键词", characters: "角色", orbs: "充能球", enchantments: "附魔", afflictions: "负面附加", mechanics: "机制"};
  // Bound local search requests and coalesce typing without delaying deliberate selections.
  const KNOWLEDGE_LIMIT = 30;
  const SEARCH_DELAY_MS = 220;
  // SVG graph dimensions preserve the original observatory silhouette.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SPARK_WIDTH = 240;
  const SPARK_HEIGHT = 40;
  const SPARK_MIN_SCALE = 120;
  // Only view state lives here; the main client owns all controller operations.
  const view = {initialized: false, theme: "system", drawer: null, lastFocus: null, searchVersion: 0, detailVersion: 0, searchTimer: null, knowledgeLoaded: false};
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  /** Find a fixed shell element. */
  function node(id) { return document.getElementById(id); }

  /** Construct text-only content for local data and diagnostics. */
  function element(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text ?? "");
    return result;
  }

  /** Apply a deliberate appearance preference without touching session settings. */
  function applyTheme(mode, persist = true) {
    view.theme = Object.hasOwn(THEMES, mode) ? mode : "system";
    document.documentElement.dataset.themeMode = view.theme;
    document.documentElement.dataset.theme = view.theme === "system" ? systemTheme.matches ? "dark" : "light" : view.theme;
    node("theme-label").textContent = tr(THEMES[view.theme][0]);
    node("theme-icon").setAttribute("href", `#i-${THEMES[view.theme][1]}`);
    const modes = Object.keys(THEMES);
    const next = modes[(modes.indexOf(view.theme) + 1) % modes.length];
    const label = tr("当前{current}；点击切换到{next}", {current: tr(THEMES[view.theme][0]), next: tr(THEMES[next][0])});
    node("theme-toggle").title = label;
    node("theme-toggle").setAttribute("aria-label", label);
    if (persist) {
      try { localStorage.setItem(THEME_KEY, view.theme); } catch (_) { /* Storage is optional. */ }
    }
  }

  /** Match the visual palette to the selected or observed native character. */
  function applyCharacter(character) {
    document.documentElement.dataset.character = CHARACTER_THEMES[String(character || "").toUpperCase()] || "ironclad";
  }

  /** Preserve the v2 telemetry bay while narrower screens retain a separate panel. */
  function placeTelemetry() {
    const telemetry = document.querySelector(".telemetry-panel");
    const stream = document.documentElement.dataset.layout === "stream";
    const bay = window.matchMedia("(min-width: 1500px)").matches
      || (stream && window.matchMedia("(min-width: 1181px)").matches);
    const target = bay ? node("decision-telemetry") : document.querySelector(".col-right");
    if (telemetry && target && telemetry.parentElement !== target) target.prepend(telemetry);
  }

  /** Keep both layout choices reloadable, with stream as the official default. */
  function applyLayout(mode, persist = true) {
    const layout = mode === "split" ? "split" : "stream";
    document.documentElement.dataset.layout = layout;
    document.querySelectorAll(".layout-option").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.layoutMode === layout)));
    if (persist) {
      const url = new URL(location.href);
      url.searchParams.set("layout", layout);
      history.replaceState(null, "", url);
    }
    placeTelemetry();
  }

  /** Select a presentation-only candidate or raw-output pane. */
  function selectSimulationTab(name) {
    document.querySelectorAll("[data-sim-tab]").forEach(button => {
      const active = button.dataset.simTab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".sim-pane").forEach(pane => {
      const active = pane.id === `sim-pane-${name}`;
      pane.classList.toggle("active", active);
      pane.hidden = !active;
    });
  }

  /** Open a local inspector and keep keyboard focus inside its modal surface. */
  function openDrawer(name) {
    const drawer = node(DRAWERS[name]);
    if (!drawer) return;
    if (view.drawer) closeDrawer(true);
    view.lastFocus = document.activeElement;
    view.drawer = name;
    const scrim = node("scrim");
    scrim.hidden = false;
    requestAnimationFrame(() => { if (view.drawer) scrim.classList.add("show"); });
    drawer.classList.add("open");
    drawer.inert = false;
    drawer.setAttribute("aria-hidden", "false");
    document.querySelector(".console").inert = true;
    document.body.classList.add("drawer-open");
    const target = name === "guide" ? node("instruction-text") : drawer.querySelector("[data-close]");
    target?.focus();
    if (name === "knowledge" && !view.knowledgeLoaded) searchKnowledge();
  }

  /** Close the inspector and return focus to its explicit opener. */
  function closeDrawer(immediate = false) {
    if (!view.drawer) return;
    const drawer = node(DRAWERS[view.drawer]);
    view.drawer = null;
    document.querySelector(".console").inert = false;
    if (view.lastFocus?.isConnected) view.lastFocus.focus();
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawer.inert = true;
    const scrim = node("scrim");
    scrim.classList.remove("show");
    if (immediate) scrim.hidden = true;
    else setTimeout(() => { if (!view.drawer) scrim.hidden = true; }, 230);
    document.body.classList.remove("drawer-open");
  }

  /** Trap focus only while an actual drawer is open. */
  function handleDrawerKey(event) {
    if (!view.drawer) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = [...node(DRAWERS[view.drawer]).querySelectorAll("button, a[href], input, select, textarea, summary, [tabindex]:not([tabindex='-1'])")]
      .filter(control => !control.disabled && control.getClientRects().length);
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  /** Read optional local panel preferences without requiring browser storage. */
  function readFolded() {
    try {
      const data = JSON.parse(localStorage.getItem(FOLD_KEY) || "[]");
      return new Set(Array.isArray(data) ? data.filter(value => typeof value === "string") : []);
    } catch (_) { return new Set(); }
  }

  /** Refresh a fold's visible action without changing its stored state. */
  function paintFoldLabel(head, source, collapsed) {
    const label = tr(source);
    head.setAttribute("aria-label", tr("{label}：{action}", {label, action: tr(collapsed ? "已收起，点击展开" : "点击收起")}));
    head.title = tr(collapsed ? "展开{label}" : "收起{label}", {label});
  }

  /** Add persistent mouse and keyboard folding to the existing panel headers. */
  function bindFolds() {
    const folded = readFolded();
    document.querySelectorAll(".panel").forEach(panel => {
      const stage = panel.classList.contains("decision");
      const head = panel.querySelector(stage ? ":scope > .decision-head" : ":scope > .panel-head");
      const title = head?.querySelector(".panel-title");
      const label = stage ? tr("当前决策") : title?.textContent.trim();
      // Keep existing panel preferences stable when their visible titles are translated.
      const foldKey = stage ? "当前决策" : title?.dataset.i18n || label;
      if (!head || !label) return;
      const chevron = document.createElementNS(SVG_NS, "svg");
      chevron.setAttribute("class", "fold-chevron ico");
      chevron.setAttribute("aria-hidden", "true");
      const use = document.createElementNS(SVG_NS, "use");
      use.setAttribute("href", "#i-chevron");
      chevron.append(use);
      (head.querySelector(".decision-meta") || head).append(chevron);
      head.setAttribute("role", "button");
      head.tabIndex = 0;
      head.dataset.foldLabel = foldKey;
      /** Apply one fold without replacing any live rendered data. */
      function apply(collapsed, persist = true) {
        panel.dataset.collapsed = String(collapsed);
        head.setAttribute("aria-expanded", String(!collapsed));
        paintFoldLabel(head, foldKey, collapsed);
        if (collapsed) folded.add(foldKey); else folded.delete(foldKey);
        if (persist) {
          try { localStorage.setItem(FOLD_KEY, JSON.stringify([...folded])); } catch (_) { /* Folding remains usable. */ }
        }
      }
      apply(folded.has(foldKey), false);
      head.addEventListener("click", () => apply(panel.dataset.collapsed !== "true"));
      head.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); apply(panel.dataset.collapsed !== "true"); }
      });
    });
  }

  /** Repaint initialized shell labels after the authoritative run language arrives. */
  function refreshLanguage() {
    if (!view.initialized) return;
    applyTheme(view.theme, false);
    for (const option of node("codex-kind")?.options || []) {
      if (option.value === "") option.textContent = tr("所有分类");
      else if (Object.hasOwn(KNOWLEDGE_KINDS, option.value)) option.textContent = tr(KNOWLEDGE_KINDS[option.value]);
    }
    document.querySelectorAll(".panel").forEach(panel => {
      const head = panel.querySelector(panel.classList.contains("decision") ? ":scope > .decision-head" : ":scope > .panel-head");
      if (head?.dataset.foldLabel) paintFoldLabel(head, head.dataset.foldLabel, panel.dataset.collapsed === "true");
    });
  }

  /** Fetch only local read-only knowledge resources and retain useful errors. */
  async function readKnowledge(path) {
    const response = await fetch(path, {cache: "no-store"});
    if (!response.ok) throw new Error(tr("本地知识库请求失败 ({status})", {status: response.status}));
    return response.json();
  }

  /** Show the selected source entity without inventing related records. */
  async function selectKnowledge(entry, button) {
    const version = ++view.detailVersion;
    const detail = node("codex-detail");
    detail.replaceChildren(element("p", "empty-note", tr("正在读取条目…")));
    document.querySelectorAll(".codex-entry").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
    try {
      const data = await readKnowledge(`/api/knowledge/${encodeURIComponent(entry.kind)}/${encodeURIComponent(entry.entity_id)}`);
      if (version !== view.detailVersion) return;
      detail.replaceChildren(element("h2", "", data.name || data.entity_id),
        element("p", "codex-id", `${tr(KNOWLEDGE_KINDS[entry.kind] || entry.kind)} · ${entry.entity_id}`),
        element("p", "codex-desc", data.description || data.text || tr("该条目没有独立的机制正文，请查看原始字段。")),
        element("h3", "", tr("来源与完整字段")), element("pre", "json-block", JSON.stringify(data, null, 2)));
      detail.scrollTop = 0;
    } catch (error) {
      if (version === view.detailVersion) detail.replaceChildren(element("p", "empty-note", error.message));
    }
  }

  /** Search the fixed local knowledge snapshot; older responses cannot overwrite newer input. */
  async function searchKnowledge() {
    clearTimeout(view.searchTimer);
    const version = ++view.searchVersion;
    ++view.detailVersion;
    const params = new URLSearchParams({q: node("codex-search").value.trim(), kind: node("codex-kind").value, limit: String(KNOWLEDGE_LIMIT)});
    node("codex-count").textContent = tr("搜索中…");
    const results = node("codex-results");
    const detail = node("codex-detail");
    try {
      const data = await readKnowledge(`/api/knowledge?${params}`);
      if (version !== view.searchVersion) return;
      view.knowledgeLoaded = true;
      const entries = Array.isArray(data.results) ? data.results : [];
      node("codex-count").textContent = tr("显示 {count} 条", {count: entries.length});
      results.replaceChildren(...entries.map(entry => {
        const button = element("button", "codex-entry");
        button.type = "button";
        button.setAttribute("aria-pressed", "false");
        button.append(element("strong", "", entry.name || entry.entity_id), element("small", "", `${tr(KNOWLEDGE_KINDS[entry.kind] || entry.kind)} · ${entry.entity_id}`));
        button.addEventListener("click", () => selectKnowledge(entry, button));
        return button;
      }));
      if (!entries.length) results.append(element("p", "empty-note", tr("没有匹配条目；试试英文名或实体 ID。")));
      detail.replaceChildren(element("p", "empty-note", tr("选择左侧条目查看来源、版本与完整字段。")));
      if (data.metadata) {
        const metadata = element("details", "knowledge-metadata");
        metadata.append(element("summary", "", tr("本地知识快照")), element("pre", "json-block", JSON.stringify(data.metadata, null, 2)));
        detail.append(metadata);
      }
    } catch (error) {
      if (version !== view.searchVersion) return;
      node("codex-count").textContent = tr("搜索失败");
      results.replaceChildren(element("p", "empty-note", error.message));
    }
  }

  // Fallback width of the live throughput window; the client passes its own value.
  const DEFAULT_SPEED_WINDOW_MS = 10000;
  // Reused sparkline nodes; streaming updates only replace path data.
  let spark = null;

  /** Draw a smooth quadratic line through sample midpoints without overshooting the values. */
  function smoothLine(coords) {
    if (coords.length < 3) {
      return coords.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
    }
    let path = `M${coords[0][0].toFixed(1)} ${coords[0][1].toFixed(1)}`;
    for (let index = 1; index < coords.length - 1; index += 1) {
      const [x, y] = coords[index];
      const [nextX, nextY] = coords[index + 1];
      path += ` Q${x.toFixed(1)} ${y.toFixed(1)} ${((x + nextX) / 2).toFixed(1)} ${((y + nextY) / 2).toFixed(1)}`;
    }
    const [lastX, lastY] = coords.at(-1);
    return `${path} L${lastX.toFixed(1)} ${lastY.toFixed(1)}`;
  }

  /** Create the sparkline once so per-frame updates avoid rebuilding SVG nodes. */
  function ensureSpark(box) {
    if (spark && spark.svg.parentNode === box) return spark;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const defs = document.createElementNS(SVG_NS, "defs");
    const gradient = document.createElementNS(SVG_NS, "linearGradient");
    gradient.id = "spark-fill";
    gradient.setAttribute("x1", "0"); gradient.setAttribute("y1", "0");
    gradient.setAttribute("x2", "0"); gradient.setAttribute("y2", "1");
    for (const [offset, className] of [["0", "spark-stop-top"], ["1", "spark-stop-bottom"]]) {
      const stop = document.createElementNS(SVG_NS, "stop");
      stop.setAttribute("offset", offset); stop.setAttribute("class", className); gradient.append(stop);
    }
    defs.append(gradient);
    const area = document.createElementNS(SVG_NS, "path");
    area.setAttribute("fill", "url(#spark-fill)");
    const line = document.createElementNS(SVG_NS, "path");
    line.setAttribute("class", "spark-line");
    svg.append(defs, area, line);
    box.replaceChildren(svg);
    spark = {svg, area, line};
    return spark;
  }

  /** Plot time-stamped samples on a fixed window; both edges stay pinned smoothly. */
  function renderSpeed(points, now = Date.now(), windowMs = DEFAULT_SPEED_WINDOW_MS, liveValue = null) {
    const box = node("speed-bars");
    const samples = (points || [])
      .filter(point => point && Number.isFinite(point.time) && Number.isFinite(point.value))
      .sort((a, b) => a.time - b.time);
    if (!samples.length) { box.replaceChildren(); spark = null; return; }
    const start = now - windowMs;
    const visible = samples.filter(point => point.time >= start);
    if (!visible.length) { box.replaceChildren(); spark = null; return; }
    // Keep the sample before the window so the left edge can be interpolated.
    const previous = samples.filter(point => point.time < start).at(-1);
    const live = Number.isFinite(liveValue) && liveValue >= 0 ? liveValue : null;
    const scale = Math.max(SPARK_MIN_SCALE, previous ? previous.value : 0, live ?? 0,
      ...visible.map(point => point.value));
    const yFor = value => SPARK_HEIGHT * (1 - Math.max(0, value) / scale);
    const coords = [];
    if (previous && visible[0].time > start) {
      const ratio = (start - previous.time) / (visible[0].time - previous.time);
      coords.push([0, yFor(previous.value + (visible[0].value - previous.value) * ratio)]);
    }
    visible.forEach(point => coords.push([
      Math.min(SPARK_WIDTH, Math.max(0, (point.time - start) / windowMs * SPARK_WIDTH)),
      yFor(point.value),
    ]));
    if (live !== null) {
      // The live estimate moves continuously between samples and holds the cursor honest.
      coords.push([SPARK_WIDTH, yFor(live)]);
    } else if (coords.at(-1)[0] < SPARK_WIDTH) {
      // Hold the newest reading at the live cursor instead of leaving a drifting gap.
      coords.push([SPARK_WIDTH, coords.at(-1)[1]]);
    }
    const line = smoothLine(coords);
    const sparkline = ensureSpark(box);
    sparkline.line.setAttribute("d", line);
    sparkline.area.setAttribute("d", `${line} L${coords.at(-1)[0].toFixed(1)} ${SPARK_HEIGHT} L${coords[0][0].toFixed(1)} ${SPARK_HEIGHT} Z`);
  }

  /** Bind appearance and inspectors once; the main client owns network lifecycle and controls. */
  function initialize() {
    if (view.initialized) return;
    view.initialized = true;
    applyTheme(document.documentElement.dataset.themeMode, false);
    node("theme-toggle").addEventListener("click", () => {
      const modes = Object.keys(THEMES);
      applyTheme(modes[(modes.indexOf(view.theme) + 1) % modes.length]);
    });
    systemTheme.addEventListener("change", () => { if (view.theme === "system") applyTheme("system", false); });
    window.addEventListener("storage", event => { if (event.key === THEME_KEY || event.key === null) applyTheme(event.newValue, false); });
    document.querySelectorAll(".layout-option").forEach(button => button.addEventListener("click", () => applyLayout(button.dataset.layoutMode)));
    applyLayout(new URLSearchParams(location.search).get("layout"), false);
    window.matchMedia("(min-width: 1500px)").addEventListener("change", placeTelemetry);
    window.matchMedia("(min-width: 1181px)").addEventListener("change", placeTelemetry);
    document.querySelectorAll("[data-sim-tab]").forEach(button => button.addEventListener("click", () => selectSimulationTab(button.dataset.simTab)));
    document.addEventListener("click", event => {
      const opener = event.target.closest("[data-open]");
      if (opener) openDrawer(opener.dataset.open);
      else if (event.target.closest("[data-close]")) closeDrawer();
    });
    node("knowledge-open").addEventListener("click", () => openDrawer("knowledge"));
    node("scrim").addEventListener("click", () => closeDrawer());
    document.addEventListener("keydown", handleDrawerKey);
    Object.entries(KNOWLEDGE_KINDS).forEach(([kind, label]) => {
      const option = element("option", "", tr(label)); option.value = kind; node("codex-kind").append(option);
    });
    node("codex-kind").addEventListener("change", searchKnowledge);
    node("codex-search").addEventListener("input", () => {
      ++view.searchVersion;
      ++view.detailVersion;
      clearTimeout(view.searchTimer);
      view.searchTimer = setTimeout(searchKnowledge, SEARCH_DELAY_MS);
    });
    node("codex-search").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); searchKnowledge(); } });
    bindFolds();
  }

  window.Observatory = {initialize, refreshLanguage, openDrawer, closeDrawer, applyCharacter, renderSpeed};
})();

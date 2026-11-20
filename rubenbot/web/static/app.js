/* RubenBot 控制台：原生 JS 单页应用，无构建链。
 *
 * 结构：小工具（DOM/格式化/请求）→ 路由 → 各页面渲染函数。
 * 安全：所有来自 API 的文本都用 textContent 写入，不用 innerHTML 拼数据。
 */
"use strict";

/* ------------------------------------------------------------------ */
/* 工具                                                               */
/* ------------------------------------------------------------------ */

const $ = (selector, root = document) => root.querySelector(selector);

const SVG_NS = "http://www.w3.org/2000/svg";

/** 创建元素。props 里 class / dataset / on* / 普通属性各有处理。 */
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "html") {
      // 只用于本文件内的固定标记
      node.innerHTML = value;
    } else if (key === "text") {
      node.textContent = value;
    } else if (key === "value" && tag === "textarea") {
      // textarea 的 value 只能走属性，setAttribute("value") 不会回填内容
      node.value = String(value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** 图标：用 index.html 里的 SVG sprite。 */
function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function fmtTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmtClock(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function fmtDateTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

function fmtRelative(ts) {
  if (!ts) return "从未";
  const diff = Date.now() / 1000 - ts;
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.round(diff / 86400)} 天前`;
  return fmtDateTime(ts);
}

function fmtDuration(seconds) {
  if (!seconds || seconds < 1) return "不到 1 分钟";
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function fmtNumber(value) {
  const num = Number(value || 0);
  return num.toLocaleString("zh-CN");
}

function fmtPercent(value, digits = 0) {
  return `${(Number(value || 0) * 100).toFixed(digits)}%`;
}

const APP_NAMES = {
  platform: { console: "终端", snowluma: "SnowLuma", recording: "测试" },
};

function platformName(platform) {
  return (APP_NAMES.platform && APP_NAMES.platform[platform]) || platform || "未知";
}

const ACTION_NAMES = {
  merge: "合并",
  drop: "删除",
  forget: "遗忘",
  restore: "撤销",
  add: "新增",
};

const CATEGORY_NAMES = {
  identity: "身份",
  preference: "偏好",
  habit: "习惯",
  relationship: "关系",
  experience: "经历",
  promise: "承诺",
  fact: "事实",
};

function categoryName(value) {
  return CATEGORY_NAMES[value] || value || "事实";
}

function sourceName(value) {
  return { extracted: "自动抽取", user: "手动添加", system: "系统" }[value] || value;
}

function avatarText(name, fallback = "?") {
  const text = String(name || "").trim();
  if (!text) return fallback;
  return text.slice(0, 1).toUpperCase();
}

let toastSeq = 0;
function toast(message, kind = "ok") {
  const box = $("#toasts");
  if (!box) return;
  const node = h(
    "div",
    { class: `toast ${kind === "error" ? "error" : ""}`, dataset: { seq: String(++toastSeq) } },
    icon(kind === "error" ? "warning" : "check"),
    h("span", {}, message)
  );
  box.append(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(4px)";
    setTimeout(() => node.remove(), 220);
  }, kind === "error" ? 5200 : 3000);
}

/** 轻量防抖：用于搜索框。 */
function debounce(fn, wait = 260) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function emptyState(iconName, title, note) {
  return h(
    "div",
    { class: "empty" },
    icon(iconName),
    h("strong", {}, title),
    note ? h("p", {}, note) : null
  );
}

function skeleton(rows = 4) {
  const list = h("div", { class: "rows" });
  for (let i = 0; i < rows; i += 1) {
    list.append(
      h(
        "div",
        { class: "skeleton-row" },
        h("div", { class: "skeleton", style: { width: "32px", height: "32px", borderRadius: "10px" } }),
        h(
          "div",
          { style: { flex: "1", display: "grid", gap: "7px" } },
          h("div", { class: "skeleton", style: { width: `${64 - i * 6}%` } }),
          h("div", { class: "skeleton", style: { width: `${34 + i * 4}%`, height: "11px" } })
        )
      )
    );
  }
  return list;
}

/* ------------------------------------------------------------------ */
/* 请求                                                               */
/* ------------------------------------------------------------------ */

class Unauthorized extends Error {}

async function request(method, path, body) {
  const options = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("网络请求失败，服务可能已停止");
  }
  let data = null;
  try {
    data = await response.json();
  } catch (err) {
    data = null;
  }
  if (response.status === 401) {
    showLogin();
    throw new Unauthorized((data && data.error) || "需要登录");
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || `请求失败（${response.status}）`);
    error.status = response.status;
    error.fields = (data && data.fields) || [];
    throw error;
  }
  return data;
}

const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
  put: (path, body) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

/* ------------------------------------------------------------------ */
/* 主题与登录                                                         */
/* ------------------------------------------------------------------ */

function applyTheme(mode) {
  const resolved =
    mode === "dark" ||
    (mode === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches)
      ? "dark"
      : "light";
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themeMode = mode;
  localStorage.setItem("rubenbot.theme", mode);
  const use = $("#theme-icon use");
  if (use) use.setAttribute("href", resolved === "dark" ? "#i-moon" : "#i-sun");
  const button = $("#theme-toggle");
  if (button) button.setAttribute("aria-label", resolved === "dark" ? "切换到亮色" : "切换到暗色");
  const segmented = document.querySelectorAll("[data-theme-choice]");
  segmented.forEach((node) => node.classList.toggle("active", node.dataset.themeChoice === mode));
}

function setupTheme() {
  applyTheme(localStorage.getItem("rubenbot.theme") || "auto");
  $("#theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme;
    applyTheme(current === "dark" ? "light" : "dark");
  });
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", () => {
    if ((localStorage.getItem("rubenbot.theme") || "auto") === "auto") applyTheme("auto");
  });
}

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  const input = $("#login-password");
  if (input) input.focus();
}

function hideLogin() {
  $("#login").hidden = true;
  $("#app").hidden = false;
}

function setupLogin() {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("#login-error");
    error.textContent = "";
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api.post("/api/login", { password: $("#login-password").value });
      hideLogin();
      boot();
    } catch (err) {
      error.textContent = err.message || "登录失败";
    } finally {
      button.disabled = false;
    }
  });
}

/* ------------------------------------------------------------------ */
/* 顶栏状态                                                           */
/* ------------------------------------------------------------------ */

function setSidebarStatus(text, state) {
  const textNode = $("#sidebar-status-text");
  const dot = $("#sidebar-status .live-dot");
  if (textNode) textNode.textContent = text;
  if (dot) dot.dataset.state = state;
}

async function pollHealth() {
  try {
    const health = await api.get("/api/health");
    setSidebarStatus(health.running ? "运行中" : "未运行", health.running ? "ok" : "off");
  } catch (err) {
    if (err instanceof Unauthorized) return;
    setSidebarStatus("连接断开", "warn");
  }
}

/* ------------------------------------------------------------------ */
/* 页面元信息与路由                                                   */
/* ------------------------------------------------------------------ */

const PAGES = {
  overview: { title: "概览", sub: "运行状态与最近动静", render: renderOverview },
  persona: { title: "人设", sub: "编辑人格文件，保存即热生效", render: renderPersona },
  memory: { title: "记忆", sub: "按人浏览、搜索与维护记忆库", render: renderMemory },
  people: { title: "人物", sub: "见过的每个人和他们的档案", render: renderPeople },
  groups: { title: "群组与意愿", sub: "每群活跃度、静默时段与决策记录", render: renderGroups },
  plugins: { title: "插件", sub: "已发现的插件与启停状态", render: renderPlugins },
  logs: { title: "日志", sub: "最近的运行日志", render: renderLogs },
  settings: { title: "设置", sub: "模型、记忆、意愿与运行参数，保存即生效", render: renderSettings },
};

const state = {
  page: "overview",
  param: "",
  pollTimer: null,
  booted: false,
};

function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [page, ...rest] = raw.split("/");
  return { page: PAGES[page] ? page : "overview", param: rest.join("/") };
}

function navigate(page, param = "") {
  location.hash = param ? `#/${page}/${param}` : `#/${page}`;
}

function setHeader(title, sub, actions = []) {
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  const box = $("#page-actions");
  box.replaceChildren(...actions);
}

async function renderRoute() {
  const route = currentRoute();
  state.page = route.page;
  state.param = route.param;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.page === route.page);
  });
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  const meta = PAGES[route.page];
  setHeader(meta.title, meta.sub, []);
  // 换页即丢弃上一页的未保存编辑，顺带收掉 beforeunload 提醒
  setPersonaDirty(false);
  const view = $("#view");
  view.replaceChildren(skeleton(3));
  try {
    await meta.render(view, route.param);
  } catch (err) {
    if (err instanceof Unauthorized) return;
    view.replaceChildren();
    view.append(
      h(
        "div",
        { class: "card" },
        emptyState("warning", "加载失败", err.message || "未知错误"),
        h(
          "div",
          { style: { display: "flex", justifyContent: "center", paddingBottom: "26px" } },
          h("button", { class: "btn", onclick: () => renderRoute() }, icon("refresh"), "重试")
        )
      )
    );
  }
}

/** 按间隔刷新页面（只在该页面停留时）。 */
function onPoll(ms, fn) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(() => {
    if (document.hidden) return;
    fn();
  }, ms);
}

/* ------------------------------------------------------------------ */
/* 共享组件                                                           */
/* ------------------------------------------------------------------ */

function card(title, { hint, actions, body, flush } = {}) {
  return h(
    "section",
    { class: "card" },
    title || actions
      ? h(
          "div",
          { class: "card-head" },
          h("h2", {}, title || ""),
          h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
            hint ? h("span", { class: "hint" }, hint) : null,
            ...(actions || []))
        )
      : null,
    h("div", { class: `card-body ${flush ? "flush" : ""}` }, body)
  );
}

function kpi(label, iconName, value, note, hint) {
  return h(
    "div",
    { class: "card kpi" },
    h("div", { class: "kpi-top" }, h("span", {}, label), h("span", { class: "kpi-icon" }, icon(iconName))),
    h("div", { class: "kpi-value" }, value),
    h("div", { class: "kpi-note" }, note, hint ? [" ", h("em", {}, hint)] : null)
  );
}

/** 概览英雄卡：运行状态 + 大号发光主按钮造型（跳设置）。 */
function overviewHero(data) {
  const running = Boolean(data.bot.running);
  const runningAdapters = data.adapters.filter((item) => item.running).length;
  return h(
    "section",
    { class: "hero" },
    h(
      "div",
      { class: "hero-copy" },
      h(
        "span",
        { class: `hero-status ${running ? "" : "is-off"}` },
        h("span", { class: "live-dot", dataset: { state: running ? "ok" : "off" } }),
        running ? "运行中" : "已停止"
      ),
      h("h2", { class: "hero-title" }, data.bot.name || "RubenBot"),
      h("p", { class: "hero-sub" },
        running ? `已运行 ${fmtDuration(data.bot.uptime_seconds)}，正在等待下一条消息` : "bot 当前没有运行"),
      h(
        "div",
        { class: "hero-chips" },
        h("span", { class: "hero-chip" }, `适配器 ${runningAdapters} / ${data.adapters.length}`),
        h("span", { class: "hero-chip" }, `记忆 ${fmtNumber(data.counts.memories)} 条`),
        h("span", { class: "hero-chip" }, `插件 ${data.plugins.enabled} / ${data.plugins.total}`),
        h("span", { class: "hero-chip" }, `WebUI ${data.webui.host}:${data.webui.port}`)
      )
    ),
    h("a", { class: "hero-action", href: "#/settings", "aria-label": "打开设置" },
      icon("settings"), "设置")
  );
}

function scoreBar(score, threshold, hard) {
  const track = h("div", { class: "track" });
  const fill = h("div", { class: `fill ${score < 0 ? "neg" : ""}` });
  // 条的上限取 threshold / 0.75，让阈值线稳定落在 3/4 处，分数超阈值时也不会立刻顶格
  const scale = hard ? 1 : Math.max(threshold / 0.75, 0.08);
  fill.style.width = `${Math.max(0, Math.min(100, (score / scale) * 100))}%`;
  const bar = h(
    "div",
    { class: "scorebar", role: "img", "aria-label": `意愿分数 ${score.toFixed(2)}，阈值 ${threshold.toFixed(2)}` },
    track,
    fill
  );
  if (!hard && threshold > 0) {
    const mark = h("div", { class: "threshold" });
    mark.style.left = `${Math.min(100, (threshold / scale) * 100)}%`;
    bar.append(mark);
  }
  return bar;
}

function decisionItem(decision, { showChat = false } = {}) {
  const hard = decision.hard_trigger || null;
  const verdict = decision.should_reply;
  const tag = hard
    ? h("span", { class: "tag accent" }, icon("pulse"), "硬触发")
    : verdict
    ? h("span", { class: "tag good" }, icon("check"), "回复")
    : h("span", { class: "tag" }, "不回复");
  const factors = decision.factors || {};
  const parts = [];
  if (factors.base) parts.push(["基础", factors.base]);
  if (factors.momentum) parts.push(["动量", factors.momentum]);
  if (factors.relevance) parts.push(["相关", factors.relevance]);
  if (factors.ratio_penalty) parts.push(["占比", -factors.ratio_penalty]);
  if (factors.cooldown_penalty) parts.push(["冷却", -factors.cooldown_penalty]);
  if (factors.noise) parts.push(["随机", factors.noise]);

  const meta = h(
    "div",
    { class: "factors" },
    showChat ? h("span", {}, h("b", {}, platformName(decision.platform)), " ", decision.chat_id) : null,
    h("span", {}, decision.sender_name || decision.sender_id || "未知"),
    h("span", {}, fmtTime(decision.created_at)),
    ...parts.map(([name, value]) =>
      h("span", { class: value >= 0 ? "factor-pos" : "factor-neg" }, `${name} ${value >= 0 ? "+" : ""}${value.toFixed(2)}`)
    )
  );

  return h(
    "div",
    { class: "decision" },
    h(
      "div",
      { class: "decision-head" },
      tag,
      h("span", { class: "decision-text", title: decision.text }, decision.text || "（空消息）"),
      h(
        "span",
        { class: "decision-score" },
        hard ? "直接放行" : `${decision.score.toFixed(2)} / ${decision.threshold.toFixed(2)}`
      )
    ),
    hard ? null : scoreBar(decision.score, decision.threshold, false),
    meta
  );
}

function confirmDialog({ title, message, confirmText = "确定", danger = false, onConfirm }) {
  const dialog = $("#dialog");
  const close = () => dialog.close();
  const confirm = h(
    "button",
    { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "button" },
    confirmText
  );
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      await onConfirm();
      close();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message || "操作失败", "error");
    } finally {
      confirm.disabled = false;
    }
  });
  dialog.replaceChildren(
    h(
      "div",
      { class: "dialog-head" },
      h("h2", {}, title),
      h("button", { class: "icon-button", type: "button", "aria-label": "关闭", onclick: close }, icon("close"))
    ),
    h("div", { class: "dialog-body" }, h("p", { class: "soft", style: { margin: 0 } }, message)),
    h("div", { class: "dialog-foot" },
      h("button", { class: "btn btn-ghost", type: "button", onclick: close }, "取消"),
      confirm)
  );
  dialog.showModal();
}

/* ------------------------------------------------------------------ */
/* 概览                                                               */
/* ------------------------------------------------------------------ */

async function renderOverview(view) {
  view.replaceChildren(skeleton(4));
  const load = async () => {
    const data = await api.get("/api/overview");
    const counts = data.counts;
    const replyRate = counts.decisions ? counts.decision_replies / counts.decisions : 0;

    const tiles = h(
      "div",
      { class: "grid grid-kpi" },
      kpi("消息总数", "chat", fmtNumber(counts.messages), `其中 bot 回复 ${fmtNumber(counts.replies)} 条`),
      kpi("记忆总量", "memory", fmtNumber(counts.memories), "最近 24 小时", `+${fmtNumber(counts.memories_24h)}`),
      kpi("人物与会话", "people", fmtNumber(counts.people), `活跃会话 ${fmtNumber(counts.chats)} 个`),
      kpi("回复决策", "pulse", fmtNumber(counts.decisions), `开口率 ${fmtPercent(replyRate)}`, `硬触发 ${fmtNumber(counts.hard_triggers)}`)
    );

    const runtime = card(
      "运行状态",
      {
        body: h(
          "div",
          { style: { display: "grid", gap: "0", padding: "4px 0" } },
          kvRow("Bot", data.bot.name),
          kvRow("状态", data.bot.running ? "运行中" : "已停止"),
          kvRow("已运行", fmtDuration(data.bot.uptime_seconds)),
          kvRow("最近消息", data.bot.last_message_at ? fmtRelative(data.bot.last_message_at) : "还没有"),
          kvRow("人格文件", data.bot.persona_path)
        ),
      }
    );

    const adapters = card(
      "适配器",
      {
        flush: true,
        body: h(
          "div",
          { class: "rows" },
          ...data.adapters.length
            ? data.adapters.map((adapter) =>
                h(
                  "div",
                  { class: "row" },
                  h("div", { class: "row-main" },
                    h("div", { class: "row-title" }, platformName(adapter.name)),
                    h("div", { class: "row-sub" }, adapter.detail || (adapter.enabled ? "已配置" : "未配置"))),
                  h("div", { class: "row-side" },
                    adapter.running
                      ? h("span", { class: "tag good" }, icon("check"), "运行中")
                      : adapter.enabled
                      ? h("span", { class: "tag warn" }, "未运行")
                      : h("span", { class: "tag outline" }, "未启用"))
                )
              )
            : [emptyState("plug", "没有启用适配器", "在设置里配置 console 或 snowluma")]
        ),
      }
    );

    const decisions = card(
      "最近决策",
      {
        flush: true,
        body: data.recent_decisions.length
          ? h("div", { class: "rows" }, ...data.recent_decisions.map((d) => decisionItem(d, { showChat: true })))
          : emptyState("pulse", "还没有决策记录", "bot 收到群消息后会在这里留下评分"),
      }
    );

    const plugins = card(
      "插件",
      {
        body: h(
          "div",
          { style: { display: "flex", alignItems: "baseline", gap: "8px" } },
          h("span", { class: "kpi-value", style: { fontSize: "22px" } }, `${data.plugins.enabled} / ${data.plugins.total}`),
          h("span", { class: "muted" }, "个插件启用"),
          h(
            "a",
            { class: "btn btn-sm btn-ghost", href: "#/plugins", style: { marginLeft: "auto" } },
            "管理",
            icon("caret")
          )
        ),
      }
    );

    const webui = card(
      "WebUI",
      {
        body: h(
          "div",
          { style: { display: "grid", gap: "0", padding: "4px 0" } },
          kvRow("监听地址", `${data.webui.host}:${data.webui.port}`),
          kvRow("访问口令", data.webui.auth_required ? "已设置" : "不需要（仅本机）")
        ),
      }
    );

    view.replaceChildren(
      overviewHero(data),
      tiles,
      h("div", { class: "grid grid-2" }, h("div", { class: "grid" }, runtime, adapters), h("div", { class: "grid" }, plugins, webui)),
      decisions
    );
    setSidebarStatus(data.bot.running ? "运行中" : "未运行", data.bot.running ? "ok" : "off");
  };
  await load();
  onPoll(6000, () => load().catch(() => {}));
}

function kvRow(key, value) {
  return h("div", { class: "kv" }, h("span", { class: "k" }, key), h("span", { class: "v" }, String(value)));
}

/* ------------------------------------------------------------------ */
/* 人设                                                               */
/* ------------------------------------------------------------------ */

/** 离开人设页时若有未保存修改，给一次浏览器级提醒。 */
const personaDirty = { active: false, beforeUnload: null };

function setPersonaDirty(dirty, { dot, saveButton, note } = {}) {
  personaDirty.active = dirty;
  if (dot) dot.hidden = !dirty;
  if (saveButton) saveButton.classList.toggle("is-dirty", dirty);
  if (note) note.hidden = !dirty;
  if (dirty && !personaDirty.beforeUnload) {
    personaDirty.beforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", personaDirty.beforeUnload);
  } else if (!dirty && personaDirty.beforeUnload) {
    window.removeEventListener("beforeunload", personaDirty.beforeUnload);
    personaDirty.beforeUnload = null;
  }
}

async function renderPersona(view) {
  const data = await api.get("/api/persona");

  const editor = h("textarea", {
    class: "textarea code persona-editor",
    spellcheck: "false",
    "aria-label": "人格文件内容",
    value: data.text,
    placeholder: "写一段自然的人格描述，比如「你是 Ruben，就是个来聊天的人…」",
  });

  const pathLine = h("span", { class: "hint persona-path", title: data.path }, data.path);
  const dot = h("span", { class: "dirty-dot", title: "有未保存的修改", hidden: true });
  const dirtyNote = h("span", { class: "muted", style: { fontSize: "12px" }, hidden: true }, "有未保存的修改");
  const saveButton = h("button", { class: "btn btn-primary", type: "button" }, icon("check"), "保存并生效");
  const reloadButton = h("button", { class: "btn btn-sm", type: "button" }, icon("refresh"), "重新读取");
  const resetButton = h("button", { class: "btn btn-danger", type: "button" }, icon("restore"), "恢复默认");
  const notice = h("div", { class: "notice", hidden: true });
  const stats = h("span", { class: "muted mono", style: { fontSize: "12px" } });

  const updateStats = () => {
    const text = editor.value;
    stats.textContent = `${text.length} 字 · ${text.split("\n").length} 行`;
  };
  const markDirty = () => setPersonaDirty(editor.value !== data.text, { dot, saveButton, note: dirtyNote });

  editor.addEventListener("input", () => {
    updateStats();
    markDirty();
  });
  editor.addEventListener("keydown", (event) => {
    // Ctrl/Cmd+S 直接保存，编辑长文本时顺手
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!saveButton.disabled) saveButton.click();
    }
  });
  // Tab 在等宽编辑区里插入两个空格，而不是跳走焦点
  editor.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey) return;
    event.preventDefault();
    const start = editor.selectionStart;
    editor.setRangeText("  ", start, editor.selectionEnd, "end");
    updateStats();
    markDirty();
  });
  updateStats();
  markDirty();

  const showNotice = (kind, message) => {
    notice.hidden = false;
    notice.className = `notice ${kind === "good" ? "good" : ""}`;
    notice.replaceChildren(icon(kind === "good" ? "check" : "warning"), h("span", {}, message));
  };

  saveButton.addEventListener("click", async () => {
    if (!editor.value.trim()) {
      toast("人格内容不能为空", "error");
      editor.focus();
      return;
    }
    saveButton.disabled = true;
    saveButton.classList.add("busy");
    try {
      const result = await api.put("/api/persona", { text: editor.value });
      data.text = editor.value;
      data.exists = true;
      setPersonaDirty(false, { dot, saveButton, note: dirtyNote });
      showNotice("good", result.message || "已生效，无需重启。");
      toast(result.applied ? "人设已保存并生效" : "人设已保存，下次启动生效");
      await loadEffective();
    } catch (err) {
      if (err instanceof Unauthorized) return;
      showNotice("bad", err.message || "保存失败");
      toast("保存失败", "error");
    } finally {
      saveButton.disabled = false;
      saveButton.classList.remove("busy");
    }
  });

  reloadButton.addEventListener("click", async () => {
    if (personaDirty.active) {
      confirmDialog({
        title: "放弃未保存的修改？",
        message: "重新读取会丢弃编辑区里还没保存的内容。",
        confirmText: "重新读取",
        danger: true,
        onConfirm: async () => {
          setPersonaDirty(false);
          await renderRoute();
        },
      });
      return;
    }
    await renderRoute();
  });

  resetButton.addEventListener("click", () => {
    confirmDialog({
      title: "恢复默认人格？",
      message: "当前人格文件会先备份为 .bak，然后换成内置的默认 Ruben 人格；运行中的 bot 会立即改用默认人格。",
      confirmText: "恢复默认",
      danger: true,
      onConfirm: async () => {
        const result = await api.post("/api/persona/reset");
        setPersonaDirty(false);
        toast(result.applied ? "已恢复默认人格并生效" : "已恢复默认人格，下次启动生效");
        await renderRoute();
      },
    });
  });

  const editCard = card("人格文件", {
    actions: [dot, reloadButton],
    body: h(
      "div",
      { class: "persona-edit" },
      h(
        "div",
        { class: "persona-meta" },
        pathLine,
        data.exists ? null : h("span", { class: "tag outline" }, "文件尚未创建，保存后生成"),
        h("span", { class: "persona-meta-spacer" }),
        h("span", { class: "hint" }, data.mtime ? `修改于 ${fmtDateTime(data.mtime)}` : "—"),
        stats
      ),
      editor,
      h(
        "div",
        { class: "persona-foot" },
        saveButton,
        resetButton,
        dirtyNote,
        h("span", { class: "persona-meta-spacer" }),
        h("span", { class: "hint" }, data.hot_reload ? "保存后立即生效" : "当前无运行中的 bot，保存后下次启动生效")
      ),
      notice
    ),
  });

  /* 只读：当前实际生效的人格 + 拼装预览 */
  const effectiveBody = h("div", { class: "persona-effective" });
  const previewBody = h("div", { class: "persona-preview" });
  const effectiveCard = card("当前生效的人格", {
    hint: data.effective_source === "bot" ? "来自运行中的 bot" : data.effective_source === "default" ? "内置默认" : "来自人格文件",
    body: effectiveBody,
  });
  const previewCard = card("拼装预览", {
    hint: "系统提示词的示例拼装",
    body: previewBody,
  });

  const loadEffective = async () => {
    const [current, preview] = await Promise.all([
      api.get("/api/persona"),
      api.get("/api/persona/preview").catch(() => null),
    ]);
    const drift =
      current.effective_source !== "default" && current.effective !== data.text;
    const tag =
      current.effective_source === "bot"
        ? h("span", { class: "tag good" }, icon("check"), "已生效")
        : current.effective_source === "default"
        ? h("span", { class: "tag outline" }, "默认人格")
        : h("span", { class: "tag outline" }, "按文件加载");
    effectiveBody.replaceChildren(
      h(
        "div",
        { class: "persona-effective-head" },
        tag,
        h("span", { class: "hint" }, `生效名：${current.effective_name || "Ruben"} · ${current.effective_chars} 字`)
      ),
      drift
        ? h("div", { class: "notice" }, icon("warning"),
            h("span", {}, "编辑区的内容和当前生效的人格不一致：可能刚保存还没有生效，或文件被外部改过。"))
        : null,
      h("pre", { class: "persona-readonly" }, current.effective || "（空）")
    );
    if (preview) {
      previewBody.replaceChildren(h("pre", { class: "persona-readonly" }, preview.prompt));
    } else {
      previewBody.replaceChildren(h("p", { class: "muted", style: { margin: 0, fontSize: "12.5px" } }, "预览不可用。"));
    }
  };
  await loadEffective();

  view.replaceChildren(editCard, h("div", { class: "grid grid-2" }, effectiveCard, previewCard));
  // 切到别的页时由 renderRoute 统一清掉脏标记与 beforeunload 提醒
}

/* ------------------------------------------------------------------ */
/* 记忆                                                               */
/* ------------------------------------------------------------------ */

const memoryState = {
  tab: "list",
  query: "",
  personId: "",
  people: [],
  total: 0,
  offset: 0,
  limit: 50,
};

async function renderMemory(view) {
  const data = await api.get("/api/people?limit=500");
  memoryState.people = data.items;
  if (memoryState.people.length && !memoryState.personId) memoryState.personId = "";

  const toolbar = h(
    "div",
    { class: "toolbar" },
    h(
      "div",
      { class: "input-search" },
      icon("search"),
      h("input", {
        class: "input",
        type: "search",
        placeholder: "搜索记忆内容",
        value: memoryState.query,
        "aria-label": "搜索记忆",
        oninput: (event) => {
          memoryState.query = event.target.value;
          debouncedSearch();
        },
      })
    ),
    h(
      "select",
      {
        class: "select",
        style: { width: "auto", minWidth: "170px" },
        "aria-label": "按人物筛选",
        onchange: (event) => {
          memoryState.personId = event.target.value;
          loadList();
        },
      },
      h("option", { value: "" }, "全部人物"),
      ...memoryState.people.map((person) =>
        h(
          "option",
          { value: person.person_id, selected: person.person_id === memoryState.personId },
          `${person.display_name || person.person_id}（${person.memory_count}）`
        )
      )
    ),
    h("div", { class: "spacer" }),
    h("div", { class: "segmented", role: "tablist" },
      tabButton("list", "记忆列表"),
      tabButton("log", "变更日志")),
    h("button", { class: "btn btn-primary", onclick: () => openMemoryDialog() }, icon("plus"), "新增记忆")
  );

  const content = h("div", { id: "memory-content" });
  view.replaceChildren(h("section", { class: "card" }, toolbar, content));

  function tabButton(value, label) {
    return h(
      "button",
      {
        type: "button",
        class: value === memoryState.tab ? "active" : "",
        role: "tab",
        "aria-selected": value === memoryState.tab ? "true" : "false",
        onclick: (event) => {
          memoryState.tab = value;
          const group = event.currentTarget.parentElement;
          group.querySelectorAll("button").forEach((button) => {
            const active = button.dataset.tab === value;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
          });
          if (value === "list") loadList();
          else loadLog();
        },
        dataset: { tab: value },
      },
      label
    );
  }

  const debouncedSearch = debounce(() => {
    memoryState.offset = 0;
    loadList();
  }, 280);

  async function loadList() {
    const params = new URLSearchParams({ limit: String(memoryState.limit), offset: String(memoryState.offset) });
    if (memoryState.query.trim()) params.set("q", memoryState.query.trim());
    if (memoryState.personId) params.set("person_id", memoryState.personId);
    content.replaceChildren(skeleton(3));
    const result = await api.get(`/api/memories?${params.toString()}`);
    memoryState.total = result.total;
    if (!result.items.length) {
      content.replaceChildren(
        emptyState(
          "memory",
          memoryState.query ? "没有匹配的记忆" : "记忆库还是空的",
          memoryState.query ? "换个关键词试试" : "消息积累后会自动抽取，也可以手动添加几条"
        )
      );
      return;
    }
    const list = h("div", { class: "rows" });
    for (const memory of result.items) {
      const person = memoryState.people.find((p) => p.person_id === memory.person_id);
      list.append(memoryRow(memory, person));
    }
    const footer = h(
      "div",
      { class: "toolbar", style: { borderTop: "1px solid var(--border)", borderBottom: "0" } },
      h("span", { class: "muted", style: { fontSize: "12px" } },
        `共 ${fmtNumber(result.total)} 条，当前 ${memoryState.offset + 1}-${memoryState.offset + result.items.length}`),
      h("div", { class: "spacer" }),
      h(
        "button",
        {
          class: "btn btn-sm",
          disabled: memoryState.offset <= 0,
          onclick: () => {
            memoryState.offset = Math.max(0, memoryState.offset - memoryState.limit);
            loadList();
          },
        },
        "上一页"
      ),
      h(
        "button",
        {
          class: "btn btn-sm",
          disabled: memoryState.offset + result.items.length >= result.total,
          onclick: () => {
            memoryState.offset += memoryState.limit;
            loadList();
          },
        },
        "下一页"
      )
    );
    content.replaceChildren(list, footer);
  }

  async function loadLog() {
    content.replaceChildren(skeleton(3));
    const result = await api.get("/api/memory-log?limit=80");
    if (!result.items.length) {
      content.replaceChildren(emptyState("logs", "还没有变更记录", "抽取、归纳和手动修改都会写在这里"));
      return;
    }
    const list = h("div", { class: "rows" });
    for (const entry of result.items) {
      list.append(logRow(entry));
    }
    content.replaceChildren(list);
  }

  function memoryRow(memory, person) {
    const display = person ? person.display_name || person.person_id : memory.person_id;
    const scope = memory.scope_type === "group" ? `群 ${memory.scope_id}` : "全局";
    return h(
      "div",
      { class: "memory" },
      h("div", { class: "avatar" }, avatarText(display)),
      h(
        "div",
        { class: "memory-body" },
        h("div", { class: "memory-text" }, memory.content),
        h(
          "div",
          { class: "memory-meta" },
          h("span", {}, display),
          h("span", { class: "tag accent", style: { height: "18px", padding: "0 7px" } }, categoryName(memory.category)),
          h("span", {}, scope),
          h("span", {}, sourceName(memory.source)),
          h("span", {}, fmtRelative(memory.created_at))
        )
      ),
      h(
        "div",
        { class: "memory-actions" },
        h(
          "button",
          { class: "icon-btn-sm", title: "编辑", "aria-label": "编辑记忆", onclick: () => openMemoryDialog(memory) },
          icon("edit")
        ),
        h(
          "button",
          {
            class: "icon-btn-sm danger",
            title: "删除",
            "aria-label": "删除记忆",
            onclick: () =>
              confirmDialog({
                title: "删除这条记忆？",
                message: `「${memory.content}」\n删除会写入变更日志，可以在「变更日志」里撤销。`,
                confirmText: "删除",
                danger: true,
                onConfirm: async () => {
                  await api.del(`/api/memories/${memory.id}`);
                  toast("已删除，可在变更日志里撤销");
                  if (memoryState.tab === "list") loadList();
                },
              }),
          },
          icon("trash")
        )
      )
    );
  }

  function logRow(entry) {
    const lines = h(
      "div",
      { style: { display: "grid", gap: "3px", marginTop: "6px" } },
      ...entry.before.map((item) =>
        h("div", { class: "memory-meta", style: { margin: 0 } },
          h("span", { class: "tag bad", style: { height: "18px" } }, "-"),
          h("span", {}, item.content))
      ),
      ...entry.after.map((item) =>
        h("div", { class: "memory-meta", style: { margin: 0 } },
          h("span", { class: "tag good", style: { height: "18px" } }, "+"),
          h("span", {}, item.content))
      )
    );
    return h(
      "div",
      { class: "memory" },
      h(
        "div",
        { class: "memory-body" },
        h(
          "div",
          { class: "memory-meta", style: { margin: 0 } },
          h("span", { class: "tag accent" }, ACTION_NAMES[entry.action] || entry.action),
          h("span", {}, entry.person_id || ""),
          h("span", {}, fmtDateTime(entry.created_at)),
          entry.reverted ? h("span", { class: "tag outline" }, "已撤销") : null
        ),
        entry.reason ? h("div", { class: "memory-text", style: { marginTop: "5px" } }, entry.reason) : null,
        lines
      ),
      h(
        "div",
        { class: "memory-actions", style: { opacity: entry.reverted ? "0.5" : "1" } },
        h(
          "button",
          {
            class: "btn btn-sm",
            disabled: entry.reverted || entry.action === "restore",
            onclick: () =>
              confirmDialog({
                title: "撤销这次变更？",
                message: "会把这一条日志里的改动回滚，恢复删除的记忆或撤掉新增的记忆。",
                confirmText: "撤销",
                onConfirm: async () => {
                  await api.post(`/api/memory-log/${entry.id}/restore`);
                  toast("已撤销");
                  loadLog();
                },
              }),
          },
          icon("restore"),
          "撤销"
        )
      )
    );
  }

  async function openMemoryDialog(memory = null) {
    const editing = Boolean(memory);
    const dialog = $("#dialog");
    const personSelect = h(
      "select",
      { class: "select", disabled: editing, "aria-label": "关于谁" },
      ...memoryState.people.map((person) =>
        h(
          "option",
          { value: person.person_id, selected: memory ? memory.person_id === person.person_id : person.person_id === memoryState.personId },
          `${person.display_name || person.person_id} · ${person.person_id}`
        )
      )
    );
    if (!memoryState.people.length) {
      personSelect.append(h("option", { value: "" }, "还没有人物，先让 bot 收到几条消息"));
    }
    const contentInput = h("textarea", {
      class: "textarea",
      rows: "3",
      maxlength: "500",
      placeholder: "一句关于这个人的耐久事实，例如：TA 在杭州做后端，周末爬宝石山。",
      value: memory ? memory.content : "",
    });
    const category = h(
      "select",
      { class: "select" },
      ...Object.entries(CATEGORY_NAMES).map(([value, label]) =>
        h("option", { value, selected: memory ? memory.category === value : value === "fact" }, label)
      )
    );
    const scope = h("select", { class: "select", disabled: editing },
      h("option", { value: "global", selected: !memory || memory.scope_type === "global" }, "全局（跨群可见）"),
      h("option", { value: "group", selected: Boolean(memory && memory.scope_type === "group") }, "仅某个群")
    );
    const scopeId = h("input", { class: "input", placeholder: "群号", value: memory && memory.scope_id ? memory.scope_id : "" });
    const scopeWrap = h("div", { class: "field", hidden: !memory || memory.scope_type !== "group" },
      h("span", { class: "field-label" }, "群号"), scopeId);
    scope.addEventListener("change", () => {
      scopeWrap.hidden = scope.value !== "group";
    });
    const error = h("p", { class: "form-error", role: "alert" });

    const submit = h("button", { class: "btn btn-primary", type: "submit" }, editing ? "保存" : "添加");
    const form = h(
      "form",
      { style: { display: "contents" } },
      h("div", { class: "dialog-head" },
        h("h2", {}, editing ? "编辑记忆" : "新增记忆"),
        h("button", { class: "icon-button", type: "button", "aria-label": "关闭", onclick: () => dialog.close() }, icon("close"))),
      h(
        "div",
        { class: "dialog-body" },
        editing
          ? h("div", { class: "notice" }, icon("memory"), "来源与归属人不可改；需要换人就删掉重新添加，删除可在变更日志撤销。")
          : null,
        h("div", { class: "field" }, h("span", { class: "field-label" }, "关于谁"), personSelect),
        h("div", { class: "field" }, h("span", { class: "field-label" }, "内容"), contentInput,
          h("span", { class: "field-hint" }, "写耐久事实，不写一次性的请求或临时情绪。")),
        h("div", { class: "grid", style: { gridTemplateColumns: "1fr 1fr" } },
          h("div", { class: "field" }, h("span", { class: "field-label" }, "类别"), category),
          h("div", { class: "field" }, h("span", { class: "field-label" }, "范围"), scope)),
        scopeWrap,
        error
      ),
      h("div", { class: "dialog-foot" },
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => dialog.close() }, "取消"),
        submit)
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      const payload = {
        person_id: personSelect.value,
        content: contentInput.value.trim(),
        category: category.value,
        scope_type: scope.value,
        scope_id: scope.value === "group" ? scopeId.value.trim() : "",
      };
      if (!payload.person_id) return (error.textContent = "先选一个人物");
      if (!payload.content) return (error.textContent = "内容不能为空");
      if (payload.scope_type === "group" && !payload.scope_id) return (error.textContent = "请填写群号");
      submit.disabled = true;
      try {
        if (editing) {
          await api.put(`/api/memories/${memory.id}`, payload);
          toast("已保存");
        } else {
          await api.post("/api/memories", payload);
          toast("已添加");
        }
        dialog.close();
        if (memoryState.tab === "list") loadList();
      } catch (err) {
        if (!(err instanceof Unauthorized)) error.textContent = err.message || "保存失败";
      } finally {
        submit.disabled = false;
      }
    });
    dialog.replaceChildren(form);
    dialog.showModal();
    contentInput.focus();
  }

  if (memoryState.tab === "list") await loadList();
  else await loadLog();
}

/* ------------------------------------------------------------------ */
/* 人物                                                               */
/* ------------------------------------------------------------------ */

async function renderPeople(view, param) {
  const peopleData = await api.get("/api/people?limit=500");
  const people = peopleData.items;
  const searchBox = h(
    "div",
    { class: "input-search", style: { maxWidth: "none", width: "100%" } },
    icon("search"),
    h("input", {
      class: "input",
      type: "search",
      placeholder: "搜索昵称或 ID",
      "aria-label": "搜索人物",
      oninput: (event) => filter(event.target.value),
    })
  );

  const listNode = h("div", { class: "rows side-list" });
  const detailNode = h("div", { class: "card", id: "person-detail" });

  const listCard = h(
    "section",
    { class: "card" },
    h("div", { class: "toolbar", style: { borderBottom: "1px solid var(--border)" } }, searchBox),
    listNode
  );

  function filter(keyword) {
    const q = keyword.trim().toLowerCase();
    const items = q
      ? people.filter(
          (p) =>
            (p.display_name || "").toLowerCase().includes(q) ||
            p.person_id.toLowerCase().includes(q) ||
            p.platform_uid.toLowerCase().includes(q)
        )
      : people;
    renderList(items);
  }

  function renderList(items, selectedId = param) {
    if (!items.length) {
      listNode.replaceChildren(emptyState("people", "没有人物", people.length ? "换个关键词试试" : "收到消息后会自动建档"));
      return;
    }
    listNode.replaceChildren(
      ...items.map((person) =>
        h(
          "div",
          {
            class: `row ${person.person_id === decodeURIComponent(selectedId || "") ? "selected" : ""}`,
            tabindex: "0",
            role: "button",
            onclick: () => navigate("people", encodeURIComponent(person.person_id)),
            onkeydown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                navigate("people", encodeURIComponent(person.person_id));
              }
            },
          },
          h("div", { class: "avatar" }, avatarText(person.display_name)),
          h("div", { class: "row-main" },
            h("div", { class: "row-title" }, person.display_name || person.platform_uid),
            h("div", { class: "row-sub" }, `${platformName(person.platform)} · ${person.memory_count} 条记忆`))
        )
      )
    );
  }

  async function loadDetail(personId, { scrollTop = false } = {}) {
    detailNode.replaceChildren(skeleton(3));
    const encoded = encodeURIComponent(personId);
    const data = await api.get(`/api/people/${encoded}`);
    const person = data.person;
    const memories = data.memories;
    const globals = memories.filter((m) => m.scope_type === "global").length;
    detailNode.replaceChildren(
      h(
        "div",
        { class: "person-head" },
        h("div", { class: "avatar lg" }, avatarText(person.display_name)),
        h(
          "div",
          { style: { flex: "1", minWidth: "0" } },
          h("div", { class: "row-title", style: { fontSize: "16px" } }, person.display_name || person.platform_uid),
          h("div", { class: "row-sub" }, `${platformName(person.platform)} · ${person.person_id}`)
        ),
        h("span", { class: "tag accent" }, `${memories.length} 条记忆`)
      ),
      h(
        "div",
        { class: "person-stats" },
        h("div", {}, h("span", { class: "label" }, "首次出现"), h("span", { class: "value" }, fmtDateTime(person.first_seen))),
        h("div", {}, h("span", { class: "label" }, "最近出现"), h("span", { class: "value" }, fmtRelative(person.last_seen))),
        h("div", {}, h("span", { class: "label" }, "全局 / 群记忆"), h("span", { class: "value" }, `${globals} / ${memories.length - globals}`))
      ),
      h("div", { style: { borderTop: "1px solid var(--border)" } },
        memories.length
          ? h("div", { class: "rows" }, ...memories.map((memory) =>
              h(
                "div",
                { class: "memory" },
                h(
                  "div",
                  { class: "memory-body" },
                  h("div", { class: "memory-text" }, memory.content),
                  h(
                    "div",
                    { class: "memory-meta" },
                    h("span", { class: "tag accent", style: { height: "18px" } }, categoryName(memory.category)),
                    h("span", {}, memory.scope_type === "group" ? `群 ${memory.scope_id}` : "全局"),
                    h("span", {}, sourceName(memory.source)),
                    h("span", {}, fmtRelative(memory.created_at))
                  )
                )
              )
            ))
          : emptyState("memory", "还没有关于这个人的记忆", "消息积累一段后会自动抽取")
      )
    );
  }

  if (!people.length) {
    view.replaceChildren(
      h("section", { class: "card" }, emptyState("people", "还没有见过任何人", "适配器收到消息后，这里会自动建立人物档案"))
    );
    return;
  }

  let target = param ? decodeURIComponent(param) : "";
  // 没有指定就选第一个；不改地址栏，保持 #/people 方便点第二次刷新
  if (!target) target = people[0].person_id;
  renderList(people, target);
  view.replaceChildren(h("div", { class: "split" }, listCard, detailNode));
  await loadDetail(target);
}

/* ------------------------------------------------------------------ */
/* 群组与意愿                                                         */
/* ------------------------------------------------------------------ */

async function renderGroups(view) {
  const load = async () => {
    const [chatsData, configData] = await Promise.all([api.get("/api/chats?limit=300"), api.get("/api/config")]);
    const configured = (configData.effective.decision && configData.effective.decision.groups) || {};
    const chats = chatsData.items.slice();
    for (const chatId of Object.keys(configured)) {
      if (!chats.some((chat) => String(chat.chat_id) === chatId)) {
        chats.push({
          platform: "—",
          chat_id: chatId,
          chat_type: "group",
          last_ts: null,
          message_count: 0,
          configured: true,
          activity_multiplier: configured[chatId].activity_multiplier || 1,
          quiet_hours: configured[chatId].quiet_hours || [],
          recent_decisions: [],
        });
      }
    }
    if (!chats.length) {
      view.replaceChildren(
        h("section", { class: "card" },
          emptyState("groups", "还没有群聊记录", "群消息进来后会自动出现在这里，可以单独调节每群的活跃度与静默时段"))
      );
      return;
    }
    const list = h("div", { class: "rows" });
    for (const chat of chats) list.append(await chatRow(chat));
    view.replaceChildren(
      h("div", { class: "notice" }, icon("warning"),
        "活跃度倍率会放大或压低这个群的意愿分数；静默时段内普通消息不会开口（@、回复和叫名字不受影响）。保存后写入 config.yaml，即时生效。"),
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "会话"), h("span", { class: "hint" }, `${chats.length} 个`)),
        list)
    );
  };

  async function chatRow(chat) {
    const chatId = String(chat.chat_id);
    const detail = h("div", { class: "chat-detail", hidden: true });
    const isGroup = chat.chat_type === "group";
    const head = h(
      "div",
      { class: "chat-row" },
      h(
        "button",
        {
          class: "chat-open",
          type: "button",
          "aria-expanded": "false",
          onclick: async (event) => {
            const button = event.currentTarget;
            const open = detail.hidden;
            detail.hidden = !open;
            button.setAttribute("aria-expanded", open ? "true" : "false");
            if (open && !detail.dataset.loaded) {
              detail.dataset.loaded = "1";
              await fillDetail(detail, chat);
            }
          },
        },
        h("div", { class: "row-title" }, isGroup ? `群 ${chatId}` : `私聊 ${chatId}`),
        h("div", { class: "row-sub" },
          `${platformName(chat.platform)} · ${fmtNumber(chat.message_count)} 条消息 · 最近活跃 ${fmtRelative(chat.last_ts)}`)
      ),
      h(
        "div",
        { class: "chat-settings" },
        chat.quiet_hours && chat.quiet_hours.length
          ? h("span", { class: "tag outline" }, "静默 ", chat.quiet_hours.join(" / "))
          : null,
        h("span", { class: "tag" }, `倍率 ${(Number(chat.activity_multiplier) || 1).toFixed(2)}`),
        h("span", { class: `tag ${chat.configured ? "accent" : "outline"}` }, chat.configured ? "已单独配置" : "默认")
      ),
      detail
    );
    return head;
  }

  if (state.param) {
    // 支持 #/groups/<chat_id> 直接展开
  }

  await load();
}

async function fillDetail(container, chat) {
  const chatId = String(chat.chat_id);
  container.replaceChildren(skeleton(2));
  const encoded = encodeURIComponent(chatId);
  const [settings, decisions] = await Promise.all([
    api.get(`/api/chats/${encoded}/decision`),
    api.get(`/api/decisions?chat_id=${encoded}&limit=12`),
  ]);

  const multiplier = h("input", {
    class: "input",
    type: "number",
    min: "0",
    max: "10",
    step: "0.05",
    value: String(settings.override.activity_multiplier ?? 1),
  });
  const base = h("input", {
    class: "input",
    type: "number",
    min: "0",
    max: "1",
    step: "0.01",
    placeholder: `默认 ${settings.defaults.base}`,
    value: settings.override.base === null || settings.override.base === undefined ? "" : String(settings.override.base),
  });
  const quietChips = h("div", { class: "quiet-chips" });
  const quietInput = h("input", {
    class: "input",
    placeholder: "23:00-07:30",
    style: { width: "130px", height: "26px", fontSize: "12px", fontFamily: "var(--font-mono)" },
    "aria-label": "添加静默时段",
  });
  let quiet = settings.override.quiet_hours.slice();

  function renderChips() {
    quietChips.replaceChildren(
      ...quiet.map((item) =>
        h(
          "span",
          { class: "chip" },
          item,
          h(
            "button",
            {
              type: "button",
              "aria-label": `移除 ${item}`,
              onclick: () => {
                quiet = quiet.filter((value) => value !== item);
                renderChips();
              },
            },
            icon("close")
          )
        )
      ),
      quietInput,
      h(
        "button",
        {
          class: "btn btn-sm btn-ghost",
          type: "button",
          onclick: () => {
            const value = quietInput.value.trim();
            if (!/^\d{1,2}:\d{2}-\d{1,2}:\d{2}$/.test(value)) {
              toast("格式应为 HH:MM-HH:MM，例如 23:00-07:30", "error");
              return;
            }
            if (!quiet.includes(value)) quiet.push(value);
            quietInput.value = "";
            renderChips();
          },
        },
        icon("plus"),
        "添加"
      )
    );
  }
  renderChips();

  const form = h(
    "div",
    { class: "chat-form" },
    h("div", { class: "field" }, h("span", { class: "field-label" }, "活跃度倍率"), multiplier),
    h("div", { class: "field" }, h("span", { class: "field-label" }, "基础意愿（留空用默认）"), base),
    h("div", { class: "field field-wide" }, h("span", { class: "field-label" }, "静默时段"), quietChips),
    h(
      "div",
      { style: { display: "flex", gap: "8px" } },
      h(
        "button",
        {
          class: "btn btn-primary",
          type: "button",
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            try {
              const payload = {
                activity_multiplier: Number(multiplier.value || 1),
                base: base.value === "" ? null : Number(base.value),
                quiet_hours: quiet,
              };
              await api.put(`/api/chats/${encoded}/decision`, payload);
              toast("已保存，即时生效");
              renderRoute();
            } catch (err) {
              if (!(err instanceof Unauthorized)) toast(err.message || "保存失败", "error");
            } finally {
              button.disabled = false;
            }
          },
        },
        "保存"
      ),
      settings.configured
        ? h(
            "button",
            {
              class: "btn btn-danger",
              type: "button",
              onclick: () =>
                confirmDialog({
                  title: "恢复默认设置？",
                  message: "会删掉这个群在 config.yaml 里的单独配置，回到全局默认值。",
                  confirmText: "恢复默认",
                  danger: true,
                  onConfirm: async () => {
                    await api.put(`/api/chats/${encoded}/decision`, { remove: true });
                    toast("已恢复默认");
                    renderRoute();
                  },
                }),
            },
            "恢复默认"
          )
        : null
    )
  );

  const decisionList = decisions.items.length
    ? h("div", { class: "rows", style: { border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden" } },
        ...decisions.items.map((d) => decisionItem(d)))
    : emptyState("pulse", "这个群还没有决策记录", "收到普通群消息并完成评分后会出现");

  container.replaceChildren(
    h(
      "div",
      { class: "grid", style: { gridTemplateColumns: "minmax(0, 1fr)", gap: "12px" } },
      h(
        "div",
        { class: "notice good" },
        icon("pulse"),
        h(
          "span",
          {},
          `当前生效：基础 ${settings.effective.base} · 阈值 ${settings.effective.threshold} · 倍率 ${settings.effective.activity_multiplier}`
        )
      ),
      form,
      h("div", { class: "mutted" }),
      h("div", { class: "field-label" }, "最近决策"),
      decisionList
    )
  );
}

/* ------------------------------------------------------------------ */
/* 插件                                                               */
/* ------------------------------------------------------------------ */

async function renderPlugins(view) {
  const load = async () => {
    const data = await api.get("/api/plugins");
    if (!data.items.length) {
      view.replaceChildren(
        h("section", { class: "card" }, emptyState("plugins", "没有发现插件", "在 plugins/ 目录下放 <名称>/plugin.py 即可"))
      );
      return;
    }
    const list = h("div", { class: "rows" });
    for (const plugin of data.items) {
      const toggle = h("button", {
        class: "switch",
        type: "button",
        role: "switch",
        "aria-checked": plugin.enabled ? "true" : "false",
        "aria-label": `${plugin.enabled ? "禁用" : "启用"}插件 ${plugin.name}`,
      });
      toggle.addEventListener("click", async () => {
        const next = toggle.getAttribute("aria-checked") !== "true";
        toggle.disabled = true;
        try {
          const result = await api.post(`/api/plugins/${encodeURIComponent(plugin.name)}`, { enabled: next });
          toast(result.message || `插件 ${plugin.name} 已${next ? "启用" : "禁用"}`);
          load();
        } catch (err) {
          if (!(err instanceof Unauthorized)) toast(err.message || "操作失败", "error");
          toggle.disabled = false;
        }
      });
      const stateText = plugin.enabled ? (plugin.loaded ? "启用中" : "加载失败") : "已禁用";
      const tagClass = plugin.enabled && plugin.loaded ? "tag good" : "tag";
      const hooks = plugin.hooks || {};
      const hookText = [
        hooks.on_message ? `on_message ${hooks.on_message}` : "",
        hooks.on_before_reply ? `on_before_reply ${hooks.on_before_reply}` : "",
        hooks.on_after_reply ? `on_after_reply ${hooks.on_after_reply}` : "",
      ].filter(Boolean);
      list.append(
        h(
          "div",
          { class: "plugin-row" },
          h("div", { class: "plugin-icon" }, icon("plugins")),
          h(
            "div",
            { class: "row-main" },
            h("div", { class: "row-title" }, plugin.name),
            h(
              "div",
              { class: "row-sub", title: plugin.path },
              plugin.commands.length ? `命令 ${plugin.commands.map((c) => "/" + c).join(" ")}` : "无命令",
              hookText.length ? ` · ${hookText.join("，")}` : " · 无钩子"
            )
          ),
          h("div", { class: "row-side" },
            h("span", { class: tagClass }, stateText),
            toggle)
        )
      );
    }
    const headHint = data.hot_reload === false
      ? "插件默认全部关闭；当前 WebUI 没有运行中的 bot，开关会写入配置，重启 RubenBot 后生效"
      : "插件默认全部关闭；开关立即生效，并写回 config.yaml 的 plugins.enabled";
    view.replaceChildren(
      h("section", { class: "card" },
        h("div", { class: "card-head" },
          h("h2", {}, "插件"),
          h("span", { class: "hint" }, headHint)),
        list)
    );
  };
  await load();
}

/* ------------------------------------------------------------------ */
/* 日志                                                               */
/* ------------------------------------------------------------------ */

async function renderLogs(view) {
  let level = "";
  let query = "";
  let following = true;

  const content = h("div", { class: "logs", id: "log-content" });
  const levelSelect = h(
    "select",
    {
      class: "select",
      style: { width: "auto" },
      "aria-label": "日志级别",
      onchange: (event) => {
        level = event.target.value;
        load();
      },
    },
    h("option", { value: "" }, "全部级别"),
    h("option", { value: "INFO" }, "INFO 及以上"),
    h("option", { value: "WARNING" }, "WARNING 及以上"),
    h("option", { value: "ERROR" }, "ERROR 及以上")
  );
  const follow = h(
    "button",
    { class: "btn btn-sm", type: "button", "aria-pressed": "true" },
    icon("pulse"),
    "自动刷新"
  );
  follow.addEventListener("click", () => {
    following = !following;
    follow.setAttribute("aria-pressed", following ? "true" : "false");
    follow.classList.toggle("btn-primary", following);
    if (following) load();
  });
  follow.classList.add("btn-primary");

  const toolbar = h(
    "div",
    { class: "toolbar" },
    levelSelect,
    h(
      "div",
      { class: "input-search" },
      icon("search"),
      h("input", {
        class: "input",
        type: "search",
        placeholder: "过滤关键字",
        "aria-label": "过滤日志",
        oninput: (event) => {
          query = event.target.value;
          debounced();
        },
      })
    ),
    h("div", { class: "spacer" }),
    follow,
    h("button", { class: "btn btn-sm", type: "button", onclick: () => load() }, icon("refresh"), "刷新")
  );
  const debounced = debounce(() => load(), 300);

  async function load() {
    const params = new URLSearchParams({ limit: "400" });
    if (level) params.set("level", level);
    if (query.trim()) params.set("q", query.trim());
    const data = await api.get(`/api/logs?${params.toString()}`);
    if (!data.items.length) {
      content.replaceChildren(h("div", { class: "log-empty" }, "还没有日志"));
      return;
    }
    const atBottom = content.scrollTop + content.clientHeight >= content.scrollHeight - 24;
    content.replaceChildren(
      ...data.items.map((item) =>
        h(
          "div",
          { class: "log-line" },
          h("span", { class: "log-time" }, fmtClock(item.ts)),
          h("span", { class: "log-level", dataset: { level: item.level } }, item.level),
          h("span", { class: "log-msg" }, h("b", {}, item.logger), `  ${item.message}`)
        )
      )
    );
    if (atBottom || following) content.scrollTop = 0;
  }

  view.replaceChildren(
    h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", {}, "运行日志"),
        h("span", { class: "hint" }, "进程内最近 1000 条，重启后清空")),
      toolbar,
      content)
  );
  await load();
  onPoll(3000, () => {
    if (following) load().catch(() => {});
  });
}

/* ------------------------------------------------------------------ */
/* 设置                                                               */
/* ------------------------------------------------------------------ */

const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

/** 需要重启才能生效的字段 → 展示名。 */
const RESTART_LABELS = {
  database_path: "数据库路径",
  "persona.path": "人格文件路径",
  "plugins.dir": "插件目录",
  "webui.enabled": "WebUI 启用状态",
  "webui.host": "WebUI 监听地址",
  "webui.port": "WebUI 端口",
};

function restartLabel(field) {
  if (field.startsWith("adapters.")) return `适配器 ${field.slice("adapters.".length)}`;
  return RESTART_LABELS[field] || field;
}

function getPath(obj, path) {
  return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  let node = obj;
  for (const key of keys.slice(0, -1)) {
    if (node[key] === null || typeof node[key] !== "object") node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
}

/** 数字输入保留原始字符串，交给后端统一校验并给出字段错误。 */
function numberField(value, onInput, { step = "any", min, max } = {}) {
  return h("input", {
    class: "input",
    type: "number",
    step,
    min: min === undefined ? null : String(min),
    max: max === undefined ? null : String(max),
    value: value === null || value === undefined ? "" : String(value),
    oninput: (event) => onInput(event.target.value),
  });
}

function textField(value, onInput, { placeholder = "", mono = false } = {}) {
  return h("input", {
    class: `input ${mono ? "mono" : ""}`,
    type: "text",
    value: value === null || value === undefined ? "" : String(value),
    placeholder,
    oninput: (event) => onInput(event.target.value),
  });
}

function switchField(value, onToggle, { label } = {}) {
  const toggle = h("button", {
    class: "switch",
    type: "button",
    role: "switch",
    "aria-checked": value ? "true" : "false",
    "aria-label": label || "开关",
  });
  toggle.addEventListener("click", () => {
    const next = toggle.getAttribute("aria-checked") !== "true";
    toggle.setAttribute("aria-checked", next ? "true" : "false");
    onToggle(next);
  });
  return toggle;
}

function selectField(value, options, onSelect) {
  return h(
    "select",
    { class: "select", onchange: (event) => onSelect(event.target.value) },
    ...options.map((option) =>
      h("option", { value: option.value, selected: String(value) === String(option.value) }, option.label)
    )
  );
}

/** 可增删的字符串列表（别名、静默时段、启用插件）。 */
function listEditor(values, onChange, { placeholder = "输入后回车或点添加", normalize = null } = {}) {
  let items = Array.isArray(values) ? values.slice() : [];
  const chips = h("div", { class: "quiet-chips" });
  const input = h("input", {
    class: "input",
    placeholder,
    style: { width: "180px", height: "28px", fontSize: "12.5px" },
    "aria-label": placeholder,
  });

  function commit(next) {
    items = next;
    onChange(items.slice());
    render();
  }

  function add() {
    let value = input.value.trim();
    if (!value) return;
    if (normalize) value = normalize(value);
    if (!value) {
      toast("格式不对", "error");
      return;
    }
    if (items.includes(value)) {
      input.value = "";
      return;
    }
    input.value = "";
    commit(items.concat(value));
  }

  function render() {
    chips.replaceChildren(
      ...items.map((item) =>
        h(
          "span",
          { class: "chip" },
          item,
          h(
            "button",
            {
              type: "button",
              "aria-label": `移除 ${item}`,
              onclick: () => commit(items.filter((value) => value !== item)),
            },
            icon("close")
          )
        )
      ),
      input,
      h(
        "button",
        { class: "btn btn-sm btn-ghost", type: "button", onclick: add },
        icon("plus"),
        "添加"
      )
    );
  }
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      add();
    }
  });
  render();
  return chips;
}

/** 一个表单项：标签 + 控件 + 说明。 */
function settingsField(def, draft) {
  const value = getPath(draft, def.path);
  const set = (next) => setPath(draft, def.path, next);
  let control;
  if (def.kind === "switch") {
    const stateText = h("span", { class: "field-hint" }, value ? "已启用" : "已停用");
    control = h(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "10px" } },
      switchField(
        Boolean(value),
        (next) => {
          set(next);
          stateText.textContent = next ? "已启用" : "已停用";
        },
        { label: def.label }
      ),
      stateText
    );
  } else if (def.kind === "select") {
    control = selectField(value, def.options, set);
  } else if (def.kind === "number") {
    control = numberField(value, set, { step: def.step, min: def.min, max: def.max });
  } else if (def.kind === "password") {
    control = h("input", {
      class: "input",
      type: "password",
      autocomplete: "new-password",
      placeholder: def.secretSet ? "已设置，留空不改" : "尚未设置",
      oninput: (event) => set(event.target.value),
    });
  } else if (def.kind === "list") {
    control = listEditor(value || [], set, { placeholder: def.placeholder, normalize: def.normalize });
  } else {
    control = textField(value, set, { placeholder: def.placeholder, mono: def.mono });
  }
  return h(
    "div",
    { class: `settings-field ${def.wide ? "wide" : ""}` },
    h("span", { class: "field-label" }, def.label),
    control,
    def.hint ? h("span", { class: "field-hint" }, def.hint) : null,
    def.restart ? h("span", { class: "restart-note" }, icon("warning"), "重启后生效") : null
  );
}

function fieldGrid(defs, draft) {
  return h(
    "div",
    { class: "settings-fields" },
    ...defs.map((def) => settingsField(def, draft))
  );
}

function quietHoursNormalize(value) {
  return /^\d{1,2}:\d{2}-\d{1,2}:\d{2}$/.test(value) ? value : "";
}

/** 每群覆盖：增删群条目，每条可设倍率、基础意愿与静默时段。 */
function groupsEditor(draft, secrets) {
  const body = h("div", { style: { display: "grid", gap: "12px" } });
  const groupsNode = h("div", { class: "group-list" });
  const newId = h("input", {
    class: "input",
    placeholder: "要单独配置的群号",
    style: { maxWidth: "200px" },
    "aria-label": "要单独配置的群号",
  });

  function groups() {
    if (!draft.decision.groups || typeof draft.decision.groups !== "object") {
      draft.decision.groups = {};
    }
    return draft.decision.groups;
  }

  function render() {
    const entries = Object.keys(groups());
    groupsNode.replaceChildren(
      ...(entries.length
        ? entries.map((chatId) => groupRow(chatId))
        : [h("div", { class: "field-hint" }, "还没有单独配置的群。默认所有群使用上面的全局参数。")])
    );
  }

  function groupRow(chatId) {
    const settings = groups()[chatId];
    if (!settings) return h("div");
    return h(
      "div",
      { class: "group-item" },
      h(
        "div",
        { class: "group-item-head" },
        h("span", { class: "row-title" }, `群 ${chatId}`),
        h(
          "button",
          {
            class: "icon-btn-sm danger",
            type: "button",
            title: "删除这个群的覆盖",
            "aria-label": `删除群 ${chatId} 的覆盖`,
            onclick: () => {
              delete groups()[chatId];
              render();
            },
          },
          icon("trash")
        )
      ),
      h(
        "div",
        { class: "settings-fields" },
        h(
          "div",
          { class: "settings-field" },
          h("span", { class: "field-label" }, "活跃度倍率"),
          numberField(settings.activity_multiplier ?? 1, (value) => {
            settings.activity_multiplier = value;
          }, { step: "0.05", min: 0, max: 10 }),
          h("span", { class: "field-hint" }, "放大或压低这个群的意愿分数（0–10）")
        ),
        h(
          "div",
          { class: "settings-field" },
          h("span", { class: "field-label" }, "基础意愿（留空用全局）"),
          numberField(settings.base, (value) => {
            settings.base = value === "" ? null : value;
          }, { step: "0.01", min: 0, max: 1 }),
          h("span", { class: "field-hint" }, "覆盖全局 base，留空表示跟随")
        ),
        h(
          "div",
          { class: "settings-field wide" },
          h("span", { class: "field-label" }, "静默时段"),
          listEditor(settings.quiet_hours || [], (values) => {
            settings.quiet_hours = values;
          }, { placeholder: "23:00-07:30", normalize: quietHoursNormalize }),
          h("span", { class: "field-hint" }, "这个时段内普通消息不开口；@、回复和叫名字不受影响")
        )
      )
    );
  }

  const addButton = h(
    "button",
    {
      class: "btn btn-sm",
      type: "button",
      onclick: () => {
        const chatId = newId.value.trim();
        if (!chatId) return;
        if (groups()[chatId]) {
          toast("这个群已经有单独配置", "error");
          return;
        }
        groups()[chatId] = { activity_multiplier: 1, base: null, quiet_hours: [] };
        newId.value = "";
        render();
      },
    },
    icon("plus"),
    "添加群"
  );
  newId.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addButton.click();
    }
  });

  render();
  body.append(
    groupsNode,
    h("div", { style: { display: "flex", gap: "8px", alignItems: "center" } }, newId, addButton)
  );
  return body;
}

function adaptersEditor(draft, secrets) {
  const adapters = draft.adapters || (draft.adapters = {});
  const consoleCfg = adapters.console || (adapters.console = { enabled: false });
  const snow = adapters.snowluma || (adapters.snowluma = {});

  const snowFields = h(
    "div",
    { class: "settings-fields", style: { marginTop: "6px" } },
    settingsField(
      { path: "adapters.snowluma.ws_url", label: "WebSocket 地址", hint: "OneBot v11 正向 WS，例如 ws://127.0.0.1:3001" },
      draft
    ),
    settingsField(
      { path: "adapters.snowluma.http_url", label: "HTTP 地址", hint: "发送消息用的 HTTP API，例如 http://127.0.0.1:3000" },
      draft
    ),
    settingsField(
      {
        path: "adapters.snowluma.access_token",
        label: "Access Token",
        kind: "password",
        hint: "OneBot 的鉴权 token，留空表示不鉴权",
        secretSet: Boolean(secrets["adapters.snowluma.access_token"]),
      },
      draft
    ),
    settingsField(
      { path: "adapters.snowluma.self_id", label: "Bot 的 QQ 号", hint: "可选，留空则从事件里自动获取" },
      draft
    )
  );
  snowFields.hidden = !snow.enabled;

  return h(
    "div",
    { style: { display: "grid", gap: "12px" } },
    h(
      "div",
      { class: "plugin-row", style: { padding: "0", borderTop: "0" } },
      h("div", { class: "plugin-icon" }, icon("plugins")),
      h(
        "div",
        { class: "row-main" },
        h("div", { class: "row-title" }, "Console（终端调试）"),
        h("div", { class: "row-sub" }, "从 stdin 读消息、往 stdout 回复；改配置后需重启")
      ),
      switchField(Boolean(consoleCfg.enabled), (value) => {
        consoleCfg.enabled = value;
      }, { label: "启用 Console 适配器" })
    ),
    h(
      "div",
      { class: "plugin-row", style: { padding: "0", borderTop: "1px solid var(--border)" } },
      h("div", { class: "plugin-icon" }, icon("pulse")),
      h(
        "div",
        { class: "row-main" },
        h("div", { class: "row-title" }, "SnowLuma（OneBot v11）"),
        h("div", { class: "row-sub" }, "正向 WS 收事件、HTTP API 发消息；连接参数改动需重启")
      ),
      switchField(Boolean(snow.enabled), (value) => {
        snow.enabled = value;
        snowFields.hidden = !value;
      }, { label: "启用 SnowLuma 适配器" })
    ),
    snowFields
  );
}

/** 插件开关：默认全关，逐个显式启用；保存后热生效并写回 plugins.enabled。 */
function pluginEditor(draft, discovered = []) {
  const plugins = draft.plugins || (draft.plugins = {});
  let enabled = Array.isArray(plugins.enabled) ? plugins.enabled.slice() : [];
  plugins.enabled = enabled;

  const setEnabled = (next) => {
    // 去重、去空白；显式启用制下空列表就是「全部关闭」
    enabled = [...new Set(next.map((name) => String(name).trim()).filter(Boolean))];
    plugins.enabled = enabled;
  };

  const discoveredNames = discovered.map((plugin) => plugin.name);
  // 配置里有、目录里没找到的名字：保留但单独列出，避免保存时被悄悄丢掉
  const orphans = enabled.filter((name) => !discoveredNames.includes(name));

  const rows = h(
    "div",
    { class: "rows", style: { border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden" } },
    ...discovered.map((plugin) => {
      const status = h("div", { class: "row-sub", title: plugin.path }, plugin.enabled ? "当前已启用" : "当前未启用");
      return h(
        "div",
        { class: "plugin-row", style: { padding: "10px 14px" } },
        h("div", { class: "plugin-icon" }, icon("plugins")),
        h(
          "div",
          { class: "row-main" },
          h("div", { class: "row-title" }, plugin.name),
          status
        ),
        h(
          "div",
          { class: "row-side" },
          switchField(enabled.includes(plugin.name), (value) => {
            setEnabled(value ? enabled.concat(plugin.name) : enabled.filter((name) => name !== plugin.name));
            status.textContent = value ? "保存后启用" : "保存后停用";
          }, { label: `启用插件 ${plugin.name}` })
        )
      );
    })
  );

  return h(
    "div",
    { style: { display: "grid", gap: "12px" } },
    h(
      "div",
      { class: "settings-fields" },
      settingsField(
        { path: "plugins.dir", label: "插件目录", hint: "扫描 <目录>/<插件名>/plugin.py；改动需重启", restart: true },
        draft
      ),
      settingsField(
        { path: "plugins.command_prefix", label: "命令前缀", hint: "例如 /，留空表示不处理命令" },
        draft
      )
    ),
    h(
      "span",
      { class: "field-hint" },
      discovered.length
        ? "插件默认全部关闭；打开开关后保存即热生效（立即加载或停用），无需重启。"
        : "插件目录里还没有发现插件；放入 plugins/<名称>/plugin.py 后刷新页面即可在这里启用。"
    ),
    rows,
    orphans.length
      ? h(
          "div",
          { class: "settings-field" },
          h("span", { class: "field-label" }, "配置里启用但目录中未找到"),
          listEditor(orphans, (values) => {
            setEnabled(enabled.filter((name) => discoveredNames.includes(name)).concat(values));
          }, { placeholder: "插件名", normalize: (v) => v.trim() }),
          h("span", { class: "field-hint" }, "这些名字写在 plugins.enabled 里，但插件目录下没有对应的 plugin.py")
        )
      : null
  );
}

function resultNotice(result, onRestart) {
  const applied = result.applied || [];
  const restart = result.requires_restart || [];
  return h(
    "div",
    { class: "notice good settings-result" },
    icon("check"),
    h(
      "div",
      { style: { display: "grid", gap: "6px", flex: "1" } },
      h("strong", {}, "配置已保存"),
      applied.length
        ? h("div", { class: "result-line" },
            h("span", { class: "tag good" }, "已立即生效"),
            h("span", {}, applied.map(groupName).join("、")))
        : null,
      restart.length
        ? h("div", { class: "result-line" },
            h("span", { class: "tag warn" }, "需重启"),
            h("span", {}, restart.map(restartLabel).join("、")))
        : !applied.length
        ? h("div", { class: "result-line" }, h("span", {}, "与当前配置一致，无需变更"))
        : null
    ),
    restart.length
      ? h("button", { class: "btn btn-sm", type: "button", onclick: onRestart }, icon("refresh"), "立即重启")
      : null
  );
}

function groupName(key) {
  return {
    llm: "模型与接口",
    memory: "记忆",
    chat: "聊天与回复",
    decision: "回复意愿",
    plugins: "插件",
    webui: "WebUI",
    persona: "人格",
    adapters: "适配器",
    log_level: "日志级别",
    background_check_minutes: "后台检查",
    database_path: "数据库路径",
  }[key] || key;
}

function restartOverlay() {
  let overlay = $(".restart-overlay");
  if (overlay) return overlay;
  overlay = h(
    "div",
    { class: "restart-overlay" },
    h(
      "div",
      { class: "restart-card" },
      h("div", { class: "brand-mark" }, "R"),
      h("strong", {}, "正在重启 RubenBot…"),
      h("p", { class: "field-hint", style: { margin: 0 } }, "进程恢复后页面会自动刷新，需要重新登录。"),
      h("div", { class: "restart-spinner" })
    )
  );
  document.body.append(overlay);
  return overlay;
}

async function restartAndWait() {
  restartOverlay();
  try {
    await api.post("/api/restart");
  } catch (err) {
    if (err instanceof Unauthorized) return;
    toast(err.message || "重启请求失败", "error");
    const overlay = $(".restart-overlay");
    if (overlay) overlay.remove();
    return;
  }
  // 轮询直到服务恢复（/api/me 不需要登录，重启后一定可达）
  const deadline = Date.now() + 45000;
  const probe = async () => {
    try {
      const response = await fetch("/api/me", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.ok) {
        location.reload();
        return true;
      }
    } catch (err) {
      // 服务还没起来，继续等
    }
    if (Date.now() > deadline) {
      const card = $(".restart-card");
      if (card) {
        card.querySelector("p").textContent = "重启好像没有完成，请手动检查进程或刷新页面。";
      }
      return true;
    }
    return false;
  };
  const tick = async () => {
    if (await probe()) return;
    setTimeout(tick, 900);
  };
  setTimeout(tick, 1200);
}

async function renderSettings(view) {
  const data = await api.get("/api/config");
  const structured = data.structured || {};
  const secrets = structured.secrets || {};
  const draft = JSON.parse(JSON.stringify(structured));
  delete draft.secrets;

  const noticeBox = h("div", { hidden: true });
  const errorBox = h("div", { hidden: true });

  const showResult = (result) => {
    noticeBox.hidden = false;
    noticeBox.replaceChildren(resultNotice(result, () => confirmRestart()));
  };

  function confirmRestart() {
    confirmDialog({
      title: "立即重启 RubenBot？",
      message: "进程会原地重启，正在处理的对话会中断，WebUI 需要重新登录。确定现在重启吗？",
      confirmText: "立即重启",
      danger: true,
      onConfirm: () => restartAndWait(),
    });
  }

  const saveButton = h(
    "button",
    { class: "btn btn-primary", type: "button" },
    icon("check"),
    "保存全部设置"
  );
  const saveBar = h(
    "div",
    { class: "settings-savebar" },
    h(
      "div",
      { class: "row-main" },
      h("div", { class: "row-title" }, "设置"),
      h("div", { class: "row-sub" }, `配置文件：${data.path}`)
    ),
    h("div", { class: "spacer" }),
    h("button", { class: "btn", type: "button", onclick: () => renderRoute() }, icon("refresh"), "放弃修改"),
    saveButton
  );

  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    errorBox.hidden = true;
    noticeBox.hidden = true;
    try {
      const result = await api.put("/api/config", { config: draft });
      toast("配置已保存");
      if (result.relogin) {
        // 口令变了，当前会话立即作废；重新登录后再看结果
        showLogin();
        toast("访问口令已更新，请重新登录", "error");
        return;
      }
      state.settingsNotice = result;
      await renderRoute();
    } catch (err) {
      if (err instanceof Unauthorized) return;
      errorBox.hidden = false;
      const fields = (err.fields || []).map((item) =>
        h("div", { class: "result-line" },
          h("span", { class: "tag bad" }, item.field),
          h("span", {}, item.message))
      );
      errorBox.replaceChildren(
        h("div", { class: "notice" },
          icon("warning"),
          h("div", { style: { display: "grid", gap: "6px", flex: "1" } },
            h("strong", {}, "保存失败，配置没有写入"),
            fields.length ? null : h("div", {}, err.message || "请检查填写内容"),
            ...fields))
      );
      errorBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
      toast("配置校验未通过", "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  const cards = [
    card("模型与接口", {
      hint: "OpenAI 兼容接口",
      body: fieldGrid(
        [
          { path: "llm.base_url", label: "接口地址", hint: "例如 https://api.deepseek.com/v1", wide: true, mono: true },
          {
            path: "llm.api_key",
            label: "API Key",
            kind: "password",
            hint: "留空表示保留原值；也可以写成 ${ENV_VAR} 从环境变量读取",
            secretSet: Boolean(secrets["llm.api_key"]),
          },
          { path: "llm.chat_model", label: "对话模型", hint: "回复消息用的模型", mono: true },
          { path: "llm.extract_model", label: "抽取模型", hint: "记忆抽取用；留空跟随对话模型", mono: true },
          { path: "llm.consolidate_model", label: "归纳模型", hint: "记忆归纳用；留空跟随对话模型", mono: true },
          { path: "llm.temperature", label: "温度", kind: "number", step: "0.05", min: 0, max: 2, hint: "0–2，越大越发散" },
          { path: "llm.timeout", label: "超时（秒）", kind: "number", step: "1", min: 0.1, hint: "单次请求超时" },
          { path: "llm.max_retries", label: "重试次数", kind: "number", step: "1", min: 0, max: 10, hint: "失败后的重试次数" },
        ],
        draft
      ),
    }),
    card("人格与数据", {
      hint: "路径改动需重启",
      body: fieldGrid(
        [
          { path: "persona.path", label: "人格文件", hint: "SOUL.md 风格；路径不变时保存会顺带重读内容", wide: true, mono: true, restart: true },
          { path: "database_path", label: "数据库文件", hint: "SQLite 路径；改动需重启", wide: true, mono: true, restart: true },
        ],
        draft
      ),
    }),
    card("记忆", {
      hint: "抽取与归纳参数",
      body: fieldGrid(
        [
          { path: "memory.extract_every_n", label: "触发抽取的消息数", kind: "number", step: "1", min: 1, hint: "同一会话攒够多少条未抽取消息就触发" },
          { path: "memory.extract_silence_seconds", label: "静默触发（秒）", kind: "number", step: "1", min: 0, hint: "会话静默这么久后触发抽取" },
          { path: "memory.extract_min_pending", label: "单人最少条数", kind: "number", step: "1", min: 1, hint: "一个人至少积累几条才抽取" },
          { path: "memory.extract_window", label: "单次抽取窗口", kind: "number", step: "1", min: 1, hint: "一次最多喂给模型的该人消息数" },
          { path: "memory.inject_limit", label: "回复注入上限", kind: "number", step: "1", min: 0, hint: "回复时最多注入多少条记忆" },
          { path: "memory.dedupe_threshold", label: "去重阈值", kind: "number", step: "0.01", min: 0, max: 1, hint: "与已有记忆的相似度达到多少算重复（0–1）" },
          { path: "memory.consolidate_new_threshold", label: "归纳触发条数", kind: "number", step: "1", min: 1, hint: "新增记忆达到多少条触发归纳" },
          { path: "memory.consolidate_interval_hours", label: "归纳间隔（小时）", kind: "number", step: "0.5", min: 0.1, hint: "距上次归纳超过这么久也会触发" },
          { path: "memory.consolidate_max_memories", label: "归纳输入上限", kind: "number", step: "1", min: 1, hint: "单次最多送多少条记忆给模型" },
          { path: "memory.consolidate_remove_ratio", label: "本轮移除比例", kind: "number", step: "0.01", min: 0, max: 1, hint: "一轮最多移除的比例" },
          { path: "memory.consolidate_remove_min", label: "最少允许移除", kind: "number", step: "1", min: 1, hint: "一轮至少允许移除的条数" },
          { path: "memory.raw_keep_per_chat", label: "原始消息保留", kind: "number", step: "1", min: 1, hint: "每个会话保留多少条原始消息" },
        ],
        draft
      ),
    }),
    card("聊天与回复", {
      hint: "回复长度与上下文",
      body: fieldGrid(
        [
          { path: "chat.bot_name", label: "Bot 名称", hint: "会写进消息流水，也用作默认的叫名硬触发" },
          { path: "chat.history_size", label: "上下文条数", kind: "number", step: "1", min: 1, hint: "拼进提示词的最近消息条数" },
          { path: "chat.split_max_parts", label: "最多拆成几条", kind: "number", step: "1", min: 1, hint: "长回复按句拆条，避免刷屏" },
          { path: "chat.split_part_chars", label: "每段字数上限", kind: "number", step: "1", min: 8, hint: "超过就拆成下一条" },
          { path: "chat.split_delay_seconds", label: "拆条间隔（秒）", kind: "number", step: "0.1", min: 0, hint: "多条消息之间的停顿" },
          { path: "chat.history_ttl_hours", label: "上下文有效期（小时）", kind: "number", step: "0.5", min: 0, hint: "超过这个时间的旧消息不再拼进上下文" },
        ],
        draft
      ),
    }),
    card("回复意愿", {
      hint: "群聊里什么时候开口",
      body: fieldGrid(
        [
          { path: "decision.merge_window_seconds", label: "合并窗口（秒）", kind: "number", step: "0.5", min: 0, hint: "同一人连发的多条先攒着合并成一轮" },
          { path: "decision.hard_merge_window_seconds", label: "硬触发窗口（秒）", kind: "number", step: "0.5", min: 0, hint: "私聊/@/回复/叫名字的短窗口，0 表示立即处理" },
          { path: "decision.aliases", label: "叫名别名", kind: "list", wide: true, placeholder: "例如 小R", hint: "叫到这些名字算硬触发；留空则用 Bot 名称" },
          { path: "decision.base", label: "基础意愿", kind: "number", step: "0.01", min: 0, max: 1, hint: "每个群的基础分，默认低，靠动量和相关性加分" },
          { path: "decision.threshold", label: "开口阈值", kind: "number", step: "0.01", min: 0, max: 1, hint: "分数达到多少才开口" },
          { path: "decision.threshold_noise", label: "阈值随机扰动", kind: "number", step: "0.01", min: 0, max: 1, hint: "阈值附近的随机波动幅度" },
          { path: "decision.threshold_noise_band", label: "扰动生效范围", kind: "number", step: "0.01", min: 0, max: 1, hint: "离阈值多近才摇骰子" },
          { path: "decision.momentum_bonus", label: "动量加成", kind: "number", step: "0.05", min: 0, max: 10, hint: "刚参与过这个会话时的加分" },
          { path: "decision.momentum_half_life_seconds", label: "动量半衰期（秒）", kind: "number", step: "10", min: 1, hint: "动量随时间衰减的速度" },
          { path: "decision.relevance_bonus", label: "相关性加成", kind: "number", step: "0.05", min: 0, max: 10, hint: "记忆检索命中时的最大加分" },
          { path: "decision.relevance_scale", label: "相关性归一尺度", kind: "number", step: "0.5", min: 0.1, hint: "检索分数除以它后封顶" },
          { path: "decision.ratio_window", label: "占比统计窗口", kind: "number", step: "1", min: 1, hint: "统计发言占比时看最近多少条消息" },
          { path: "decision.ratio_threshold", label: "占比阈值", kind: "number", step: "0.01", min: 0, max: 1, hint: "bot 占比超过这个值开始减分" },
          { path: "decision.ratio_silence", label: "占比静默线", kind: "number", step: "0.01", min: 0, max: 1, hint: "占比到这个值直接静默" },
          { path: "decision.ratio_penalty", label: "占比最大减分", kind: "number", step: "0.1", min: 0, max: 10, hint: "占比超阈值时的最大减分" },
          { path: "decision.cooldown_seconds", label: "发言后冷却（秒）", kind: "number", step: "1", min: 0, hint: "发言后这段时间内降低意愿" },
          { path: "decision.cooldown_penalty", label: "冷却最大减分", kind: "number", step: "0.1", min: 0, max: 10, hint: "冷却期内最多减多少分" },
          { path: "decision.veto_marker", label: "不回复标记", hint: "模型输出这个标记就静默丢弃" },
        ],
        draft
      ),
    }),
    card("每群覆盖", {
      hint: "单独调节某个群",
      body: groupsEditor(draft, secrets),
    }),
    card("插件", {
      hint: "默认全关，逐个启用",
      body: pluginEditor(draft, data.plugins_available || []),
    }),
    card("适配器", {
      hint: "连接参数改动需重启",
      body: adaptersEditor(draft, secrets),
    }),
    card("WebUI 与高级", {
      hint: "监听地址与运行参数",
      body: h(
        "div",
        { style: { display: "grid", gap: "16px" } },
        fieldGrid(
          [
            { path: "webui.enabled", label: "启用 WebUI", kind: "switch", hint: "关闭后本次运行仍然可用，重启后不再启动", restart: true },
            { path: "webui.host", label: "监听地址", hint: "默认 127.0.0.1；绑非本地地址必须设置口令", restart: true, mono: true },
            { path: "webui.port", label: "端口", kind: "number", step: "1", min: 1, max: 65535, hint: "改动需重启", restart: true },
            {
              path: "webui.password",
              label: "访问口令",
              kind: "password",
              hint: "留空表示保留原值；绑非本地地址时必须设置",
              secretSet: Boolean(secrets["webui.password"]),
            },
            { path: "webui.session_hours", label: "登录有效期（小时）", kind: "number", step: "1", min: 0.1, hint: "会话 Cookie 的有效时长" },
            { path: "background_check_minutes", label: "后台检查间隔（分钟）", kind: "number", step: "1", min: 0.1, hint: "记忆抽取/归纳的定期检查频率" },
            {
              path: "log_level",
              label: "日志级别",
              kind: "select",
              hint: "立即生效",
              options: LOG_LEVELS.map((value) => ({ value, label: value })),
            },
          ],
          draft
        )
      ),
    }),
  ];

  const themeButton = (value, label) =>
    h("button", { type: "button", "data-theme-choice": value, onclick: () => applyTheme(value) }, label);
  const appearance = card("外观", {
    body: h(
      "div",
      { class: "field" },
      h("span", { class: "field-label" }, "主题"),
      h("div", { class: "segmented", style: { alignSelf: "flex-start" } },
        themeButton("auto", "跟随系统"),
        themeButton("light", "亮色"),
        themeButton("dark", "暗色"))
    ),
  });
  applyTheme(localStorage.getItem("rubenbot.theme") || "auto");

  // 原始 YAML：折叠放在底部，保留旧编辑方式
  const textarea = h("textarea", {
    class: "textarea code",
    rows: "18",
    spellcheck: "false",
    "aria-label": "config.yaml 内容",
    placeholder: "config.yaml 不存在，保存后会创建",
  });
  textarea.value = data.text;
  const yamlNotice = h("div", { hidden: true });
  const yamlSave = h("button", { class: "btn", type: "button" }, icon("check"), "保存原始 YAML");
  yamlSave.addEventListener("click", async () => {
    yamlSave.disabled = true;
    try {
      const result = await api.put("/api/config", { text: textarea.value });
      yamlNotice.hidden = false;
      yamlNotice.replaceChildren(
        h("div", { class: "notice" }, icon("warning"),
          h("span", {}, result.message || "已保存，需要重启后生效。"))
      );
      toast("原始配置已保存");
    } catch (err) {
      if (err instanceof Unauthorized) return;
      yamlNotice.hidden = false;
      yamlNotice.replaceChildren(
        h("div", { class: "notice" }, icon("warning"), h("span", {}, err.message || "保存失败"))
      );
    } finally {
      yamlSave.disabled = false;
    }
  });
  const advanced = h(
    "details",
    { class: "card advanced-yaml" },
    h(
      "summary",
      { class: "card-head" },
      h("h2", {}, "高级：原始 YAML"),
      h("span", { class: "hint" }, "直接编辑 config.yaml；保存后需要重启才生效")
    ),
    h(
      "div",
      { class: "card-body", style: { display: "grid", gap: "12px" } },
      h("div", { class: "notice" }, icon("warning"),
        "这里的修改不做逐字段校验，保存前只检查 YAML 语法并留一份 .bak 备份。"),
      textarea,
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
        yamlSave,
        h("span", { class: "muted", style: { fontSize: "12px" } }, `${data.text.split("\n").length} 行`)),
      yamlNotice
    )
  );

  // 重启提示：磁盘上已有改动但还没重启的字段
  const pending = data.restart_fields || [];
  const pendingCard = pending.length
    ? h(
        "div",
        { class: "notice settings-result" },
        icon("warning"),
        h(
          "div",
          { style: { display: "grid", gap: "6px", flex: "1" } },
          h("strong", {}, "有改动等待重启后生效"),
          h("div", {}, pending.map(restartLabel).join("、"))
        ),
        h("button", { class: "btn btn-sm", type: "button", onclick: () => confirmRestart() },
          icon("refresh"), "立即重启")
      )
    : null;

  if (state.settingsNotice) {
    showResult(state.settingsNotice);
    state.settingsNotice = null;
  }

  view.replaceChildren(
    ...[
      saveBar,
      noticeBox,
      errorBox,
      pendingCard,
      h(
        "div",
        { class: "settings-stack" },
        ...cards,
        h("div", { class: "settings-stack-foot" }, appearance, advanced)
      ),
    ].filter(Boolean)
  );
  applyTheme(localStorage.getItem("rubenbot.theme") || "auto");
}

/* ------------------------------------------------------------------ */
/* 启动                                                               */
/* ------------------------------------------------------------------ */

async function boot() {
  if (state.booted) {
    await renderRoute();
    return;
  }
  state.booted = true;
  hideLogin();
  await renderRoute();
  pollHealth();
  setInterval(pollHealth, 15000);
}

async function main() {
  setupTheme();
  setupLogin();
  window.addEventListener("hashchange", () => {
    if (!$("#app").hidden) renderRoute();
  });
  let me = null;
  try {
    me = await api.get("/api/me");
  } catch (err) {
    if (!(err instanceof Unauthorized)) {
      // 服务没起来时给出提示，但仍显示登录/界面
      toast("无法连接 WebUI 服务", "error");
    }
  }
  if (me && me.auth_required && !me.authenticated) {
    showLogin();
    return;
  }
  hideLogin();
  await boot();
}

main();

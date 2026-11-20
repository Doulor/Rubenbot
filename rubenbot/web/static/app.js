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
    node.classList.add("is-leaving");
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
  bot: { title: "机器人", sub: "人设、聊天、表达、记忆、情绪、语音与表情", render: renderBot },
  // 旧链接 #/persona 仍然打开人设编辑器（已并入「机器人」选项卡）
  persona: { title: "机器人", sub: "人设", render: renderLegacyPersona },
  memory: { title: "记忆", sub: "按人浏览、搜索与维护记忆库", render: renderMemory },
  people: { title: "人物", sub: "见过的每个人和他们的档案", render: renderPeople },
  groups: { title: "群组与意愿", sub: "每群活跃度、静默时段与决策记录", render: renderGroups },
  plugins: { title: "插件市场", sub: "已装与可装同列表；安装后默认关闭，手动启用才加载", render: renderPlugins },
  logs: { title: "日志", sub: "最近的运行日志", render: renderLogs },
  settings: { title: "设置", sub: "模型、搜索、主动触达、适配器与运行环境", render: renderSettings },
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
  const view = $("#view");
  const routeChanged = route.page !== state.page || route.param !== state.param;
  const subpage = routeChanged && route.page === state.page && ["bot", "settings"].includes(route.page);
  markRouteChange(view, routeChanged);
  view.classList.toggle("subpage-motion", subpage);
  view.classList.remove("route-arriving");
  state.page = route.page;
  state.param = route.param;
  // 旧路由 #/persona 已并入机器人选项卡，侧栏高亮跟着「机器人」
  const navPage = route.page === "persona" ? "bot" : route.page;
  document.querySelectorAll(".nav-item").forEach((item, index, items) => {
    if (item.dataset.page === navPage) {
      item.parentNode.style.setProperty("--nav-index", index);
      item.parentNode.style.setProperty("--nav-count", items.length);
    }
    item.classList.toggle("active", item.dataset.page === navPage);
  });
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  const meta = PAGES[route.page];
  setHeader(meta.title, meta.sub, []);
  // 换页即丢弃上一页的未保存编辑，顺带收掉 beforeunload 提醒
  setPersonaDirty(false);
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
  } finally {
    if (state.page === route.page && state.param === route.param) {
      view.classList.toggle("route-arriving", routeChanged && !subpage);
    }
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

/** 概览英雄卡：一张平整的 tonal 表面，信息优先 + 普通药丸设置入口。 */
function overviewHero(data) {
  const running = Boolean(data.bot.running);
  const runningAdapters = data.adapters.filter((item) => item.running).length;
  return h(
    "section",
    { class: "card hero" },
    h(
      "div",
      { class: "hero-head" },
      h(
        "span",
        { class: `hero-status ${running ? "" : "is-off"}` },
        h("span", { class: "live-dot", dataset: { state: running ? "ok" : "off" } }),
        running ? "运行中" : "已停止"
      ),
      h("a", { class: "btn btn-sm hero-action", href: "#/settings" }, icon("settings"), "设置")
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

/** 心情枚举（与后端 MOODS 一致，设置页的情绪下拉用）。 */
const MOOD_NAMES = ["平静", "开心", "委屈", "无聊", "兴奋", "低落"];

const MOOD_TONE = {
  开心: "good",
  兴奋: "accent",
  委屈: "bad",
  低落: "bad",
  无聊: "warn",
  平静: "outline",
};

const AFFINITY_TONE = {
  亲近: "good",
  友好: "accent",
  普通: "outline",
  冷淡: "warn",
  疏远: "bad",
};

/** 好感档位色条：0–100 的横向进度，够直观又不抢注意力。 */
function affinityBar(score) {
  const fill = h("div", { class: "fill" });
  fill.style.width = `${Math.max(0, Math.min(100, Number(score) || 0))}%`;
  return h("div", { class: "scorebar", role: "img", "aria-label": `好感度 ${score}` }, h("div", { class: "track" }), fill);
}

function affinityRow(item) {
  return h(
    "div",
    { class: "row emotion-person", tabindex: "0", role: "link",
      onclick: () => navigate("people", encodeURIComponent(item.person_id)),
      onkeydown: (event) => {
        if (event.key === "Enter") navigate("people", encodeURIComponent(item.person_id));
      } },
    h("div", { class: "avatar" }, avatarText(item.display_name)),
    h("div", { class: "row-main" },
      h("div", { class: "row-title" }, item.display_name),
      affinityBar(item.score)),
    h("div", { class: "row-side" },
      h("span", { class: "num" }, Number(item.score).toFixed(1)),
      h("span", { class: `tag ${AFFINITY_TONE[item.band] || "outline"}` }, item.band))
  );
}

/** 情绪状态卡：当前心情 + 好感 Top5/Bottom3 + 最近变化；数据来自 /api/emotion。 */
function emotionCard(data) {
  const mood = data.mood || {};
  const moodNode = data.enabled
    ? h(
        "div",
        { class: "emotion-mood" },
        h("span", { class: `tag ${MOOD_TONE[mood.mood] || "outline"}` }, mood.mood || "平静"),
        mood.phrase ? h("span", { class: "muted" }, mood.phrase) : null,
        h("span", { class: "emotion-threshold" },
          data.threshold_adjust
            ? `开口阈值 ${data.threshold_adjust > 0 ? "+" : ""}${Number(data.threshold_adjust).toFixed(2)}`
            : "开口阈值不受影响")
      )
    : h("div", { class: "muted" }, "情绪系统已关闭（emotion.enabled: false）");

  const top = (data.affinity && data.affinity.top) || [];
  const bottom = (data.affinity && data.affinity.bottom) || [];
  const tracked = (data.affinity && data.affinity.tracked) || 0;

  const ranking = h(
    "div",
    { class: "grid grid-2" },
    h("div", {},
      h("div", { class: "emotion-subhead" }, "好感 Top5"),
      top.length ? h("div", { class: "emotion-list" }, ...top.map(affinityRow))
        : emptyState("heartbeat", "还没有好感记录", "跟 bot 说上话就会开始攒")),
    h("div", {},
      h("div", { class: "emotion-subhead" }, "好感 Bottom3"),
      bottom.length ? h("div", { class: "emotion-list" }, ...bottom.map(affinityRow))
        : h("div", { class: "muted emotion-empty" }, tracked > 5 ? "人数还不够分出 Bottom3" : "暂时没有更低的人"))
  );

  const recent = data.recent_changes || [];
  const changes = h(
    "div",
    { class: "emotion-changes" },
    h("div", { class: "emotion-subhead" }, "最近变化"),
    recent.length
      ? h("div", { class: "emotion-list" },
          ...recent.map((entry) =>
            h("div", { class: "emotion-change" },
              h("span", { class: entry.delta >= 0 ? "num pos" : "num neg" },
                `${entry.delta >= 0 ? "+" : ""}${Number(entry.delta).toFixed(2)}`),
              h("span", { class: "row-title" }, entry.display_name),
              h("span", { class: "tag outline" }, entry.reason || "变化"),
              h("span", { class: "muted" }, fmtRelative(entry.created_at)))))
      : h("div", { class: "muted emotion-empty" }, "还没有好感度变化")
  );

  return card("情绪与好感", {
    hint: tracked ? `已记录 ${tracked} 人` : "",
    body: h("div", { class: "emotion-body" }, moodNode, ranking, changes),
  });
}

async function renderOverview(view) {
  view.replaceChildren(skeleton(4));
  const load = async () => {
    const [data, emotion] = await Promise.all([
      api.get("/api/overview"),
      api.get("/api/emotion").catch(() => null),
    ]);
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

    // replaceChildren 会把 null 转成文本节点，用 filter 把缺省项剔掉
    view.replaceChildren(
      ...[
        overviewHero(data),
        tiles,
        emotion ? emotionCard(emotion) : null,
        h("div", { class: "grid grid-2" }, h("div", { class: "grid" }, runtime, adapters), h("div", { class: "grid" }, plugins, webui)),
        decisions,
      ].filter(Boolean)
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

async function renderLegacyPersona(view) {
  // 旧路由 #/persona：人设已并入机器人选项卡，这里仍显示编辑器并保持侧栏高亮
  await renderBot(view, "persona");
}

/**
 * 人设编辑器的全部节点。人设页已搬进「机器人」选项卡做人设二级页，
 * 但编辑器本身（保存、.bak 备份、热重载、只读预览）保持原样。
 */
async function personaNodes() {
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

  // 切到别的页时由 renderRoute / 二级菜单统一清掉脏标记与 beforeunload 提醒
  return [editCard, h("div", { class: "grid grid-2" }, effectiveCard, previewCard)];
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

/** 只读的待执行提醒：没有取消入口（一期不做取消工具），只在记忆页顺带展示。 */
function remindersCard() {
  const body = h("div", { class: "rows" }, skeleton(2));
  const wrap = card("待执行提醒", {
    hint: "只读；到点后 bot 会在原会话说一句",
    flush: true,
    body,
  });
  api
    .get("/api/reminders?limit=50")
    .then((result) => {
      if (!result.items.length) {
        body.replaceChildren(emptyState("pulse", "没有待执行的提醒", "用户明确要求「提醒我」时，模型会创建提醒"));
        return;
      }
      body.replaceChildren(
        ...result.items.map((item) =>
          h(
            "div",
            { class: "row" },
            h("div", { class: "row-main" },
              h("div", { class: "row-title" }, item.text),
              h("div", { class: "row-sub" },
                `${platformName(item.platform)} · ${item.chat_type === "private" ? "私聊" : `群 ${item.chat_id}`}` +
                `${item.requester_name ? ` · ${item.requester_name}` : ""} · 由 ${item.created_by || "未知"} 设定`)),
            h("div", { class: "row-side" }, h("span", { class: "tag accent" }, fmtDateTime(item.fire_at)))
          )
        )
      );
    })
    .catch(() => {
      body.replaceChildren(emptyState("warning", "提醒列表加载失败", "稍后刷新页面试试"));
    });
  return wrap;
}

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
  view.replaceChildren(h("section", { class: "card" }, toolbar, content), remindersCard());

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
    for (const row of list.children) {
      row.classList.toggle("item-arriving", row.dataset.motionKey === content.dataset.arrivingKey);
    }
    delete content.dataset.arrivingKey;
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
      { class: "memory", dataset: { motionKey: memory.id } },
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
            onclick: (event) => {
              const row = event.currentTarget.closest(".memory");
              return confirmDialog({
                title: "删除这条记忆？",
                message: `「${memory.content}」\n删除会写入变更日志，可以在「变更日志」里撤销。`,
                confirmText: "删除",
                danger: true,
                onConfirm: async () => {
                  await api.del(`/api/memories/${memory.id}`);
                  markDeparture(content, row);
                  toast("已删除，可在变更日志里撤销");
                  if (memoryState.tab === "list") loadList();
                },
              });
            },
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
          const created = await api.post("/api/memories", payload);
          content.dataset.arrivingKey = created.item.id;
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
    markListChange(quietChips, quiet);
    quietChips.replaceChildren(
      ...quiet.map((item) =>
        h(
          "span",
          { class: "chip", dataset: { motionKey: item } },
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
/* 插件市场（与插件页合并：已装与可装同列表，靠状态区分）              */
/* ------------------------------------------------------------------ */

const MARKET_STATE_TEXT = {
  not_installed: "未安装",
  installed: "已安装",
  enabled: "已启用",
};

/** 权限徽标：manifest 的 permissions 映射成中文短标签。 */
function permissionBadges(permissions, meta = []) {
  const byId = new Map((meta || []).map((item) => [item.id, item]));
  return h(
    "div",
    { class: "market-perms" },
    ...(permissions || []).map((name) => {
      const info = byId.get(name) || { label: name, high_risk: false };
      return h(
        "span",
        { class: `tag ${info.high_risk ? "bad" : "outline"}`, title: info.description || name },
        info.label || name
      );
    })
  );
}

/** 市场卡片的状态标签（含可更新 / 不合规 / 核心版本过低 / 仅展示）。 */
function marketTags(item) {
  const tags = [];
  if (item.invalid) tags.push(h("span", { class: "tag bad" }, "不合规"));
  if (item.core_too_old) tags.push(h("span", { class: "tag bad" }, "核心版本过低"));
  if (item.updatable) tags.push(h("span", { class: "tag warn" }, "可更新"));
  if (item.state === "enabled") tags.push(h("span", { class: "tag good" }, "已启用"));
  else if (item.state === "installed") tags.push(h("span", { class: "tag" }, "已安装"));
  else if (!item.download_available) tags.push(h("span", { class: "tag outline" }, "仅展示"));
  return tags;
}

/** 安装确认框：逐项列出权限；高权限（空间写入 / 定时任务）额外红色提示。 */
function installDialog(item) {
  const dialog = $("#dialog");
  const close = () => dialog.close();
  const meta = item.permission_meta || [];
  const highRisk = meta.filter((info) => info.high_risk);

  const rows = meta.length
    ? h(
        "ul",
        { class: "perm-list" },
        ...meta.map((info) =>
          h(
            "li",
            { class: info.high_risk ? "high-risk" : "" },
            h("strong", {}, info.label || info.id),
            h("span", {}, info.description || "")
          )
        )
      )
    : h("p", { class: "field-hint" }, "该插件没有声明任何权限。");

  const confirm = h(
    "button",
    { class: "btn btn-primary", type: "button" },
    "确认安装"
  );
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      const result = await api.post(`/api/market/plugins/${encodeURIComponent(item.id)}/install`);
      toast(result.message || "已安装");
      close();
      await renderRoute();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message || "安装失败", "error");
    } finally {
      confirm.disabled = false;
    }
  });

  dialog.replaceChildren(
    h(
      "div",
      { class: "dialog-head" },
      h("h2", {}, `安装「${item.name}」`),
      h("button", { class: "icon-button", type: "button", "aria-label": "关闭", onclick: close }, icon("close"))
    ),
    h(
      "div",
      { class: "dialog-body" },
      h(
        "p",
        { class: "soft", style: { marginTop: 0 } },
        `版本 ${item.version || "未知"} · ${item.author || "未知作者"}。插件是任意 Python 代码，安装即完全信任；` +
          "安装后默认不启用，需要你在卡片上手动开启。"
      ),
      h("div", { class: "field-label" }, "它需要这些权限"),
      rows,
      highRisk.length
        ? h(
            "div",
            { class: "notice bad" },
            icon("warning"),
            h(
              "span",
              {},
              `注意：该插件声明了高权限（${highRisk.map((info) => info.label).join("、")}），` +
                "会读写你的 QQ 空间或运行后台定时任务，请确认来源可信。"
            )
          )
        : null
    ),
    h(
      "div",
      { class: "dialog-foot" },
      h("button", { class: "btn btn-ghost", type: "button", onclick: close }, "取消"),
      confirm
    )
  );
  dialog.showModal();
}

/** 详情抽屉：完整说明、版本、作者、权限、钩子与不合规原因。 */
function detailDrawer(item) {
  const dialog = $("#dialog");
  const close = () => dialog.close();
  const kv = (label, value) =>
    h(
      "div",
      { class: "market-kv" },
      h("span", { class: "field-label" }, label),
      h("span", {}, value || "—")
    );
  const meta = item.permission_meta || [];
  dialog.replaceChildren(
    h(
      "div",
      { class: "dialog-head" },
      h("h2", {}, `${item.name}${item.version ? ` · v${item.version}` : ""}`),
      h("button", { class: "icon-button", type: "button", "aria-label": "关闭", onclick: close }, icon("close"))
    ),
    h(
      "div",
      { class: "dialog-body" },
      h("p", { class: "soft", style: { marginTop: 0 } }, item.description || "这个插件没有写说明。"),
      item.invalid_reason
        ? h("div", { class: "notice bad" }, icon("warning"), h("span", {}, `不合规：${item.invalid_reason}`))
        : null,
      h(
        "div",
        { class: "market-kv-grid" },
        kv("版本", item.version),
        kv("作者", item.author),
        kv("分类", item.category || "工具"),
        kv("状态", MARKET_STATE_TEXT[item.state] || item.state),
        item.latest_version && item.latest_version !== item.version
          ? kv("最新版本", item.latest_version)
          : null,
        item.local_path ? kv("本地目录", item.local_path) : null
      ),
      h("div", { class: "field-label" }, "权限"),
      meta.length
        ? h(
            "ul",
            { class: "perm-list" },
            ...meta.map((info) =>
              h(
                "li",
                { class: info.high_risk ? "high-risk" : "" },
                h("strong", {}, info.label || info.id),
                h("span", {}, info.description || "")
              )
            )
          )
        : h("p", { class: "field-hint" }, "没有声明权限。"),
      h("div", { class: "field-label" }, "钩子"),
      h(
        "p",
        { class: "field-hint", style: { margin: 0 } },
        item.hooks && item.hooks.length ? item.hooks.join("、") : "未声明（以 plugin.py 实际注册为准）"
      )
    ),
    h(
      "div",
      { class: "dialog-foot" },
      h("button", { class: "btn", type: "button", onclick: close }, "关闭")
    )
  );
  dialog.showModal();
}

/** 卡片的操作按钮组：按状态机切换安装 / 启用 / 停用 / 更新 / 卸载。 */
async function pluginConfigDialog(item) {
  const dialog = $("#dialog");
  const close = () => dialog.close();
  dialog.replaceChildren(h("div", { class: "dialog-body" }, h("p", {}, "正在读取配置…")));
  dialog.showModal();
  try {
    const data = await api.get(`/api/plugins/${encodeURIComponent(item.id)}/config`);
    const draft = { ...(data.values || {}) };
    const fields = h("div", { style: { display: "grid", gap: "14px" } });
    for (const spec of data.schema || []) {
      const key = String(spec.key || "");
      const kind = String(spec.type || "string").toLowerCase();
      const label = spec.description || key;
      let control;
      if (kind === "bool" || kind === "boolean") {
        control = switchField(Boolean(draft[key]), (value) => { draft[key] = value; }, { label });
      } else if (["int", "integer", "float", "number"].includes(kind)) {
        control = numberField(draft[key], (value) => { draft[key] = value === "" ? "" : Number(value); });
      } else if (["list", "string_list"].includes(kind)) {
        control = listEditor(draft[key], (value) => { draft[key] = value; });
      } else {
        control = textField(draft[key], (value) => { draft[key] = value; });
      }
      fields.append(h("label", { class: "field" }, h("span", { class: "field-label" }, label), control));
    }
    const save = h("button", { class: "btn btn-primary", type: "button" }, "保存配置");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const result = await api.put(`/api/plugins/${encodeURIComponent(item.id)}/config`, { values: draft });
        toast(result.message || "插件配置已保存");
        close();
      } catch (err) {
        if (!(err instanceof Unauthorized)) toast(err.message || "保存失败", "error");
      } finally {
        save.disabled = false;
      }
    });
    dialog.replaceChildren(
      h("div", { class: "dialog-head" }, h("h2", {}, `配置「${item.name}」`), h("button", { class: "icon-button", type: "button", onclick: close }, icon("close"))),
      h("div", { class: "dialog-body" }, h("div", { class: "notice" }, icon("warning"), "保存后重启 bot 生效。"), fields),
      h("div", { class: "dialog-foot" }, h("button", { class: "btn", type: "button", onclick: close }, "取消"), save)
    );
  } catch (err) {
    if (!(err instanceof Unauthorized)) {
      dialog.replaceChildren(h("div", { class: "dialog-body" }, h("p", {}, err.message || "读取配置失败"), h("button", { class: "btn", type: "button", onclick: close }, "关闭")));
    }
  }
}

function marketActions(item, refresh) {
  const buttons = [];
  const run = (path, body, message) => async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api.post(path, body);
      toast(result.message || message);
      await refresh();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message || "操作失败", "error");
      button.disabled = false;
    }
  };

  if (item.state === "not_installed") {
    buttons.push(
      h(
        "button",
        {
          class: "btn btn-sm btn-primary",
          type: "button",
          disabled: !item.download_available,
          title: item.download_available ? "" : "该插件没有下载地址，仅作展示",
          onclick: () => installDialog(item),
        },
        "安装"
      )
    );
  } else {
    if (item.updatable && item.download_available) {
      buttons.push(
        h(
          "button",
          {
            class: "btn btn-sm",
            type: "button",
            onclick: run(`/api/market/plugins/${encodeURIComponent(item.id)}/update`, {}, "已更新"),
          },
          icon("refresh"),
          "更新"
        )
      );
    }
    if (item.has_config) {
      buttons.push(h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => pluginConfigDialog(item) }, "配置"));
    }
    if (item.state === "enabled") {
      buttons.push(
        h(
          "button",
          {
            class: "btn btn-sm btn-ghost",
            type: "button",
            disabled: item.invalid || item.core_too_old,
            onclick: run(
              `/api/plugins/${encodeURIComponent(item.plugin_name)}`,
              { enabled: false },
              "已停用"
            ),
          },
          "停用"
        )
      );
    } else {
      buttons.push(
        h(
          "button",
          {
            class: "btn btn-sm btn-primary",
            type: "button",
            disabled: item.invalid || item.core_too_old,
            title: item.invalid_reason || (item.core_too_old ? "当前框架版本过低" : ""),
            onclick: run(
              `/api/plugins/${encodeURIComponent(item.plugin_name)}`,
              { enabled: true },
              "已启用"
            ),
          },
          "启用"
        )
      );
    }
    buttons.push(
      h(
        "button",
        {
          class: "btn btn-sm btn-ghost",
          type: "button",
          title: "卸载会删除插件目录（插件自带的配置文件一并删除，config.yaml 的配置段保留）",
          onclick: () =>
            confirmDialog({
              title: `卸载「${item.name}」？`,
              message:
                "会先停用再删除插件目录，目录里自带的文件（含插件自己的 config.yaml）会一起删掉；" +
                "框架 config.yaml 里该插件的配置段保留不动，只从 plugins.enabled 摘除。",
              confirmText: "卸载",
              danger: true,
              onConfirm: async () => {
                const result = await api.post(
                  `/api/market/plugins/${encodeURIComponent(item.id)}/uninstall`
                );
                toast(result.message || "已卸载");
                await refresh();
              },
            }),
        },
        "卸载"
      )
    );
  }
  return h("div", { class: "market-actions" }, ...buttons);
}

function marketCard(item, refresh) {
  const iconText = (item.icon || "").trim() || avatarText(item.name);
  return h(
    "article",
    { class: `market-card ${item.state === "enabled" ? "is-enabled" : ""}` },
    h(
      "div",
      { class: "market-card-head" },
      h("div", { class: "market-icon", "aria-hidden": "true" }, iconText),
      h(
        "div",
        { class: "market-title" },
        h("div", { class: "row-title" }, item.name),
        h("div", { class: "row-sub" }, [item.author || "未知作者", item.version ? `v${item.version}` : ""].filter(Boolean).join(" · "))
      ),
      h(
        "button",
        {
          class: "icon-button",
          type: "button",
          "aria-label": `查看 ${item.name} 详情`,
          title: "详情",
          onclick: () => detailDrawer(item),
        },
        icon("caret")
      )
    ),
    h("p", { class: "market-desc" }, item.description || "这个插件没有写说明。"),
    permissionBadges(item.permissions, item.permission_meta),
    h(
      "div",
      { class: "market-foot" },
      h("div", { class: "market-tags" }, ...marketTags(item)),
      marketActions(item, refresh)
    )
  );
}

async function renderPlugins(view) {
  const keyword = { value: "" };
  const category = { value: "" };
  let data = null;

  const gridBox = h("div", {});
  const tabsBox = h("div", { class: "market-tabs", role: "tablist" });
  const sourceBox = h("div", {});

  const searchInput = h("input", {
    class: "input",
    type: "search",
    placeholder: "搜索插件名称、说明或作者",
    "aria-label": "搜索插件",
  });
  searchInput.addEventListener(
    "input",
    debounce(() => {
      keyword.value = searchInput.value;
      drawBody();
    }, 200)
  );

  const drawBody = () => {
    if (!data) return;
    const q = keyword.value.trim().toLowerCase();
    const items = (data.items || []).filter((item) => {
      if (category.value && item.category !== category.value) return false;
      if (!q) return true;
      return [item.name, item.id, item.description, item.author]
        .filter(Boolean)
        .some((text) => String(text).toLowerCase().includes(q));
    });

    tabsBox.replaceChildren(
      ...[["", "全部"], ...(data.categories || [])].map((item) => {
        const value = Array.isArray(item) ? item[0] : item;
        const label = Array.isArray(item) ? item[1] : item;
        return h(
          "button",
          {
            class: `market-tab ${category.value === value ? "active" : ""}`,
            type: "button",
            role: "tab",
            "aria-selected": category.value === value ? "true" : "false",
            onclick: () => {
              category.value = value;
              drawBody();
            },
          },
          label
        );
      })
    );
    gridBox.replaceChildren(
      items.length
        ? h("div", { class: "market-grid" }, ...items.map((item) => marketCard(item, load)))
        : emptyState("plugins", "没有匹配的插件", "换个关键词或分类试试")
    );
  };

  const drawSource = () => {
    const source = (data && data.index) || {};
    const available = source.available !== false;
    sourceBox.replaceChildren(
      h(
        "div",
        { class: `notice ${available ? "good" : ""}` },
        icon(available ? "plugins" : "warning"),
        h(
          "div",
          { style: { display: "grid", gap: "4px", flex: "1" } },
          h(
            "span",
            {},
            h("strong", {}, `数据来源：${source.label || "内置官方索引"}`),
            source.url ? `（${source.url}）` : ""
          ),
          source.error
            ? h("span", { class: "field-hint" }, `${source.note || "索引不可用"}：${source.error}`)
            : source.note
            ? h("span", { class: "field-hint" }, source.note)
            : null
        )
      )
    );
  };

  const load = async () => {
    data = await api.get("/api/market");
    drawSource();
    drawBody();
  };

  view.replaceChildren(
    h(
      "div",
      { class: "grid", style: { gridTemplateColumns: "minmax(0, 1fr)", gap: "14px" } },
      sourceBox,
      h("div", { class: "market-toolbar" }, searchInput, tabsBox),
      gridBox,
      h(
        "p",
        { class: "field-hint" },
        "插件默认全部关闭：安装后状态是「已安装」，在卡片上点「启用」才会加载。"
      )
    )
  );
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
      content),
    auditCard()
  );
  await load();
  onPoll(3000, () => {
    if (following) load().catch(() => {});
  });
}

/**
 * 网关调用审计（最小挂点）：只读列表，默认折叠在日志页底部。
 * 前端整体重组在另一分支进行，这里只保留接口可用时的一张小卡片。
 */
function auditCard() {
  const body = h("div", { class: "rows" }, h("div", { class: "log-empty" }, "加载中…"));
  const load = async () => {
    try {
      const data = await api.get("/api/gateway/audit?limit=30");
      if (!data.items.length) {
        body.replaceChildren(h("div", { class: "log-empty" }, "还没有网关调用记录"));
        return;
      }
      body.replaceChildren(
        ...data.items.map((item) =>
          h(
            "div",
            { class: "log-line" },
            h("span", { class: "log-time" }, fmtClock(item.ts)),
            h("span", { class: "log-level", dataset: { level: item.ok ? "INFO" : "WARNING" } },
              item.ok ? "OK" : "拒绝"),
            h("span", { class: "log-msg" },
              h("b", {}, item.action),
              item.sensitive ? h("span", { class: "tag warn" }, "敏感") : null,
              `  ${item.reject_reason || item.result || ""}`)
          )
        )
      );
    } catch (err) {
      if (err instanceof Unauthorized) return;
      body.replaceChildren(h("div", { class: "log-empty" }, "审计接口暂不可用"));
    }
  };
  load();
  return h(
    "details",
    { class: "card advanced-yaml" },
    h("summary", { class: "card-head" },
      h("h2", {}, "网关调用审计"),
      h("span", { class: "hint" }, "成功与拒绝都记录；敏感动作标红")),
    h("div", { class: "card-body" }, body)
  );
}

/* ------------------------------------------------------------------ */
/* 设置                                                               */
/* ------------------------------------------------------------------ */

const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

/** 需要重启才能生效的字段 → 展示名。 */
const RESTART_LABELS = {
  database_path: "数据库路径",
  mcp: "MCP 外部工具",
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
    markListChange(chips, items);
    chips.replaceChildren(
      ...items.map((item) =>
        h(
          "span",
          { class: "chip", dataset: { motionKey: item } },
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

/** 模型角色表：预置角色 + 自定义角色名，值留空表示跟随对话模型。 */
function modelsEditor(draft) {
  const ROLES = [
    ["fast", "快速档（小任务）"],
    ["summary", "滚动摘要"],
    ["vision", "看图"],
    ["gate", "分寸审查"],
    ["distill", "画像提炼"],
  ];
  const models = () => {
    const current = draft.llm && draft.llm.models;
    return current && typeof current === "object" ? current : {};
  };
  const field = (role, label) =>
    h(
      "div",
      { class: "settings-field" },
      h("span", { class: "field-label" }, `${label}（${role}）`),
      h("input", {
        class: "input mono",
        type: "text",
        value: models()[role] || "",
        placeholder: "留空跟随对话模型",
        "aria-label": `${label} 模型`,
        oninput: (event) => {
          const next = { ...models() };
          const value = event.target.value.trim();
          if (value) next[role] = value;
          else delete next[role];
          draft.llm = draft.llm || {};
          draft.llm.models = next;
        },
      })
    );

  const extraRoles = Object.keys(models()).filter((role) => !ROLES.some(([name]) => name === role));

  return h(
    "div",
    { style: { display: "grid", gap: "12px" } },
    h("div", { class: "settings-fields" }, ...ROLES.map(([role, label]) => field(role, label)),
      ...extraRoles.map((role) => field(role, role))),
    h("span", { class: "field-hint" }, "留空即跟随对话模型；配置里写过的自定义角色名会原样保留在下面（刷新页面后可见）。")
  );
}

/** 任意键 → 数值 的表（目前用于 emotion.threshold_adjust 的「心情 → 微调」）。 */
function mapEditor(draft, path, { keyPlaceholder = "键", valuePlaceholder = "值" } = {}) {
  const table = () => {
    const current = getPath(draft, path);
    return current && typeof current === "object" ? current : {};
  };
  const rows = h("div", { style: { display: "grid", gap: "8px" } });

  function render() {
    const current = table();
    const entries = Object.keys(current);
    markListChange(rows, entries);
    rows.replaceChildren(
      ...(entries.length
        ? entries.map((key) =>
            h(
              "div",
              { dataset: { motionKey: key }, style: { display: "flex", alignItems: "center", gap: "10px" } },
              h("span", { class: "field-label", style: { minWidth: "56px", margin: 0 } }, key),
              h("input", {
                class: "input",
                type: "number",
                step: "0.01",
                min: "-1",
                max: "1",
                value: String(current[key] ?? ""),
                "aria-label": `${key} 的微调值`,
                oninput: (event) => {
                  const next = { ...table() };
                  next[key] = event.target.value;
                  setPath(draft, path, next);
                },
              }),
              h(
                "button",
                {
                  class: "icon-btn-sm danger",
                  type: "button",
                  title: `删除 ${key}`,
                  "aria-label": `删除 ${key}`,
                  onclick: () => {
                    const next = { ...table() };
                    delete next[key];
                    setPath(draft, path, next);
                    render();
                  },
                },
                icon("trash")
              )
            )
          )
        : [h("div", { class: "field-hint" }, "还没有条目；没配的心情按 0 处理。")])
    );
  }

  const keyInput = h("input", {
    class: "input",
    placeholder: keyPlaceholder,
    style: { maxWidth: "140px" },
    "aria-label": keyPlaceholder,
  });
  const valueInput = h("input", {
    class: "input",
    type: "number",
    step: "0.01",
    min: "-1",
    max: "1",
    placeholder: valuePlaceholder,
    style: { maxWidth: "120px" },
    "aria-label": valuePlaceholder,
  });
  const addButton = h(
    "button",
    {
      class: "btn btn-sm btn-ghost",
      type: "button",
      onclick: () => {
        const key = keyInput.value.trim();
        if (!key) return;
        const next = { ...table() };
        next[key] = valueInput.value === "" ? 0 : valueInput.value;
        setPath(draft, path, next);
        keyInput.value = "";
        valueInput.value = "";
        render();
      },
    },
    icon("plus"),
    "添加"
  );

  render();
  return h(
    "div",
    { style: { display: "grid", gap: "10px" } },
    rows,
    h("div", { style: { display: "flex", gap: "8px", alignItems: "center" } }, keyInput, valueInput, addButton)
  );
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
    markListChange(groupsNode, entries);
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
      { class: "group-item", dataset: { motionKey: chatId } },
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

/** 同名连接只提交变动的参数；数组/映射保留顺序、重复参数和空字符串。 */
function mcpServerEditor(draft) {
  const rows = h("div", { class: "group-list" });
  const servers = draft.mcp.servers;
  const field = (label, input) => h("label", { class: "settings-field" }, h("span", { class: "field-label" }, label), input);
  function serverRow(row) {
    const params = h("div", { class: "settings-fields" });
    const connectionField = (key, label, example, structured = false) => {
      const current = row[key];
      const input = h("input", {
        type: "text", class: "input mono", autocomplete: "off", placeholder: example,
        value: current === undefined ? "" : (structured && typeof current !== "string" ? JSON.stringify(current) : current),
        oninput: ({ target }) => {
          if (!target.value) { delete row[key]; return; }
          if (!structured) { row[key] = target.value; return; }
          // 暂存无效文本：保存时后端返回字段错误，不能把拼错的 JSON 当作「保留原值」。
          try { row[key] = JSON.parse(target.value); } catch { row[key] = target.value; }
        },
      });
      return field(label, input);
    };
    function renderParams() {
      params.replaceChildren(...(row.transport === "http" ? [
        connectionField("url", "服务地址", "https://host/mcp"),
        connectionField("headers", "请求头 · JSON 对象", '{"Authorization":"Bearer ${MCP_TOKEN}"}', true),
      ] : [
        connectionField("command", "命令", "/path/to/server"),
        connectionField("args", "启动参数 · JSON 数组", '["--mode","stdio"]', true),
        connectionField("env", "环境变量 · JSON 对象", '{"TOKEN":"${MCP_TOKEN}"}', true),
      ]));
    }
    renderParams();
    return h("div", { class: "group-item" },
      h("div", { class: "group-item-head" },
        field("名称", textField(row.name, value => { row.name = value; }, { placeholder: "例如 local-docs", mono: true })),
        field("传输", selectField(row.transport, ["stdio", "http"].map(value => ({ value, label: value })), value => {
          for (const key of ["command", "args", "env", "url", "headers"]) delete row[key];
          row.transport = value; renderParams();
        })),
        field("启用", switchField(row.enabled, on => { row.enabled = on; }, { label: "启用此服务器" })),
        h("button", { type: "button", class: "icon-btn-sm danger", "aria-label": "移除此服务器", onclick: () => {
          servers.splice(servers.indexOf(row), 1); render();
        } }, icon("trash"))), params);
  }
  function render() {
    rows.replaceChildren(...(servers.length ? servers.map(serverRow) : [h("p", { class: "field-hint" }, "未配置服务器。模型只会使用内置工具。") ]));
  }
  render();
  return h("div", { class: "settings-stack" }, rows,
    h("div", {}, h("button", { class: "btn btn-sm", type: "button", onclick: () => {
      servers.push({ name: "", transport: "stdio", enabled: true }); render();
    } }, icon("plus"), "添加服务器")));
}

/** 仅在操作电脑页读取审计；刷新记录不改动表单草稿。 */
function computerAuditCard() {
  const rows = h("div", { class: "rows", "aria-live": "polite" }, skeleton(2));
  const refresh = h("button", { type: "button", class: "btn btn-sm", onclick: load }, icon("refresh"), "刷新");
  async function load() {
    refresh.disabled = true;
    try {
      const { items } = await api.get("/api/computer/audit?limit=30");
      const contexts = { host: "真机", sandbox: "访客沙盒" };
      rows.replaceChildren(...(items.length ? items.map(item => h("div", { class: "plugin-row" },
        h("div", { class: "row-main" },
          h("div", { class: "row-title mono" }, item.tool),
          h("div", { class: "row-sub" }, `${fmtDateTime(item.created_at)} · ${item.who} · ${contexts[item.context] || item.context}`),
          h("div", { class: "row-sub" }, `参数 ${item.args_summary.arg_count} 项，输入 ${item.args_summary.input_bytes} 字节 · 耗时 ${item.duration_ms} 毫秒`)),
        h("span", { class: `tag ${item.success ? "good" : "bad"}` }, item.success ? "成功" : "失败")))
        : [h("p", { class: "field-hint" }, "还没有电脑操作记录。") ]));
    } catch (err) {
      if (!(err instanceof Unauthorized)) rows.replaceChildren(h("p", { class: "field-hint" }, "无法读取操作记录，请刷新重试。"));
    } finally { refresh.disabled = false; }
  }
  load();
  return card("操作审计", { hint: "最近 30 次操作，仅显示参数用量，不显示命令或文件内容。", actions: [refresh], body: rows });
}

/** 工具卡只读；按需刷新，不因查看状态发起连接。 */
function mcpStatusCard() {
  const states = { disabled: "已停用", connecting: "连接中", connected: "已连接", disconnected: "已断开", unavailable: "未运行" };
  const rows = h("div", { class: "rows", "aria-live": "polite" }, skeleton(2));
  const refresh = h("button", { type: "button", class: "btn btn-sm", onclick: load }, icon("refresh"), "刷新");
  async function load() {
    refresh.disabled = true;
    try {
      const { servers } = await api.get("/api/mcp/status");
      rows.replaceChildren(...(servers.length ? servers.map(s => h("div", { class: "plugin-row" },
        h("div", { class: "row-main" }, h("div", { class: "row-title mono" }, s.name),
          h("div", { class: "row-sub" }, `${s.transport} · ${s.tool_count} 项工具`),
          s.last_error ? h("div", { class: "row-sub" }, `最近错误：${s.last_error}`) : null),
        h("span", { class: `tag ${s.state === "connected" ? "good" : s.state === "disconnected" ? "warn" : ""}` }, states[s.state] || s.state))) :
        [h("p", { class: "field-hint" }, "未配置外部服务器。") ]));
    } catch (err) {
      if (!(err instanceof Unauthorized)) rows.replaceChildren(h("p", { class: "field-hint" }, "暂时无法读取连接状态。"));
    } finally { refresh.disabled = false; }
  }
  load();
  return card("MCP 连接状态", { actions: [h("a", { class: "btn btn-sm", href: "#/settings/mcp" }, "配置服务器"), refresh], body: rows });
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
    reminder: "提醒",
    proactive: "冷场开口",
    generation: "生成",
    emotion: "情绪与好感度",
    plugins: "插件",
    market: "插件市场",
    webui: "WebUI",
    persona: "人格",
    adapters: "适配器",
    log_level: "日志级别",
    background_check_minutes: "后台检查",
    database_path: "数据库路径",
    search: "联网搜索",
    voice: "语音双向",
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

/** 字段路径 → 二级页面：规则由后端 config_layout() 提供，前端只做最长前缀匹配。 */
function pageOf(layout, path) {
  let best = null;
  for (const rule of (layout && layout.rules) || []) {
    if (path === rule.prefix || path.startsWith(`${rule.prefix}.`)) {
      if (!best || rule.prefix.length > best.prefix.length) best = rule;
    }
  }
  return best ? best.page : "";
}

/** 找到分组与二级页面；缺省回落到第一个，未知 id 不会把页面打空。 */
function findPage(layout, groupId, pageId) {
  const groups = (layout && layout.groups) || [];
  const group = groups.find((item) => item.id === groupId) || groups[0];
  const page =
    (group && group.pages.find((item) => item.id === pageId)) || (group && group.pages[0]);
  return { group, page };
}

/** 左侧二级菜单：只列当前分组（机器人 / 设置）的页面。 */
function settingsMenu(layout, group, page) {
  return h(
    "nav",
    { class: "settings-menu", "aria-label": `${group.title}分类` },
    h("div", { class: "settings-menu-group" }, group.title),
    ...group.pages.map((item) =>
      h(
        "button",
        {
          type: "button",
          class: `settings-menu-item ${item.id === page.id ? "active" : ""}`,
          "aria-current": item.id === page.id ? "page" : null,
          onclick: () => navigate(group.route, item.id),
        },
        item.title
      )
    )
  );
}

/**
 * 配置卡片清单（只有标签与控件形态，不含分类）。
 * 每张卡用 ``path`` 标出它覆盖的代表字段，具体归到哪个二级页面由后端
 * ``layout.rules`` 决定；这里不再维护第二份「字段 → 页面」表。
 */
function settingsCardSpecs(draft, secrets, pluginsAvailable) {
  return [
    {
      path: "llm.base_url",
      node: card("模型与接口", {
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
            { path: "llm.temperature", label: "默认温度", kind: "number", step: "0.05", min: 0, max: 2, hint: "非聊天角色的默认温度；0–2" },
            { path: "llm.chat_temperature", label: "聊天温度", kind: "number", step: "0.05", min: 0, max: 2, hint: "留空沿用默认温度；聊天角色单独生效" },
            { path: "llm.chat_frequency_penalty", label: "聊天重复惩罚", kind: "number", step: "0.05", min: -2, max: 2, hint: "聊天角色 frequency_penalty，默认 0.3" },
            { path: "llm.timeout", label: "超时（秒）", kind: "number", step: "1", min: 0.1, hint: "单次请求超时" },
            { path: "llm.max_retries", label: "重试次数", kind: "number", step: "1", min: 0, max: 10, hint: "失败后的重试次数" },
          ],
          draft
        ),
      }),
    },
    {
      path: "llm.models",
      node: card("模型角色与审查模型", {
        hint: "「角色 → 模型名」；角色配了就用，没配回退对话模型",
        body: h(
          "div",
          { style: { display: "grid", gap: "16px" } },
          fieldGrid(
            [
              { path: "llm.gate_model", label: "分寸审查模型", hint: "记忆分寸审查用；留空跟随对话模型", mono: true },
            ],
            draft
          ),
          modelsEditor(draft)
        ),
      }),
    },
    {
      path: "memory.extract_every_n",
      node: card("记忆：抽取与归纳", {
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
    },
    {
      path: "memory.distill_new_threshold",
      node: card("记忆：画像与时间线", {
        hint: "记忆 v2 的画像提炼、近况过期与时间线压缩",
        body: fieldGrid(
          [
            { path: "memory.distill_new_threshold", label: "画像提炼触发条数", kind: "number", step: "1", min: 1, hint: "新增事件 + 事实达到多少条触发提炼" },
            { path: "memory.distill_interval_hours", label: "提炼间隔（小时）", kind: "number", step: "1", min: 0.1, hint: "距上次提炼超过这么久也会触发" },
            { path: "memory.distill_max_events", label: "提炼事件上限", kind: "number", step: "1", min: 1, hint: "一次提炼最多喂多少条新事件" },
            { path: "memory.distill_max_facts", label: "提炼事实上限", kind: "number", step: "1", min: 1, hint: "一次提炼最多喂多少条新事实" },
            { path: "memory.status_expiry_min_days", label: "近况过期下限（天）", kind: "number", step: "1", min: 1, hint: "模型估计的过期天数夹取到这一段区间" },
            { path: "memory.status_expiry_max_days", label: "近况过期上限（天）", kind: "number", step: "1", min: 1 },
            { path: "memory.timeline_compress_after_days", label: "时间线压缩阈值（天）", kind: "number", step: "1", min: 0.1, hint: "比这更早的事件按人按周压成 digest" },
            { path: "memory.timeline_prune_after_days", label: "时间线清理阈值（天）", kind: "number", step: "1", min: 0.1, hint: "比这更早的事件只保留高显著度的" },
            { path: "memory.timeline_prune_keep_salience", label: "清理时保留的显著度", kind: "number", step: "0.05", min: 0, max: 1, hint: "显著度高于它的旧事件不清理" },
            { path: "memory.self_event_max_chars", label: "自我事件截断（字）", kind: "number", step: "10", min: 1, hint: "bot 自己发的每条消息记进自我时间线时的截断长度" },
          ],
          draft
        ),
      }),
    },
    {
      path: "memory.index_active_days",
      node: card("记忆：读取侧", {
        hint: "索引、预拉取、跨会话回看与分寸审查",
        body: fieldGrid(
          [
            { path: "memory.index_active_days", label: "索引活跃窗口（天）", kind: "number", step: "1", min: 0.1, hint: "群成员索引只收近 N 天在本群活跃过的人" },
            { path: "memory.index_max_members", label: "索引人数上限", kind: "number", step: "1", min: 1 },
            { path: "memory.index_ttl_seconds", label: "索引缓存时效（秒）", kind: "number", step: "10", min: 0, hint: "画像更新时缓存会立即失效" },
            { path: "memory.index_identity_chars", label: "索引身份截断（字）", kind: "number", step: "4", min: 4, hint: "索引里「一句话身份」的截断长度" },
            { path: "memory.mentioned_max_people", label: "预拉画像人数", kind: "number", step: "1", min: 0, hint: "一轮里最多拉几个被提及者的核心画像" },
            { path: "memory.profile_inject_max_chars", label: "说话人画像上限（字）", kind: "number", step: "20", min: 120, hint: "画像核心不参与预算裁剪" },
            { path: "memory.inject_budget_tokens", label: "注入预算（token）", kind: "number", step: "100", min: 0, hint: "超出后先砍事实、再砍时间线，画像核心不动" },
            { path: "memory.timeline_hit_limit", label: "每人相关旧事条数", kind: "number", step: "1", min: 1 },
            { path: "memory.cross_session_hours", label: "跨会话回看（小时）", kind: "number", step: "1", min: 0.1, hint: "私聊跨会话摘要回看的小时数" },
            { path: "memory.self_inject_hours", label: "自我回看（小时）", kind: "number", step: "1", min: 0.1, hint: "回看本会话里 bot 自己最近说过/做过的事" },
            { path: "memory.self_inject_limit", label: "自我注入条数", kind: "number", step: "1", min: 0 },
            { path: "memory.gate_confidence_threshold", label: "分寸审查置信度", kind: "number", step: "0.05", min: 0, max: 1, hint: "模型裁定置信度低于它不放行" },
            { path: "memory.gate_ask_high_sensitivity", label: "高敏感项进待问队列", kind: "switch", hint: "高敏感跨场合内容是否顺带进待问队列" },
            { path: "memory.recall_tool_enabled", label: "启用 recall 工具", kind: "switch", hint: "模型支持 OpenAI 工具调用时生效" },
            { path: "memory.recall_tool_max_rounds", label: "recall 最多轮数", kind: "number", step: "1", min: 0 },
          ],
          draft
        ),
      }),
    },
    {
      path: "memory.rolling_summary.enabled",
      node: card("滚动摘要", {
        hint: "把本会话的早期对话压成摘要，prompt 里只放摘要 + 近期原文",
        body: fieldGrid(
          [
            { path: "memory.rolling_summary.enabled", label: "启用滚动摘要", kind: "switch", hint: "关闭后行为与以前一致：只带最近原文" },
            { path: "memory.rolling_summary.trigger_after", label: "触发条数", kind: "number", step: "1", min: 1, hint: "未摘要的原文超过这么多条时压缩一次" },
            { path: "memory.rolling_summary.keep_recent", label: "保留近期原文", kind: "number", step: "1", min: 0, hint: "始终不压缩的最新原文条数" },
            { path: "memory.rolling_summary.max_chars", label: "摘要上限（字）", kind: "number", step: "50", min: 100, hint: "五段式摘要的总长上限，更新时模型需在此取舍" },
          ],
          draft
        ),
      }),
    },
    {
      path: "computer.enabled",
      node: card("操作电脑", {
        hint: "默认关闭。启用后，只有白名单账号可用 bot 的系统权限操作真机，其他账号使用各自的访客沙盒。",
        body: fieldGrid([
          { path: "computer.enabled", label: "启用电脑工具", kind: "switch", wide: true },
          { path: "computer.whitelist", label: "真机白名单", kind: "list", wide: true,
            placeholder: "平台:发送者账号", hint: "完整匹配账号，不按昵称、群号或私聊授权。默认仅有本地终端账号 console:console:me；移除所有账号后无人可操作真机。" },
          { path: "computer.sandbox_quota_mb", label: "每位访客的容量（MiB）", kind: "number", step: "1", min: 1, max: 10240 },
          { path: "computer.command_timeout_seconds", label: "命令超时（秒）", kind: "number", step: "1", min: 1, max: 600 },
          { path: "computer.output_limit_bytes", label: "输出上限（字节）", kind: "number", step: "1", min: 32, max: 1048576 },
        ], draft),
      }),
    },
    {
      path: "mcp.servers",
      node: card("MCP 服务器", {
        hint: "重启后生效",
        body: h("div", { class: "settings-stack" },
          h("p", { class: "field-hint" }, "只连接可信服务；stdio 命令以 bot 身份运行。名称只用英文、数字、短横线与下划线；不可含双下划线，也不可以下划线结尾。同名、同传输的连接参数不回显，留空保留；改名或换传输须重填。用 [] 或 {} 清空参数、变量或请求头。"),
          mcpServerEditor(draft),
          fieldGrid([
            { path: "mcp.connect_timeout", label: "连接超时（秒）", kind: "number", step: "any", min: 0.001 },
            { path: "mcp.call_timeout", label: "调用超时（秒）", kind: "number", step: "any", min: 0.001 },
            { path: "mcp.max_result_bytes", label: "调用结果上限（字节）", kind: "number", step: "1", min: 32 },
          ], draft)),
      }),
    },
    {
      path: "search.order",
      node: card("联网搜索", {
        hint: "给 bot 一个 web_search 工具：按顺序尝试各渠道，未配任何 Key 时工具不开放",
        body: fieldGrid(
          [
            {
              path: "search.order",
              label: "渠道顺序",
              kind: "list",
              wide: true,
              placeholder: "例如 tavily",
              hint: "按顺序尝试；没配 Key 的渠道自动跳过，失败或没结果就换下一个。可选：tavily / deepseek",
            },
            {
              path: "search.tavily.api_key",
              label: "Tavily API Key",
              kind: "password",
              hint: "留空表示保留原值；api.tavily.com",
              secretSet: Boolean(secrets["search.tavily.api_key"]),
            },
            {
              path: "search.deepseek.api_key",
              label: "DeepSeek API Key",
              kind: "password",
              hint: "留空表示保留原值；与 DeepSeek 模型用同一把 Key",
              secretSet: Boolean(secrets["search.deepseek.api_key"]),
            },
            { path: "search.timeout_seconds", label: "超时（秒）", kind: "number", step: "1", min: 0.1, hint: "单次搜索请求的超时" },
            { path: "search.cooldown_seconds", label: "每会话冷却（秒）", kind: "number", step: "5", min: 0, hint: "冷却内重复搜索直接返回已有结果提示，不发请求" },
          ],
          draft
        ),
      }),
    },
    {
      path: "voice.stt_provider",
      node: card("语音双向", {
        hint: "听懂语音条（STT）与用语音回复（TTS）；未配置时全部降级为文字",
        body: fieldGrid(
          [
            {
              path: "voice.stt_provider",
              label: "语音转写后端",
              kind: "select",
              hint: "首选后端；失败或空文本会顺延到其他已配置的后端。qq_native 仅 SnowLuma 可用",
              options: [
                { value: "siliconflow", label: "硅基流动 SiliconFlow" },
                { value: "openai_compatible", label: "OpenAI 兼容接口" },
                { value: "qq_native", label: "QQ 原生转写（仅 SnowLuma）" },
              ],
            },
            {
              path: "voice.stt_api_key",
              label: "转写 API Key",
              kind: "password",
              hint: "留空表示保留原值；不填则语音消息只记 [语音] 占位（qq_native 不需要）",
              secretSet: Boolean(secrets["voice.stt_api_key"]),
            },
            {
              path: "voice.stt_base_url",
              label: "兼容接口地址",
              hint: "仅 OpenAI 兼容后端用，例如 https://host/v1；填了才启用",
              mono: true,
            },
            { path: "voice.stt_model", label: "转写模型", mono: true, hint: "默认 FunAudioLLM/SenseVoiceSmall" },
            { path: "voice.stt_timeout_seconds", label: "转写超时（秒）", kind: "number", step: "1", min: 0.1, hint: "单次转写请求的超时" },
            { path: "voice.stt_max_seconds", label: "单条时长上限（秒）", kind: "number", step: "5", min: 0, hint: "超过就不转写，正文只留 [语音] 占位" },
            { path: "voice.stt_max_bytes", label: "单条大小上限（字节）", kind: "number", step: "1024", min: 0, hint: "默认 5MB；超过就不转写" },
            {
              path: "voice.tts_mode",
              label: "语音回复模式",
              kind: "select",
              hint: "off 从不发语音；reply_voice 对方发语音时回语音；on_request 再加「用语音说」的明确要求",
              options: [
                { value: "off", label: "关闭（默认）" },
                { value: "reply_voice", label: "对方发语音时回语音" },
                { value: "on_request", label: "被要求时也发语音" },
              ],
            },
            {
              path: "voice.tts_provider",
              label: "语音合成后端",
              kind: "select",
              hint: "自定义 TTS 走 OpenAI 兼容的 /audio/speech；硅基流动 CosyVoice2 需要 Key",
              options: [
                { value: "custom", label: "自定义 TTS（OpenAI 兼容）" },
                { value: "siliconflow", label: "硅基流动 CosyVoice2" },
              ],
            },
            {
              path: "voice.tts_api_key",
              label: "合成 API Key",
              kind: "password",
              hint: "留空表示保留原值；硅基流动后端留空时复用转写 Key",
              secretSet: Boolean(secrets["voice.tts_api_key"]),
            },
            { path: "voice.tts_base_url", label: "自定义 TTS 地址", mono: true, hint: "OpenAI 兼容 base_url，例如 https://host/v1；留空视为未配置，降级发文字" },
            { path: "voice.tts_model", label: "合成模型", mono: true, hint: "自定义 TTS 必填；硅基流动留空用默认 CosyVoice2-0.5B" },
            { path: "voice.tts_voice", label: "合成音色", mono: true, hint: "自定义 TTS 的音色名，随请求一起发给接口" },
            { path: "voice.tts_siliconflow_voice", label: "合成音色（硅基流动）", mono: true },
            { path: "voice.tts_max_chars", label: "语音回复字数上限", kind: "number", step: "5", min: 1, hint: "正文超过就不合成语音；被拆成多条的长回复一律发文字" },
            { path: "voice.tts_timeout_seconds", label: "合成超时（秒）", kind: "number", step: "1", min: 0.1, hint: "单次合成请求的超时" },
          ],
          draft
        ),
      }),
    },
    {
      path: "chat.bot_name",
      node: card("聊天与上下文", {
        hint: "回复长度与上下文",
        body: fieldGrid(
          [
            { path: "chat.bot_name", label: "Bot 名称", hint: "会写进消息流水，也用作默认的叫名硬触发" },
            { path: "chat.history_size", label: "上下文条数", kind: "number", step: "1", min: 1, hint: "拼进提示词的最近消息条数" },
            { path: "chat.history_ttl_hours", label: "上下文有效期（小时）", kind: "number", step: "0.5", min: 0, hint: "超过这个时间的旧消息不再拼进上下文" },
            { path: "chat.dedupe_ttl_seconds", label: "入站去重窗口（秒）", kind: "number", step: "10", min: 0, hint: "同一平台消息 id 在这个时间窗内重复到达就丢弃；0 表示不去重" },
            { path: "decision.batch_max_seconds", label: "整批最长等待（秒）", kind: "number", step: "1", min: 0, hint: "从首条消息算起最多攒这么久就放出；0 表示不设上限" },
            { path: "decision.urgent_seconds", label: "@/回复的截止（秒）", kind: "number", step: "0.5", min: 0, hint: "整批里有 @ 或回复 bot 的消息时，不晚于「首条 + 这个秒数」放出；0 表示不启用" },
          ],
          draft
        ),
      }),
    },
    {
      // 拆条属于「回复形态」：这一段在 YAML 里仍属于 chat 段，但按语义归到表达页
      path: "chat.split_max_parts",
      node: card("长回复拆条", {
        hint: "按句拆成多条发送，避免刷屏；属于回复形态，和生成策略放在一起",
        body: fieldGrid(
          [
            { path: "chat.split_max_parts", label: "最多拆成几条", kind: "number", step: "1", min: 1, hint: "长回复按句拆条，避免刷屏" },
            { path: "chat.split_part_chars", label: "每段字数上限", kind: "number", step: "1", min: 8, hint: "超过就拆成下一条" },
            { path: "chat.strip_action_parens", label: "剥离动作描写", kind: "switch", hint: "发送前去掉括号里的动作描写（如「（笑）」），默认关闭" },
            { path: "chat.type_speed", label: "打字节奏倍率", kind: "number", step: "0.1", min: 0, hint: "中文字约 0.3 秒/字、其他字符约 0.15 秒/字，再乘这个倍率；0 表示两条之间不停顿" },
            { path: "chat.type_delay_max_seconds", label: "单条打字停顿上限（秒）", kind: "number", step: "0.5", min: 0, hint: "长文也不会等太久；只影响多条之间的停顿" },
          ],
          draft
        ),
      }),
    },
    {
      path: "generation.two_stage.enabled",
      node: card("生成", {
        hint: "决定回复之后怎么写出正文",
        body: fieldGrid(
          [
            {
              path: "generation.two_stage.enabled",
              label: "两段式生成",
              kind: "switch",
              hint: "所有回复都有表达决策书；开启后，闲聊碎片也由快速模型决定，不走本地快速通道",
            },
          ],
          draft
        ),
      }),
    },
    {
      path: "persona.examples_enabled",
      node: card("我说话的样子", {
        hint: "用自己的短回复作示例，不改人格正文",
        body: fieldGrid([
          { path: "persona.examples_enabled", label: "注入示例", kind: "switch" },
          { path: "persona.examples_count", label: "最多注入几条", kind: "number", min: 0, step: "1" },
          { path: "persona.examples_position", label: "注入位置", kind: "select", options: [
            { value: "before", label: "人格正文前" }, { value: "after", label: "人格正文后" },
          ] },
          { path: "persona.examples_path", label: "示例文件", mono: true, wide: true },
        ], draft),
      }),
    },
    {
      path: "emotion.enabled",
      node: card("情绪与好感度", {
        hint: "状态由规则计算，只做背景注入与阈值微调",
        body: fieldGrid(
          [
            {
              path: "emotion.enabled",
              label: "启用情绪系统",
              kind: "switch",
              hint: "关掉后不记录好感度、不注入状态、不调整开口阈值",
            },
            { path: "emotion.chat_delta", label: "每轮对话加分", kind: "number", step: "0.1", min: 0, max: 10, hint: "与 bot 完成一轮对话的好感度增量" },
            { path: "emotion.chat_daily_cap", label: "对话加分每日上限", kind: "number", step: "0.5", min: 0, hint: "每人每个自然日靠对话最多加多少" },
            { path: "emotion.praise_delta", label: "被夸 / 感谢加分", kind: "number", step: "0.5", min: -50, max: 50, hint: "关键词命中且确认是对 bot 说的" },
            { path: "emotion.insult_delta", label: "被骂减分", kind: "number", step: "0.5", min: -50, max: 50, hint: "同一套保守规则，宁可漏判" },
            { path: "emotion.decay_weekly", label: "每周回归基线比例", kind: "number", step: "0.01", min: 0, max: 1, hint: "好感度每周向 50 靠拢的比例（惰性折算）" },
            { path: "emotion.half_life_seconds", label: "情绪半衰期（秒）", kind: "number", step: "60", min: 1, hint: "情绪强度减半所需时间，默认一小时" },
            { path: "emotion.boredom_after_seconds", label: "多久没人理算无聊（秒）", kind: "number", step: "60", min: 0, hint: "超过这么久没人说话、情绪也淡了才判无聊" },
            {
              path: "emotion.praise_mood",
              label: "被夸时的情绪",
              kind: "select",
              options: MOOD_NAMES.map((name) => ({ value: name, label: name })),
              hint: "情绪枚举之一；强度在下面几项里配",
            },
            { path: "emotion.praise_intensity", label: "被夸情绪强度", kind: "number", step: "0.05", min: 0, max: 1 },
            {
              path: "emotion.insult_mood",
              label: "被骂时的情绪",
              kind: "select",
              options: MOOD_NAMES.map((name) => ({ value: name, label: name })),
            },
            { path: "emotion.insult_intensity", label: "被骂情绪强度", kind: "number", step: "0.05", min: 0, max: 1 },
            {
              path: "emotion.excitement_mood",
              label: "热闹时的情绪",
              kind: "select",
              options: MOOD_NAMES.map((name) => ({ value: name, label: name })),
            },
            { path: "emotion.excitement_intensity", label: "兴奋强度", kind: "number", step: "0.05", min: 0, max: 1, hint: "群里热闹且 bot 参与多时" },
            {
              path: "emotion.boredom_mood",
              label: "没人理时的情绪",
              kind: "select",
              options: MOOD_NAMES.map((name) => ({ value: name, label: name })),
            },
            { path: "emotion.boredom_intensity", label: "无聊强度", kind: "number", step: "0.05", min: 0, max: 1 },
            { path: "emotion.min_intensity", label: "情绪有效期阈值", kind: "number", step: "0.01", min: 0, max: 1, hint: "强度低于它视为平静：不再注入、不调阈值" },
            { path: "emotion.affinity_baseline", label: "好感度基线", kind: "number", step: "1", min: 0, max: 100, hint: "好感度长期回归的基准值" },
            { path: "emotion.affinity_min", label: "好感度下限", kind: "number", step: "1", min: 0, max: 100 },
            { path: "emotion.affinity_max", label: "好感度上限", kind: "number", step: "1", min: 0, max: 100 },
            { path: "emotion.excitement_window_seconds", label: "热闹统计窗口（秒）", kind: "number", step: "30", min: 0, hint: "统计「群里热闹」时看最近多久的消息" },
            { path: "emotion.excitement_min_messages", label: "热闹的最少消息数", kind: "number", step: "1", min: 0 },
            { path: "emotion.excitement_min_bot_messages", label: "热闹时 bot 最少发言", kind: "number", step: "1", min: 0 },
          ],
          draft
        ),
      }),
    },
    {
      path: "stickers.enabled",
      node: card("表情包行为", {
        hint: "核心功能默认关闭；保存后立即生效",
        body: fieldGrid([
          { path: "stickers.enabled", label: "启用表情包行为", kind: "switch", hint: "关闭时不收集、不发表情，也不会创建表情库" },
          { path: "stickers.collect", label: "收集群聊表情", kind: "switch", hint: "把群里的图片、贴纸和商城表情去重后存入表情库" },
          { path: "stickers.send_probability", label: "回复后补发概率", kind: "number", step: "0.01", min: 0, max: 1, hint: "每次正常回复后补发表情的概率（0–1）" },
          { path: "stickers.nonverbal_probability", label: "只发表情概率", kind: "number", step: "0.01", min: 0, max: 1, hint: "接梗或短附和时只发一张表情、不发文字的概率（0–1）" },
          { path: "stickers.cooldown_seconds", label: "会话冷却（秒）", kind: "number", step: "1", min: 0, hint: "同一平台会话两次尝试发表情的最短间隔，两种方式共用" },
        ], draft),
      }),
    },
    {
      path: "emotion.threshold_adjust",
      node: card("情绪阈值微调", {
        hint: "按当前心情调整开口阈值：正数更难开口，负数更容易",
        body: mapEditor(draft, "emotion.threshold_adjust", { keyPlaceholder: "心情，如 开心", valuePlaceholder: "如 -0.05" }),
      }),
    },
    {
      path: "decision.merge_window_seconds",
      node: card("回复意愿", {
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
            { path: "decision.cooldown_seconds", label: "软冷却（秒）", kind: "number", step: "1", min: 0, hint: "发言后这段时间内降低意愿" },
            { path: "decision.cooldown_penalty", label: "冷却最大减分", kind: "number", step: "0.1", min: 0, max: 10, hint: "冷却期内最多减多少分" },
            { path: "decision.exclude_bot_momentum", label: "bot 不计入自身动量", kind: "boolean", hint: "关闭后保留旧的动量来源语义" },
            { path: "decision.refractory_enabled", label: "启用发言不应期", kind: "boolean", hint: "普通消息在不应期内不触发；硬触发不受影响" },
            { path: "decision.refractory_seconds", label: "不应期（秒）", kind: "number", step: "1", min: 0, hint: "默认 45 秒" },
            { path: "decision.proactive_window_enabled", label: "启用主动发言窗口上限", kind: "boolean", hint: "普通群消息的主动开口受限；硬触发不计入" },
            { path: "decision.proactive_window_seconds", label: "主动发言窗口（秒）", kind: "number", step: "1", min: 0, hint: "默认 300 秒" },
            { path: "decision.proactive_window_limit", label: "窗口内主动发言上限", kind: "number", step: "1", min: 0, hint: "默认 3 条" },
            { path: "decision.veto_marker", label: "不回复标记", hint: "模型输出这个标记就静默丢弃" },
          ],
          draft
        ),
      }),
    },
    {
      path: "decision.groups",
      node: card("每群覆盖", {
        hint: "单独调节某个群",
        body: groupsEditor(draft, secrets),
      }),
    },
    {
      path: "reminder.enabled",
      node: card("提醒", {
        hint: "模型被明确要求「提醒我」时创建，不自动提醒",
        body: fieldGrid(
          [
            { path: "reminder.enabled", label: "启用提醒工具", kind: "switch", hint: "关闭后模型无法设置提醒，已设的提醒照常触发" },
            { path: "reminder.max_pending_per_chat", label: "每会话提醒上限", kind: "number", step: "1", min: 0, max: 50, hint: "同一会话最多同时挂多少条待执行提醒" },
            { path: "reminder.max_delay_minutes", label: "最远提前（分钟）", kind: "number", step: "60", min: 1, hint: "超过这个跨度的提醒请求会被拒绝" },
            { path: "reminder.max_text_chars", label: "提醒正文上限（字）", kind: "number", step: "10", min: 20, max: 2000, hint: "超出部分会被截断" },
          ],
          draft
        ),
      }),
    },
    {
      path: "proactive.enabled",
      node: card("冷场开口", {
        hint: "默认关闭：群里长时间没人才可能主动说一句，宁可漏不可扰",
        body: fieldGrid(
          [
            { path: "proactive.enabled", label: "启用冷场开口", kind: "switch", hint: "只在群聊里生效，私聊不主动开口" },
            { path: "proactive.idle_hours", label: "安静多久才考虑（小时）", kind: "number", step: "0.5", min: 0.1, hint: "距上一条消息超过这个时长" },
            { path: "proactive.daily_limit", label: "每群每日上限", kind: "number", step: "1", min: 0, max: 100, hint: "每个群每天最多主动开口几次" },
            { path: "proactive.cooldown_minutes", label: "评估冷却（分钟）", kind: "number", step: "5", min: 0, hint: "没开口时，隔多久再评估一次" },
            { path: "proactive.quiet_hours", label: "不开口时段", kind: "list", wide: true, placeholder: "例如 23:00-08:00", hint: "格式 HH:MM-HH:MM，可写多个；这段时间内不主动开口" },
            { path: "proactive.base", label: "基础意愿", kind: "number", step: "0.01", min: 0, max: 1, hint: "主动开口场景的基础分，给得低大多数时候不开口" },
            { path: "proactive.threshold", label: "开口阈值", kind: "number", step: "0.01", min: 0, max: 1, hint: "分数达到多少才开口" },
            { path: "proactive.threshold_noise", label: "阈值随机扰动", kind: "number", step: "0.01", min: 0, max: 1, hint: "阈值附近的随机波动幅度" },
            { path: "proactive.threshold_noise_band", label: "扰动生效范围", kind: "number", step: "0.01", min: 0, max: 1, hint: "离阈值多近才摇骰子" },
            { path: "proactive.relevance_bonus", label: "相关性加成", kind: "number", step: "0.01", min: 0, max: 10, hint: "群里近期话题命中记忆时最多加多少分" },
            { path: "proactive.relevance_scale", label: "相关性归一尺度", kind: "number", step: "0.5", min: 0.1, hint: "检索分数除以它后封顶" },
          ],
          draft
        ),
      }),
    },
    {
      path: "plugins.dir",
      node: card("插件市场", {
        hint: "索引地址与远程来源；装卸启停在插件市场页",
        body: h(
          "div",
          { style: { display: "grid", gap: "16px" } },
          fieldGrid(
            [
              {
                path: "market.index_url",
                label: "索引地址",
                hint: "索引仓库 plugins.json 的 raw 地址；留空或拉取失败时回退内置官方索引",
                wide: true,
                mono: true,
              },
            ],
            draft
          ),
          pluginEditor(draft, pluginsAvailable)
        ),
      }),
    },
    {
      path: "adapters.console",
      node: card("适配器", {
        hint: "连接参数改动需重启",
        body: adaptersEditor(draft, secrets),
      }),
    },
    {
      path: "gateway.enabled",
      node: card("网关与事件通道", {
        hint: "默认关闭：开启后模型可查/调平台接口，notice 与 request 事件也会进事件流",
        body: fieldGrid(
          [
            { path: "gateway.enabled", label: "启用适配器网关", kind: "switch", hint: "关时模型看不见 adapter_actions / adapter_call" },
            { path: "gateway.judge_events", label: "事件快判", kind: "switch", hint: "关时事件只落库；开时才让模型判断要不要动作" },
            { path: "gateway.allow_sensitive", label: "允许敏感动作", kind: "switch", hint: "踢人/禁言/撤回/处理申请等；开启后每次调用都会标红审计" },
            { path: "gateway.enabled_conversations", label: "允许的会话", kind: "list", wide: true, placeholder: "例如 snowluma:123456789", hint: "platform:chat_id；私聊会话恒可，群聊需在这里列出" },
            { path: "gateway.quiet_hours", label: "安静时段", hint: "HH:MM-HH:MM；这段时间里非必判事件只记录，必判事件也只挂待办" },
            { path: "gateway.daily_budget_per_conversation", label: "每会话每日额度", kind: "number", step: "1", min: 0, hint: "每个会话每天最多主动动作次数" },
            { path: "gateway.global_daily_cap", label: "全局每日上限", kind: "number", step: "1", min: 0, hint: "所有会话合计的熔断线" },
          ],
          draft
        ),
      }),
    },
    {
      path: "webui.enabled",
      node: card("WebUI 与高级", {
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
    },
    {
      path: "persona.path",
      node: card("人格与数据", {
        hint: "路径改动需重启",
        body: fieldGrid(
          [
            { path: "persona.path", label: "人格文件", hint: "SOUL.md 风格；路径不变时保存会顺带重读内容", wide: true, mono: true, restart: true },
            { path: "database_path", label: "数据库文件", hint: "SQLite 路径；改动需重启", wide: true, mono: true, restart: true },
          ],
          draft
        ),
      }),
    },
  ];
}

/** 只有系统页展示的纯前端设置（主题、原始 YAML），不参与配置字段归类。 */
function stickerLibraryCard() {
  const box = h("div", { class: "sticker-library-grid" });
  const load = async () => {
    try {
      const data = await api.get("/api/stickers");
      const items = data.items || [];
      if (!items.length) {
        box.replaceChildren(emptyState("image", "表情库还是空的", "打开收集后，群聊里的图片和贴纸会自动入库。"));
        return;
      }
      const tiles = items.map((item) => {
        const remove = h("button", {
          class: "icon-btn-sm danger",
          type: "button",
          title: "删除",
          "aria-label": `删除表情 ${item.id}`,
          onclick: async () => {
            if (!window.confirm("确定删除这条表情吗？")) return;
            try {
              await api.del(`/api/stickers/${encodeURIComponent(item.id)}`);
              toast("表情已删除");
              await load();
            } catch (err) {
              if (!(err instanceof Unauthorized)) toast(err.message || "删除失败", "error");
            }
          },
        }, icon("trash"));
        const image = item.image_url
          ? h("img", { src: item.image_url, alt: (item.tags || []).join("、") || "表情" })
          : h("div", { class: "sticker-missing" }, "无图片");
        return h("article", { class: "sticker-tile" }, image,
          h("div", { class: "sticker-tile-foot" },
            h("span", {}, (item.tags || []).join("、") || "未分类"), remove));
      });
      box.replaceChildren(...tiles);
    } catch (err) {
      if (!(err instanceof Unauthorized)) box.replaceChildren(emptyState("warning", "表情库读取失败", err.message));
    }
  };
  const cardNode = card("表情库", { hint: "图片本体与索引存放在 stickers.dir", body: box });
  load();
  return cardNode;
}

function systemExtras(data) {
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

  // 整份文件可能在命令、URL、参数或任意请求头中含凭据，不能安全回显。
  if (data.raw_hidden) return [appearance, card("高级：原始 YAML", {
    body: h("p", { class: "field-hint" }, "为避免暴露连接凭据，含 MCP 服务器或无法解析的配置不显示原文。请使用设置表单，或在本机直接编辑文件。"),
  })];
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
  return [appearance, advanced];
}

/**
 * 「机器人」与「设置」共用的分组渲染：一级选项卡 + 左侧二级菜单。
 *
 * 分组、页面与字段归属都来自后端 ``/api/config`` 的 ``layout``；
 * 这里只负责按当前页面筛卡片并渲染。
 */
function settingsMenuSkeleton(title) {
  return h(
    "nav",
    { class: "settings-menu", "aria-label": `${title}分类`, "aria-busy": "true" },
    h("div", { class: "settings-menu-group" }, title),
    ...Array.from({ length: 5 }, (_, index) =>
      h("div", { class: "skeleton", style: { height: "36px", width: `${72 - index * 6}%`, margin: "2px 12px" } })
    )
  );
}

async function renderSettingsGroup(view, groupId, sub) {
  // 先把页头和分类菜单骨架放进 DOM；配置接口慢时也能立即确认已切页。
  const groupTitle = groupId === "bot" ? "机器人" : "设置";
  const groupSub = groupId === "bot"
    ? "人设、聊天、表达、记忆、情绪、语音与表情"
    : "模型、搜索、主动触达、适配器与运行环境";
  setHeader(groupTitle, groupSub, []);
  const menuHost = settingsMenuSkeleton(groupTitle);
  const content = h(
    "div",
    { class: "settings-content" },
    h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(3)))
  );
  view.replaceChildren(h("div", { class: "settings-layout" }, menuHost, content));

  const data = await api.get("/api/config");
  const layout = data.layout || { groups: [] };
  const { group, page } = findPage(layout, groupId, sub);
  if (!group || !page) {
    menuHost.replaceChildren();
    content.replaceChildren(emptyState("warning", "没有这个分类", "刷新页面后再试"));
    return;
  }
  setHeader(`${group.title} · ${page.title}`, page.sub || group.sub, []);
  const menu = settingsMenu(layout, group, page);
  menuHost.replaceChildren(...menu.childNodes);
  menuHost.setAttribute("aria-label", `${group.title}分类`);
  menuHost.removeAttribute("aria-busy");

  // 人设页是 SOUL 编辑器，不走结构化表单；保存、备份与热重载保持原样
  if (page.id === "tools" && group.id === "bot") {
    content.replaceChildren(mcpStatusCard());
    return;
  }
  if (page.id === "persona" && group.id === "bot") {
    content.replaceChildren(...(await personaNodes()));
    return;
  }

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
      h("div", { class: "row-title" }, group.title),
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

  // 卡片按后端规则落到当前二级页面；同页卡片保持清单里的顺序
  const cards = settingsCardSpecs(draft, secrets, data.plugins_available || [])
    .filter((spec) => pageOf(layout, spec.path) === page.id)
    .map((spec) => spec.node);

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

  const extras = page.id === "system" ? systemExtras(data) : [];
  if (page.id === "emoji") extras.push(stickerLibraryCard());
  if (page.id === "computer") extras.push(computerAuditCard());
  const hasContent = cards.length > 0 || extras.length > 0;
  const stack = hasContent
    ? h("div", { class: "settings-stack" }, ...cards, ...extras)
    : page.empty
    ? emptyState("plugins", page.title, page.empty)
    : emptyState("settings", page.title, "这个分类暂时没有可调参数。");

  // 没有结构化字段的页面（例如「表情」「人设」）不显示保存栏，避免误点
  content.replaceChildren(
    ...[saveBar, noticeBox, errorBox, pendingCard, stack].filter((node) => node && (node !== saveBar || hasContent))
  );
}

async function renderBot(view, sub) {
  await renderSettingsGroup(view, "bot", sub);
}

async function renderSettings(view, sub) {
  await renderSettingsGroup(view, "settings", sub);
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

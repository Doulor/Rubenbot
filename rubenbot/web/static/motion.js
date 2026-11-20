/* 所有位移共用一套弹簧和一个按需启动的帧循环。 */
"use strict";

const Motion = (() => {
  const tuning = Object.freeze({
    // ζ = damping / (2 √(stiffness · mass)) = 0.8，一次约 1.5% 的轻微过冲。
    stiffness: 484, damping: 35.2, mass: 1,
    maxStep: 1 / 240, maxFrame: 0.064, restDistance: 0.001, restSpeed: 0.035,
    enterOffset: 8, slideOffset: 12, dialogOffset: 10, dialogScale: 0.96,
    pressScale: 0.96,
  });
  const preference = matchMedia("(prefers-reduced-motion: no-preference)");
  const enabled = () => preference.matches;
  const active = new Map();
  let frame = null;

  /** 对 m·x″ = −k(x−目标) − c·x′ 做中点积分；切后台不补算积压的帧。 */
  function advance(state, target, seconds, config = tuning) {
    const dt = Math.min(Math.max(0, seconds), config.maxFrame);
    const steps = Math.max(1, Math.ceil(dt / config.maxStep));
    const h = dt / steps;
    const acceleration = (x, v) => (config.stiffness * (target - x) - config.damping * v) / config.mass;
    for (let i = 0; i < steps; i++) {
      const { x, v } = state;
      const midV = v + acceleration(x, v) * h / 2;
      state.x += midV * h;
      state.v += acceleration(x + v * h / 2, midV) * h;
    }
    const atRest = Math.abs(state.x - target) <= config.restDistance && Math.abs(state.v) <= config.restSpeed;
    if (atRest) { state.x = target; state.v = 0; }
    return atRest;
  }

  function settle(element, entry) {
    active.delete(element);
    entry.paint(entry.target);
    entry.finish?.();
  }

  function tick(now) {
    frame = null;
    for (const [element, entry] of active) {
      if (!element.isConnected) { active.delete(element); continue; }
      const done = advance(entry, entry.target, (now - entry.time) / 1000);
      entry.time = now;
      if (done) settle(element, entry);
      else entry.paint(entry.x);
    }
    schedule();
  }

  function schedule() {
    if (active.size && frame === null) frame = requestAnimationFrame(tick);
  }

  /** 同一元素换目标保留当前位置和速度；restart 用于新内容或重新量过的起点。 */
  function spring(element, from, to, paint, { finish, restart = false } = {}) {
    const prior = !restart && active.get(element);
    const entry = { x: prior ? prior.x : from, v: prior ? prior.v : 0,
      target: to, time: performance.now(), paint, finish };
    if (!enabled() || (entry.x === to && entry.v === 0)) { settle(element, entry); return; }
    active.set(element, entry);
    paint(entry.x);
    schedule();
  }

  preference.addEventListener("change", () => {
    if (enabled()) return;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    for (const [element, entry] of active) settle(element, entry);
  });

  function presence(element, entering, horizontal = false, finish) {
    const distance = horizontal ? tuning.slideOffset : tuning.enterOffset;
    spring(element, entering ? 0 : 1, entering ? 1 : 0, p => {
      element.style.transform = `translate${horizontal ? 'X' : 'Y'}(${(1 - p) * distance}px)`;
      element.style.opacity = Math.max(0, Math.min(1, p));
    }, { restart: entering, finish() {
      element.style.transform = element.style.opacity = '';
      finish?.();
    } });
  }

  // 目标已在最终布局中；只把背景从旧的绘制位置移来，不缩放标签文字。
  function indicator(element, from, to, finish) {
    spring(element, 0, 1, p => {
      const d = 1 - p;
      element.style.transform = `translate(${(from.left - to.left) * d}px, ${(from.top - to.top) * d}px) scale(${1 + (from.width / to.width - 1) * d}, ${1 + (from.height / to.height - 1) * d})`;
    }, { restart: true, finish });
  }

  // 菜单本身可能随路由重建。只在切换时量旧药丸和新选项；每帧只画背景。
  function selection(group) {
    if (!enabled() || !group) return null;
    const current = group.querySelector('.active');
    return (current?.querySelector('.motion-pill') || current)?.getBoundingClientRect();
  }

  function moveSelection(group, from) {
    if (!enabled() || !from || !group) return;
    const item = group.querySelector('.active');
    if (!item) return;
    group.querySelector('.motion-pill')?.remove();
    const to = item.getBoundingClientRect();
    if (!from.width || !to.width || !to.height) return;
    if (from.left === to.left && from.top === to.top && from.width === to.width && from.height === to.height) return;
    const pill = document.createElement('i');
    pill.className = 'motion-pill';
    pill.setAttribute('aria-hidden', 'true');
    item.append(pill);
    indicator(pill, from, to, () => pill.remove());
  }

  function dialogMotion(dialog, opening) {
    if (opening) {
      dialog.inert = false;
      if (!dialog.open) dialog.showModal();
    } else {
      if (!dialog.open || dialog.inert) return;
      dialog.inert = true;
    }
    spring(dialog, opening ? 0 : 1, opening ? 1 : 0, p => {
      dialog.style.transform = `translateY(${(1 - p) * tuning.dialogOffset}px) scale(${tuning.dialogScale + (1 - tuning.dialogScale) * p})`;
      dialog.style.opacity = Math.max(0, Math.min(1, p));
    }, { finish() {
      if (!opening) { dialog.close(); dialog.inert = false; }
      dialog.style.transform = dialog.style.opacity = '';
    } });
  }

  function switchThumb(thumb, checked) {
    spring(thumb, checked ? 0 : 1, checked ? 1 : 0, p => {
      thumb.style.transform = `translateX(${p * 100}%)`;
    });
  }

  function bindPress() {
    let pressed = null;
    function press(element) {
      if (!enabled() || !element || element.disabled || pressed === element) return;
      release();
      pressed = element;
      scalePress(element, true);
    }
    function scalePress(element, down) {
      spring(element, down ? 0 : 1, down ? 1 : 0, p => {
        element.style.transform = `scale(${1 - p * (1 - tuning.pressScale)})`;
      }, { finish: () => { if (!down || !enabled()) element.style.transform = ''; } });
    }
    function release() {
      if (!pressed) return;
      scalePress(pressed, false);
      pressed = null;
    }
    const control = target => target.closest?.('button, .chat-open, .emotion-person, [role="button"]');
    document.addEventListener('pointerdown', e => { if (e.isPrimary && e.button === 0) press(control(e.target)); });
    for (const type of ['pointerup', 'pointercancel']) window.addEventListener(type, e => { if (e.isPrimary) release(); });
    document.addEventListener('keydown', e => { if (!e.repeat && [' ', 'Enter'].includes(e.key)) press(control(e.target)); });
    document.addEventListener('keyup', e => { if ([' ', 'Enter'].includes(e.key)) release(); });
    window.addEventListener('blur', release);
    document.addEventListener('focusout', e => { if (e.target === pressed) release(); });
    preference.addEventListener('change', () => { if (!enabled()) release(); });
  }

  return { tuning, enabled, advance, spring, selection, moveSelection, dialogMotion, switchThumb, bindPress,
    enter: (element, horizontal) => presence(element, true, horizontal),
    leave: (element, finish) => presence(element, false, false, finish) };
})();

const leavingCopies = new Map();
function clearLeavingCopies(owner) {
  for (const [key, copy] of leavingCopies) {
    if (!owner || key === owner) {
      copy.remove();
      leavingCopies.delete(key);
    }
  }
}

/** replaceChildren 同步更新真实列表；离场副本不占位置、不接收输入。 */
function markDeparture(owner, row) {
  clearLeavingCopies(owner);
  if (!row?.isConnected || !Motion.enabled()) return;
  const box = row.getBoundingClientRect();
  if (!box.width || !box.height || box.bottom < 0 || box.top > innerHeight) return;
  const copy = row.cloneNode(true);
  copy.removeAttribute("id");
  for (const child of copy.querySelectorAll("[id]")) child.removeAttribute("id");
  copy.inert = true;
  copy.setAttribute("aria-hidden", "true");
  copy.classList.add("motion-copy");
  Object.assign(copy.style, {
    left: `${scrollX + box.left}px`, top: `${scrollY + box.top}px`,
    width: `${box.width}px`, height: `${box.height}px`,
  });
  document.body.append(copy);
  leavingCopies.set(owner, copy);
  Motion.leave(copy, () => {
    if (leavingCopies.get(owner) === copy) clearLeavingCopies(owner);
  });
}

function markRouteChange(view, changed) {
  clearLeavingCopies();
  if (changed) markDeparture(view, view.querySelector(".settings-savebar"));
}

function markListChange(list, nextKeys) {
  if (!list.isConnected || !Motion.enabled()) return;
  const selector = ":scope > [data-motion-key]";
  const previous = [...list.querySelectorAll(selector)];
  const keys = new Set(previous.map(row => row.dataset.motionKey));
  const next = new Set(nextKeys);
  markDeparture(list, previous.find(row => !next.has(row.dataset.motionKey)));
  // 只在实际增删时遍历；滚动、轮询不测量行高，不为保留项启动帧循环。
  queueMicrotask(() => {
    for (const row of list.querySelectorAll(selector)) {
      if (!keys.has(row.dataset.motionKey)) Motion.enter(row);
    }
  });
}

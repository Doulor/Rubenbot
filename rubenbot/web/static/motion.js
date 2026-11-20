/* 纯绘制钩子。渲染器仍然同步替换节点，任何保存、删除或导航都不等动效。 */
"use strict";

const motionPreference = matchMedia("(prefers-reduced-motion: no-preference)");
const leavingCopies = new Map();

function clearLeavingCopies(owner) {
  for (const [key, copy] of leavingCopies) {
    if (!owner || key === owner) {
      copy.remove();
      leavingCopies.delete(key);
    }
  }
}

/** replaceChildren 不能做 CSS 离场：只留下旧项的不可交互绘制副本。 */
function markDeparture(owner, row) {
  clearLeavingCopies(owner);
  if (!row?.isConnected || !motionPreference.matches) return;
  const box = row.getBoundingClientRect();
  if (!box.width || !box.height || box.bottom < 0 || box.top > innerHeight) return;
  const copy = row.cloneNode(true);
  copy.removeAttribute("id");
  for (const child of copy.querySelectorAll("[id]")) child.removeAttribute("id");
  copy.inert = true;
  copy.setAttribute("aria-hidden", "true");
  copy.classList.remove("item-arriving");
  copy.classList.add("motion-copy", "is-leaving");
  Object.assign(copy.style, {
    left: `${scrollX + box.left}px`, top: `${scrollY + box.top}px`,
    width: `${box.width}px`, height: `${box.height}px`,
  });
  document.body.append(copy);
  leavingCopies.set(owner, copy);
  Promise.allSettled(copy.getAnimations().map((animation) => animation.finished)).then(() => {
    if (leavingCopies.get(owner) === copy) clearLeavingCopies(owner);
  });
}

function markRouteChange(view, changed) {
  clearLeavingCopies();
  if (changed) markDeparture(view, view.querySelector(".settings-savebar"));
}

function markListChange(list, nextKeys) {
  if (!list.isConnected || !motionPreference.matches) return;
  const selector = ":scope > [data-motion-key]";
  const previous = [...list.querySelectorAll(selector)];
  const keys = new Set(previous.map((row) => row.dataset.motionKey));
  const removed = previous.find((row) => !nextKeys.includes(row.dataset.motionKey));
  markDeparture(list, removed);
  // 在同步重绘后、浏览器绘制前打钩子；未变的兄弟项不重播。
  queueMicrotask(() => {
    for (const row of list.querySelectorAll(selector)) {
      row.classList.toggle("item-arriving", !keys.has(row.dataset.motionKey));
    }
  });
}

motionPreference.addEventListener("change", () => clearLeavingCopies());

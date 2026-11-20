// 用可控帧时钟运行浏览器使用的同一份 JS，不依赖动画计时器或屏幕刷新率。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/motion.js'), 'utf8');

function host() {
  let now = 0, next = 0, change;
  const frames = new Map();
  const preference = {matches: true, addEventListener: (_, fn) => { change = fn; }};
  const motion = vm.runInNewContext(source + ';Motion', {
    matchMedia: () => preference, performance: {now: () => now},
    requestAnimationFrame: fn => { frames.set(++next, fn); return next; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  return { motion, frames, reduce() { preference.matches = false; change(); }, tick() {
    now += 1000 / 60;
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach(fn => fn(now));
  } };
}

function stepResponse(motion, hz, config = motion.tuning) {
  const state = {x: 0, v: 0};
  let peak = 0, crossings = 0, side = -1;
  for (let i = 1; i < hz * 3; i++) {
    const done = motion.advance(state, 1, 1 / hz, config);
    const sign = Math.sign(state.x - 1);
    if (sign && side !== sign) { crossings++; side = sign; }
    peak = Math.max(peak, state.x);
    if (done) return {peak, crossings, seconds: i / hz, ...state};
  }
  assert.fail('三秒内未收敛');
}

for (const hz of [30, 60, 120, 144]) {
  test(`${hz} Hz：一次轻微过冲，约 400 ms 收敛并停在终值`, () => {
    const r = stepResponse(host().motion, hz);
    assert.equal(r.crossings, 1);
    assert.ok(r.peak > 1.01 && r.peak < 1.02);
    assert.ok(r.seconds > 0.35 && r.seconds < 0.45);
    assert.equal(r.x, 1);
    assert.equal(r.v, 0);
  });
}

test('质量、刚度、阻尼可调；临界阻尼无过冲，质量增加时响应变慢', () => {
  const {motion} = host();
  const config = {...motion.tuning, stiffness: 256, damping: 32, mass: 1};
  const critical = stepResponse(motion, 60, config);
  assert.equal(critical.crossings, 0);
  const heavy = stepResponse(motion, 60, {...config, mass: 4, damping: 64});
  assert.equal(heavy.crossings, 0);
  assert.ok(heavy.seconds > critical.seconds * 1.7);
});

test('一个共享帧循环；反向接续速度，卸载和收敛后不再请求帧', () => {
  const {motion, tick, frames} = host();
  const a = {isConnected: true}, b = {isConnected: true};
  let x, renders = 0, finishes = 0;
  const paint = p => { x = p; };
  motion.spring(a, 0, 1, paint, {finish: () => assert.fail('旧目标不应触发完成')});
  motion.spring(b, 0, 1, () => renders++);
  assert.equal(frames.size, 1);
  tick(); tick();
  const current = x;
  motion.spring(a, 1, 0, paint, {finish: () => finishes++});
  assert.equal(x, current);
  b.isConnected = false;
  tick();
  assert.ok(x > current, '反向的第一帧仍有正向动量');
  for (let i = 0; i < 60; i++) tick();
  assert.equal(renders, 3);
  assert.equal(finishes, 1);
  assert.equal(x, 0);
  assert.equal(frames.size, 0);
});

test('弹窗快速关开不会被旧回调关闭；切到减少动态只关闭一次', () => {
  const {motion, frames, tick, reduce} = host();
  let closed = 0;
  const dialog = {isConnected: true, style: {}, open: false,
    showModal() { this.open = true; }, close() { this.open = false; closed++; }};
  motion.dialogMotion(dialog, true);
  assert.equal(dialog.style.transform, 'translateY(10px) scale(0.96)');
  tick(); tick();
  const current = dialog.style.transform;
  motion.dialogMotion(dialog, false);
  assert.equal(dialog.style.transform, current);
  assert.equal(dialog.inert, true);
  tick();
  motion.dialogMotion(dialog, true);
  for (let i = 0; i < 60; i++) tick();
  assert.equal(closed, 0);
  assert.equal(dialog.inert, false);
  assert.equal(dialog.style.transform, '');
  motion.dialogMotion(dialog, false);
  motion.dialogMotion(dialog, false);
  reduce();
  assert.equal(closed, 1);
  assert.equal(dialog.open, false);
  assert.equal(frames.size, 0);
});

test('切换为减少动态：立即完成在途离场，不为下一次入场申请帧', () => {
  const {motion, frames, reduce, tick} = host();
  const element = {style: {}, isConnected: true};
  let cleaned = 0;
  motion.leave(element, () => cleaned++);
  tick();
  reduce();
  assert.equal(cleaned, 1);
  assert.equal(element.style.transform, '');
  assert.equal(element.style.opacity, '');
  motion.enter(element);
  assert.equal(frames.size, 0);
  assert.equal(element.style.transform, '');
});

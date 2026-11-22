const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const index = fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/index.html'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/styles.css'), 'utf8');

function navEntries(html) {
  const nav = html.match(/<nav class="nav"[\s\S]*?<\/nav>/g) || [];
  return nav.map(block => [...block.matchAll(/href="([^"]+)" data-page="([^"]+)"/g)].map(match => match.slice(1)));
}

test('移动端抽屉与桌面侧栏顺序一致，模型紧跟机器人', () => {
  const navs = navEntries(index);
  assert.equal(navs.length, 2);
  assert.deepEqual(navs[0], navs[1]);
  assert.deepEqual(navs[0].map(([, page]) => page),
    ['overview', 'bot', 'models', 'memory', 'people', 'groups', 'plugins', 'logs', 'settings', 'about']);
  assert.equal(navs[0].at(-1)[0], '#/about');
  assert.equal(navs[0][2][0], '#/models');
});

test('移动端不再使用底部 dock，并提供抽屉关闭与滚动锁定逻辑', () => {
  assert.match(css, /@media \(max-width: 768px\)/);
  assert.doesNotMatch(css, /底部悬浮 dock/);
  assert.match(css, /\.mobile-drawer-layer\.is-open \.mobile-drawer/);
  assert.match(css, /body\.mobile-drawer-open\s*\{[\s\S]*?overflow: hidden/);
  assert.match(app, /mobile-scrim/);
  assert.match(app, /mobile-drawer-close/);
  assert.match(app, /event\.key === "Escape"/);
  assert.match(app, /clientX - startX < -48/);
});

test('抽屉使用弹簧量级缓动并支持减少动态', () => {
  assert.match(css, /340ms cubic-bezier\(0\.32, 0\.72, 0, 1\)/);
  assert.match(css, /prefers-reduced-motion: reduce/);
});

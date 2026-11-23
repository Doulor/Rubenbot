// 在真实路由和插件渲染器上控制 API 响应顺序，不用计时估算是否“足够快”。
const test = require('node:test');
const assert = require('node:assert/strict');
const {loadUI} = require('./dom.cjs');

function setup() {
  const {run} = loadUI();
  run(`for (const id of ['app', 'login', 'view', 'page-title', 'page-sub', 'page-actions']) root.append(h('div', {id}));
    document.querySelectorAll = () => [];
    const schema = [{key: 'name', type: 'string', description: '名字'}];
    const pending = {};
    api.get = async path => {
      if (path === '/api/plugins') return {items: [{name: 'demo', valid: true, manifest: {id: 'demo', config_schema: schema}}]};
      if (path === '/api/chats?limit=300') return {items: []};
      return new Promise((resolve, reject) => { pending[path] = {resolve, reject}; });
    };`);
  const route = hash => run(`location.hash = ${JSON.stringify(hash)}; renderRoute()`);
  const market = name => run(`pending['/api/market'].resolve({items: [{id: 'demo', state: 'enabled', name: ${JSON.stringify(name)}}]})`);
  return {run, route, market, view: run(`$('#view')`)};
}

const hasSkeleton = view => view.walk().some(n => n.className.split(' ').includes('skeleton'));

test('首次进市场显示市场骨架，切回时同步显示旧商品；刷新无骨架，失败保留旧内容', async () => {
  const {route, market, run, view} = setup();
  const first = route('#/plugins');
  assert.ok(view.querySelector('.market-grid') && hasSkeleton(view));
  market('旧商品'); await first;
  await route('#/groups');
  const revisit = route('#/plugins');
  assert.match(view.textContent, /旧商品/);
  assert.equal(hasSkeleton(view), false);
  market('新商品'); await revisit;
  assert.match(view.textContent, /新商品/);
  assert.doesNotMatch(view.textContent, /旧商品/);
  await route('#/groups');
  const offline = route('#/plugins');
  run(`pending['/api/market'].reject(Error('暂时断网'))`); await offline;
  assert.match(view.textContent, /新商品/);
  assert.equal(hasSkeleton(view), false);
});

test('模型页首访显示两张卡的骨架，旧设置链接回切复用模型页缓存', async () => {
  const {route, run, view} = setup();
  const first = route('#/models');
  const cards = () => view.querySelector('.settings-stack').childNodes;
  assert.equal(cards().length, 2);
  assert.ok(hasSkeleton(view));
  assert.equal(view.querySelector('.settings-menu'), null);
  run(`const config = {layout: {groups: [{id: 'models', title: '模型', pages: [{id: 'models'}]}],
    rules: [{prefix: 'llm', page: 'models'}]}, structured: {llm: {providers: [{name: 'local', base_url: 'http://local/v1', models: ['test-chat']}], role_pools: {chat: [{provider: 'local', model: 'test-chat'}]}}, mcp: {servers: []}, decision: {groups: {}}}};
    pending['/api/config'].resolve(config);`);
  await first;
  assert.ok(view.querySelector('.settings-stack'), view.textContent);
  assert.deepEqual(cards().map(n => n.querySelector('h2').textContent), ['模型配置', '调用参数']);
  assert.equal(run(`$('#page-title').textContent`), '模型');
  assert.equal(view.querySelector('.settings-menu'), null);
  const stack = view.querySelector('.settings-stack');
  await route('#/groups');
  const revisit = route('#/settings/model');
  assert.equal(view.querySelector('.settings-stack'), stack);
  run(`pending['/api/config'].resolve(config)`); await revisit;
  assert.equal(hasSkeleton(view), false);
});

const sha = 'abcdef0'.padEnd(40, '1');
const repository = 'https://github.com/Doulor/Rubenbot';
const about = {
  project: {name: 'RubenBot', version: '1.2.3', repository_url: repository, commit_source: 'deploy', commit: sha, commit_url: `${repository}/commit/${sha}`},
  license: {name: 'MIT', text: 'MIT License\n\nCopyright (c) test'},
  runtime: {models: {chat_model: 'base-chat', gate_model: null}, roles: {chat: {model: 'pool-first', count: 2, strategy: 'round_robin'}},
    model_count: 2, model_strategy: 'failover', adapters: ['console', 'snowluma'], snowluma_status: 'connected', plugins: {total: 6, enabled: 0}},
};

async function loadAbout(ctx, data = about) {
  const first = ctx.route('#/about');
  ctx.run(`pending['/api/about'].resolve(${JSON.stringify(data)})`);
  await first;
}

test('关于首访与实页布局同构，回切先读缓存，随后可展示未知提交及其原因', async () => {
  const {route, run, view} = setup();
  const first = route('#/about');
  const layout = () => view.walk().filter(n => n.matches('section')).map(n => n.className);
  const skeleton = layout();
  assert.ok(hasSkeleton(view));
  assert.equal(skeleton.length, 4);
  run(`pending['/api/about'].resolve(${JSON.stringify(about)})`);
  await first;
  assert.deepEqual(layout(), skeleton);
  assert.equal(run(`$('#page-title').textContent`), '关于');
  for (const text of ['v1.2.3', 'abcdef0', 'base-chat', 'pool-first', '2 个 · 轮询', '已连接', '已发现 6 · 已启用 0']) {
    assert.ok(view.textContent.includes(text), text);
  }
  assert.equal(view.querySelector('pre').textContent, about.license.text);
  const links = view.walk().filter(n => n.matches('a'));
  assert.deepEqual(links.map(n => n.getAttribute('href')), [repository, `${repository}/commit/${sha}`]);
  assert.equal(links[1].getAttribute('title'), `部署盖戳 · ${sha}`);
  assert.ok(links.every(n => n.getAttribute('target') === '_blank' && n.getAttribute('rel').includes('noopener')));
  assert.equal(run(`'/api/about/update' in pending`), false);
  const cached = view.querySelector('.about-page');
  await route('#/groups');
  const revisit = route('#/about');
  assert.equal(view.querySelector('.about-page'), cached);
  const unknown = {...about, project: {...about.project, commit: null, commit_url: null, commit_source: 'unknown', commit_reason: '无盖戳，Git 不可用'}};
  run(`pending['/api/about'].resolve(${JSON.stringify(unknown)})`);
  await revisit;
  assert.match(view.textContent, /未知（无盖戳，Git 不可用）/);
  assert.ok(!view.walk().some(n => n.getAttribute('href')?.includes('/commit/')));
});

test('更新仅由按钮触发，可重试；最新、可更新和失败都不替换其余信息', async () => {
  const ctx = setup();
  const {run, view} = ctx;
  await loadAbout(ctx);
  const btn = view.querySelector('button'), result = view.querySelector('.about-result'), license = view.querySelector('details');
  for (const [status, expected] of [['current', '已是最新'], ['available', '有更新：abcdef0'], ['error', '检查失败，请稍后再试'], ['offline', '检查失败，请稍后再试']]) {
    const check = btn.fire('click');
    assert.equal(btn.disabled, true);
    assert.equal(result.textContent, '正在检查…');
    if (status === 'offline') run(`pending['/api/about/update'].reject(Error('内部详情'))`);
    else run(`pending['/api/about/update'].resolve(${JSON.stringify({status, message: expected, latest_commit: sha, commit_url: `${repository}/commit/${sha}`})})`);
    await check;
    assert.equal(result.textContent, expected);
    if (status === 'available') assert.equal(result.querySelector('a').getAttribute('href'), `${repository}/commit/${sha}`);
    assert.equal(btn.disabled, false);
    assert.equal(view.querySelector('details'), license);
  }
});

test('缓存中开始编辑的表单不被后台结果覆盖；下次访问不带回草稿', async () => {
  const {route, run, view} = setup();
  const edit = () => route('#/plugins/config/demo');
  const complete = async (load, name) => {
    await new Promise(resolve => setImmediate(resolve));
    run(`pending['/api/plugins/demo/config'].resolve({schema, values: {name: ${JSON.stringify(name)}}})`);
    await load;
  };
  await complete(edit(), '旧名');
  await route('#/groups');
  const fresh = edit();
  const input = view.querySelector('input');
  assert.equal(input.value, '旧名');
  input.value = '编辑中';
  await input.fire('input');
  // 轻量 DOM 宿主不传播事件，显式送达页面的捕获监听器。
  await view.querySelector('.route-page').fire('input', {target: input});
  await complete(fresh, '服务器新名');
  assert.equal(view.querySelector('input'), input);
  assert.equal(input.value, '编辑中');
  await route('#/groups');
  const again = edit();
  assert.equal(view.querySelector('input').value, '服务器新名');
  await complete(again, '服务器新名');
});

test('切回概览恢复唯一轮询；离开后迟到的概览请求不能重启它', async () => {
  const {route, run, view} = setup();
  run(`const intervals = new Map(); let nextTimer = 0;
    globalThis.setInterval = (fn, ms) => { intervals.set(++nextTimer, ms); return nextTimer; };
    globalThis.clearInterval = id => intervals.delete(id);
    const overview = {bot: {name: '运行实例'}, counts: {}, adapters: [], recent_decisions: [], plugins: {}, webui: {}};
    const finishOverview = () => { pending['/api/emotion'].resolve(null); pending['/api/overview'].resolve(overview); };`);
  const first = route('#/overview');
  assert.ok(view.querySelector('.hero') && view.querySelector('.grid-kpi'));
  run('finishOverview()'); await first;
  assert.equal(run('intervals.get(state.pollTimer)'), 6000);
  await route('#/groups');
  assert.equal(run('intervals.size'), 0);
  const late = route('#/overview');
  assert.equal(hasSkeleton(view), false);
  assert.equal(run('intervals.size'), 1);
  await route('#/groups');
  run('finishOverview()'); await late;
  assert.equal(run('intervals.size'), 0);
  assert.match(view.textContent, /还没有群聊记录/);
});

test('旧路由响应不能覆盖当前页，重新登录前清空路由缓存', async () => {
  const {route, market, run, view} = setup();
  const late = route('#/plugins');
  await route('#/groups');
  market('迟到'); await late;
  assert.match(view.textContent, /还没有群聊记录/);
  const current = route('#/plugins'); market('已缓存'); await current;
  run('showLogin()');
  await route('#/groups');
  const afterLogin = route('#/plugins');
  assert.ok(hasSkeleton(view));
  assert.doesNotMatch(view.textContent, /已缓存/);
  market('新会话'); await afterLogin;
});

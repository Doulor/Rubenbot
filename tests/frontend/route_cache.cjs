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

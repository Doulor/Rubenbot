// 用真实路由、渲染器和事件处理器验证插件页面；只替换 API 传输。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

function setup() {
  const {root, run} = loadUI();
  const schema = [
    {key: 'model', type: 'string', description: '模型'},
    {key: 'ready', type: 'bool', description: '开启'},
    {key: 'limit', type: 'number', description: '上限'},
    {key: 'aliases', type: 'list', description: '别名'},
    {key: 'api_key', type: 'string', description: '密钥'},
  ];
  const records = {
    '/api/market': {items: [
      {name: '远程插件', id: 'remote', state: 'not_installed', download_available: true},
      {name: '本地插件', id: 'local-id', state: 'enabled', loaded: true, has_config: true,
        plugin_name: 'local_dir', hot_config: true},
    ]},
    '/api/plugins': {hot_reload: true, items: [
      {name: 'local_dir', valid: true, loaded: true, hot_config: true,
        manifest: {id: 'local-id', name: '本地插件', config_schema: schema},
        permissions: {declared: ['read_messages', 'scheduler'], granted: ['read_messages'], all_granted: false,
          permission_meta: [
            {id: 'read_messages', label: '读消息', description: '读取聊天消息', high_risk: false, declared: true, granted: true},
            {id: 'scheduler', label: '定时任务', description: '注册后台定时任务', high_risk: true, declared: true, granted: false},
          ]}},
      {name: 'empty', valid: true, manifest: {id: 'empty', name: '简单插件', config_schema: []},
        permissions: {declared: [], granted: [], all_granted: true, permission_meta: []}},
    ]},
    '/api/plugins/local-id/config': {schema, values: {model: '默认', ready: false, limit: 1, aliases: ['甲'], api_key: ''}, secrets: {api_key: true}},
  };
  run(`const records = ${JSON.stringify(records)}; const calls = []; let fail = false; let configHot = true;
    window.toasts = [];
    toast = (message, kind = 'ok') => { window.toasts.push([message, kind]); };
    api.get = async path => { calls.push(path); if (!records[path]) throw Error(path); return records[path]; };
    api.post = async (path, body) => { calls.push([path, body]);
      if (path.endsWith('/reload')) return {message: '插件 local_dir 已热重载，授权与配置保持不变。'};
      return {message: '已启用'}; };
    api.put = async (path, body) => { calls.push([path, JSON.parse(JSON.stringify(body))]);
      if (fail) throw Error('校验失败');
      return {message: configHot ? '插件配置已保存并热生效，无需重启。' : '插件配置已保存，重启 bot 后生效。',
        hot: configHot, restart_required: !configHot, secrets: {api_key: true}}; };`);
  const route = href => run(`location.hash = ${JSON.stringify(href)}; PAGES[currentRoute().page].render(root, currentRoute().param)`);
  function node(tag, match = () => true) {
    const found = root.walk().find(n => n.tagName === tag && match(n));
    assert.ok(found, `没找到 ${tag}。页面内容：${root.textContent}`);
    return found;
  }
  const link = label => node('A', n => n.textContent === label);
  return {root, run, route, node, link};
}

test('从市场切到插件列表，默认进配置，按已安装插件切换，再返回市场', async () => {
  const {root, run, route, node, link} = setup();
  await route('#/plugins');
  assert.equal(link('插件市场').getAttribute('aria-current'), 'page');
  for (const label of ['安装', '停用', '卸载']) node('BUTTON', n => n.textContent === label);
  assert.ok(!root.walk().some(n => n.tagName === 'BUTTON' && n.textContent === '配置'));
  await route(link('插件').getAttribute('href'));
  assert.equal(link('插件').getAttribute('aria-current'), 'page');
  // 插件列表默认进配置子 tab，首个插件的表单直接可见
  assert.equal(link('配置').getAttribute('aria-current'), 'page');
  assert.equal(link('本地插件').getAttribute('aria-current'), 'page');
  assert.doesNotMatch(root.textContent, /远程插件/);
  node('FORM');
  await route(link('简单插件').getAttribute('href'));
  assert.equal(link('简单插件').getAttribute('aria-current'), 'page');
  assert.match(root.textContent, /无可配置项/);
  assert.doesNotMatch(root.textContent, /保存配置/);
  assert.deepEqual(JSON.parse(run('JSON.stringify(calls)')), ['/api/market', '/api/plugins', '/api/plugins/local-id/config', '/api/plugins']);
  await route(link('插件市场').getAttribute('href'));
  assert.match(root.textContent, /远程插件/);
});

test('修改 schema 控件，保存失败保留草稿，重试提交至 manifest ID 对应端点', async () => {
  const {root, run, route, node} = setup();
  await route('#/plugins/local-id/config');
  assert.equal(node('A', n => n.textContent === '配置').getAttribute('aria-current'), 'page');
  const field = label => node('INPUT', n => n.getAttribute('aria-label') === label);
  const model = field('模型');
  assert.equal(model.value, '默认');
  model.value = '新模型'; await model.fire('input');
  const limit = field('上限'); limit.value = '17'; await limit.fire('input');
  await node('BUTTON', n => n.getAttribute('role') === 'switch').fire('click');
  const alias = field('添加别名'); alias.value = '乙'; await alias.fire('keydown', {key: 'Enter'});
  const key = field('密钥'); assert.equal(key.getAttribute('type'), 'password');
  assert.equal(key.value, ''); key.value = '新值'; await key.fire('input');
  run('fail = true'); await node('FORM').fire('submit');
  assert.match(root.textContent, /校验失败/);
  assert.equal(model.value, '新模型');
  run('fail = false'); await node('FORM').fire('submit');
  assert.deepEqual(JSON.parse(run('JSON.stringify(calls.at(-1))')), ['/api/plugins/local-id/config',
    {values: {model: '新模型', ready: true, limit: 17, aliases: ['甲', '乙'], api_key: '新值'}}]);
  // 插件声明了 on_config_change（hot_config=true）：如实提示已热生效
  assert.match(root.textContent, /已保存并热生效，无需重启/);
  assert.equal(key.value, '');
  // 没声明钩子的插件：保存后明说需要重启，不假装生效
  run('configHot = false');
  await node('FORM').fire('submit');
  assert.match(root.textContent, /已保存，重启 bot 后生效/);
});

test('已加载插件的市场卡片可以先重载，提示回传后刷新列表', async () => {
  const {root, run, route, node} = setup();
  await route('#/plugins');
  const reload = node('BUTTON', n => n.textContent === '重载');
  assert.match(reload.getAttribute('title'), /on_unload/);
  await reload.fire('click');
  const calls = JSON.parse(run('JSON.stringify(calls)'));
  const reloadCall = calls.find(call => Array.isArray(call) && call[0] === '/api/plugins/local_dir/reload');
  assert.ok(reloadCall, `没有发出重载请求：${JSON.stringify(calls)}`);
  assert.equal(JSON.stringify(reloadCall[1]), '{}');
  // 重载成功后刷新市场列表
  assert.ok(calls.slice(calls.indexOf(reloadCall) + 1).some(call => call === '/api/market'));
  const toasts = JSON.parse(run('JSON.stringify(window.toasts)'));
  assert.match(toasts.at(-1)[0], /已热重载/);
  // 未加载的插件没有可重载的实例，不给按钮
  run(`records['/api/market'].items.find(row => row.id === 'local-id').loaded = false`);
  await route('#/plugins');
  assert.ok(!root.walk().some(n => n.tagName === 'BUTTON' && n.textContent === '重载'));
});

test('没有本地插件、读取错误与迟到的响应都不破坏导航', async () => {
  const {root, run, route, link} = setup();
  run(`let resolveConfig; const get = api.get; api.get = path => path.endsWith('/config')
    ? new Promise(resolve => {resolveConfig = resolve}) : get(path)`);
  const first = route('#/plugins/local-id/config');
  await new Promise(resolve => setImmediate(resolve));
  await route(link('简单插件').getAttribute('href'));
  run(`resolveConfig(records['/api/plugins/local-id/config'])`); await first;
  assert.match(root.textContent, /无可配置项/);
  assert.doesNotMatch(root.textContent, /保存配置/);
  run(`api.get = get; delete records['/api/plugins/local-id/config']`);
  await route('#/plugins/local-id/config');
  assert.match(root.textContent, /读取配置失败/);
  link('简单插件');
  run(`records['/api/plugins'].items = []`);
  await route('#/plugins/installed');
  assert.match(root.textContent, /还没有已安装的插件/);
  link('插件市场');
});

test('市场卡片没有单独的授权操作，详情列出每项声明和实际状态且不能修改', async () => {
  const {root, run, route, node} = setup();
  await route('#/plugins');
  assert.ok(!root.walk().some(n => ['BUTTON', 'A'].includes(n.tagName) && /授权/.test(n.textContent)));
  const detailLink = node('A', n => n.getAttribute('href') === '#/plugins/local-id/detail');
  run(`const perms = records['/api/plugins'].items[0].permissions;
    records['/api/plugins/local-id/detail'] = {plugin: {id: 'local-id', name: '本地插件',
      installed: true, valid: true, permissions: {...perms, items: perms.permission_meta}}}`);
  await route(detailLink.getAttribute('href'));
  assert.match(root.textContent, /共声明 2 项，已授权 1 项/);
  node('LI', n => /读消息.*已授权/.test(n.textContent));
  node('LI', n => /定时任务.*高危.*未授权/.test(n.textContent));
  assert.ok(!root.walk().some(n => n.getAttribute('role') === 'switch'));
  assert.ok(run('calls.every(call => typeof call === "string")'));
});

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
      {name: '本地插件', id: 'local-id', state: 'enabled', has_config: true},
    ]},
    '/api/plugins': {hot_reload: true, items: [
      {name: 'local_dir', valid: true, manifest: {id: 'local-id', name: '本地插件', config_schema: schema},
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
  run(`const records = ${JSON.stringify(records)}; const calls = []; let fail = false;
    api.get = async path => { calls.push(path); if (!records[path]) throw Error(path); return records[path]; };
    api.put = async (path, body) => { calls.push([path, JSON.parse(JSON.stringify(body))]);
      if (path.endsWith('/permissions')) {
        const item = records['/api/plugins'].items.find(row => '/api/plugins/' + row.name + '/permissions' === path);
        const declared = item.permissions.declared;
        const granted = declared.filter(name => body.permissions.includes(name));
        item.permissions = {...item.permissions, granted, all_granted: granted.length === declared.length,
          permission_meta: item.permissions.permission_meta.map(meta => ({...meta, granted: granted.includes(meta.id)}))};
        return {item: {name: item.name, permissions: item.permissions},
          items: records['/api/plugins'].items, restart_required: false, message: '插件授权已更新，立即生效。'};
      }
      if (fail) throw Error('校验失败'); return {message: '插件配置已保存，重启 bot 后生效。', secrets: {api_key: true}}; };`);
  const route = href => run(`location.hash = ${JSON.stringify(href)}; PAGES[currentRoute().page].render(root, currentRoute().param)`);
  function node(tag, match = () => true) {
    const found = root.walk().find(n => n.tagName === tag && match(n));
    assert.ok(found, `没找到 ${tag}。页面内容：${root.textContent}`);
    return found;
  }
  const link = label => node('A', n => n.textContent === label);
  return {root, run, route, node, link};
}

test('从旧市场入口切到配置，按已安装插件选项切换，再返回市场', async () => {
  const {root, run, route, node, link} = setup();
  await route('#/plugins');
  assert.equal(link('插件市场').getAttribute('aria-current'), 'page');
  for (const label of ['安装', '停用', '卸载']) node('BUTTON', n => n.textContent === label);
  assert.ok(!root.walk().some(n => n.tagName === 'BUTTON' && n.textContent === '配置'));
  await route(link('插件配置').getAttribute('href'));
  assert.equal(link('插件配置').getAttribute('aria-current'), 'page');
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
  await route('#/plugins/config/local-id');
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
  assert.match(root.textContent, /已保存，重启 bot 后生效/);
  assert.equal(key.value, '');
});

test('没有本地插件、读取错误与迟到的响应都不破坏导航', async () => {
  const {root, run, route, link} = setup();
  run(`let resolveConfig; const get = api.get; api.get = path => path.endsWith('/config')
    ? new Promise(resolve => {resolveConfig = resolve}) : get(path)`);
  const first = route('#/plugins/config/local-id');
  await new Promise(resolve => setImmediate(resolve));
  await route(link('简单插件').getAttribute('href'));
  run(`resolveConfig(records['/api/plugins/local-id/config'])`); await first;
  assert.match(root.textContent, /无可配置项/);
  assert.doesNotMatch(root.textContent, /保存配置/);
  run(`api.get = get; delete records['/api/plugins/local-id/config']`);
  await route('#/plugins/config/local-id');
  assert.match(root.textContent, /读取配置失败/);
  link('简单插件');
  run(`records['/api/plugins'].items = []`);
  await route('#/plugins/config');
  assert.match(root.textContent, /还没有已安装的插件/);
  link('插件市场');
});

test('权限授权页逐项对照并即时写回，未授权的一项一眼可见', async () => {
  const {root, run, route, node, link} = setup();
  await route('#/plugins/grants/local-id');
  assert.equal(link('权限授权').getAttribute('aria-current'), 'page');
  assert.equal(link('本地插件').getAttribute('aria-current'), 'page');
  // 声明了两项，只有「读消息」授权；「定时任务」标为未授且带高危标记
  assert.match(root.textContent, /读消息/);
  assert.match(root.textContent, /定时任务/);
  assert.match(root.textContent, /未授权/);
  node('SPAN', n => n.textContent === '高危');
  const status = node('SPAN', n => n.getAttribute('role') === 'status');
  assert.match(status.textContent, /声明了但未授权/);
  const switches = root.walk().filter(n => n.tagName === 'BUTTON' && n.getAttribute('role') === 'switch');
  assert.equal(switches.length, 2);
  assert.equal(switches[0].getAttribute('aria-checked'), 'true');
  assert.equal(switches[1].getAttribute('aria-checked'), 'false');
  // 打开「定时任务」：立即 PUT 整表授权并刷新显示
  await switches[1].fire('click');
  const call = JSON.parse(run('JSON.stringify(calls.at(-1))'));
  assert.deepEqual(call, ['/api/plugins/local_dir/permissions', {permissions: ['read_messages', 'scheduler']}]);
  assert.equal(switches[1].getAttribute('aria-checked'), 'true');
  assert.match(status.textContent, /已授权全部声明的权限/);
  // 再撤掉「读消息」：同样立即写回
  await switches[0].fire('click');
  const revoked = JSON.parse(run('JSON.stringify(calls.at(-1))'));
  assert.deepEqual(revoked, ['/api/plugins/local_dir/permissions', {permissions: ['scheduler']}]);
  assert.match(root.textContent, /声明了但未授权/);
});

test('独立 WebUI 没有运行中的 bot 时授权页只读并说明原因', async () => {
  const {root, run, route} = setup();
  run("records['/api/plugins'].hot_reload = false");
  await route('#/plugins/grants/local-id');
  assert.match(root.textContent, /没有运行中的 bot/);
  assert.equal(root.walk().filter(n => n.tagName === 'BUTTON' && n.getAttribute('role') === 'switch').length, 0);
});

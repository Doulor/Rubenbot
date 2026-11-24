// 运行真实编辑器，检查控件操作与提交的结构化请求，不依赖线上提供商。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

const ref = (provider, model) => ({provider, model});
const key = (provider, model) => JSON.stringify([provider, model]);
function render() {
  const ui = loadUI();
  const llm = {
    providers: [
      {name: 'a', base_url: 'https://a.example/v1', models: ['shared', 'org/vision'], has_api_key: true},
      {name: 'b', base_url: 'https://b.example/v1', models: ['shared', 'b2'], has_api_key: false},
    ],
    role_pools: {chat: [ref('a', 'shared')], vision: [ref('a', 'org/vision')]},
    model_strategy: 'failover', model_strategies: {vision: 'random'}, chat_temperature: 0.7,
    models: {ignored: '旧模型字段不得发回'},
  };
  ui.run(`const draft = {llm: ${JSON.stringify(llm)}, memory: {inject_limit: 2}};`);
  const root = ui.run('modelsEditor(draft)');
  const payload = () => JSON.parse(JSON.stringify(ui.run('settingsPayload(draft, "models")')));
  return {ui, root, payload};
}
function named(node, label) {
  return node.walk().find(el => el.getAttribute('aria-label') === label || (el.tagName === 'BUTTON' && el.textContent === label));
}
function field(node, label) {
  return node.walk().find(el => el.tagName === 'LABEL' && el.childNodes[0].textContent === label).querySelector('input');
}
const providers = root => root.walk().filter(el => el.tagName === 'ARTICLE' && !el.dataset.role);
async function change(node, value, type = 'change') {
  node.value = value; await node.fire(type);
}

test('角色可选跨提供商的同名模型，调整优先级、替换模型、单独设置策略', async () => {
  const {root, payload} = render();
  const roleRef = (role, value, model) => change(named(root, `${role} 新增模型`), key(value, model));
  await roleRef('chat', 'b', 'shared');
  await roleRef('vision', 'b', 'b2');
  await named(root, '上移 chat 第 2 项').fire('click');
  assert.deepEqual(payload().llm.role_pools.chat, [ref('b', 'shared'), ref('a', 'shared')]);
  await named(root, '下移 chat 第 1 项').fire('click');
  await change(named(root, 'vision 第 1 个模型'), key('b', 'shared'));
  await change(named(root, 'chat 调用策略'), 'round_robin');
  await change(named(root, 'vision 调用策略'), '');
  assert.deepEqual(payload().llm.model_strategies, {chat: 'round_robin'});
  assert.deepEqual(payload().llm.role_pools.vision, [ref('b', 'shared'), ref('b', 'b2')]);
  await named(root, '删除 chat 第 1 项').fire('click');
  assert.deepEqual(payload().llm.role_pools.chat, [ref('b', 'shared')]);
  assert.equal(named(root, '删除 chat 第 1 项').getAttribute('disabled'), '');
  assert.equal(payload().llm.chat_temperature, 0.7);
  assert.deepEqual(payload().llm.providers[0], {name: 'a', base_url: 'https://a.example/v1', models: ['shared', 'org/vision']});
});

test('改提供商名与上游模型名同步所有引用，防止删除引用中的记录', async () => {
  const {root, payload} = render();
  const a = providers(root)[0];
  await named(a, '删除此提供商').fire('click');
  await named(a, '删除此上游模型').fire('click');
  assert.equal(payload().llm.providers.length, 2);
  assert.deepEqual(payload().llm.providers[0].models, ['shared', 'org/vision']);
  await change(field(a, '提供商名称'), 'main');
  await change(named(a, '上游模型名'), 'org/new');
  assert.deepEqual(payload().llm.role_pools, {chat: [ref('main', 'org/new')], vision: [ref('main', 'org/vision')]});
  assert.deepEqual(payload().llm.providers[0].models, ['org/new', 'org/vision']);
  await change(named(a, '上游模型名'), 'org/vision');
  await change(field(a, '提供商名称'), 'b');
  assert.deepEqual(payload().llm.providers[0].models, ['org/new', 'org/vision']);
  assert.equal(payload().llm.providers[0].name, 'main');
});

test('提供商、模型及自定义角色增删，密钥只提交用户填写的新值', async () => {
  const {root, payload} = render();
  await named(root, '添加提供商').fire('click');
  const p = providers(root)[2];
  await change(field(p, '提供商名称'), 'third');
  await change(field(p, '接口地址'), 'https://third.example/v1', 'input');
  await change(field(p, 'API Key'), '${NEW_TOKEN}', 'input');
  let add = named(p, '添加模型'); add.value = 'org/m'; await add.fire('keydown', {key: 'Enter'});
  assert.deepEqual(payload().llm.providers[2], {name: 'third', base_url: 'https://third.example/v1', models: ['org/m'], api_key: '${NEW_TOKEN}'});
  add = named(root, '添加自定义角色'); add.value = 'plugin-x'; await add.fire('keydown', {key: 'Enter'});
  await change(named(root, 'plugin-x 新增模型'), key('third', 'org/m'));
  assert.deepEqual(payload().llm.role_pools['plugin-x'], [ref('third', 'org/m')]);
  await named(root, '删除自定义角色 plugin-x').fire('click');
  await named(p, '删除此上游模型').fire('click');
  assert.deepEqual(payload().llm.providers[2].models, []);
  await named(p, '删除此提供商').fire('click');
  assert.equal(payload().llm.providers.length, 2);
  assert.ok(!('plugin-x' in payload().llm.role_pools));
});

test('留空保留密钥、显式清除及撤销清除是不同的提交', async () => {
  const {root, payload} = render();
  const p = providers(root)[0], input = field(p, 'API Key');
  await change(input, 'new-value', 'input');
  await change(input, '', 'input');
  assert.ok(!('api_key' in payload().llm.providers[0]));
  await named(p, '清除密钥').fire('click');
  assert.equal(payload().llm.providers[0].api_key, '');
  assert.equal(input.placeholder, '保存后清除密钥');
  await named(p, '撤销清除').fire('click');
  assert.ok(!('api_key' in payload().llm.providers[0]));
  await named(p, '清除密钥').fire('click');
  await change(input, 'new-value', 'input');
  assert.equal(payload().llm.providers[0].api_key, 'new-value');
});

test('非模型页保存不迁移模型配置，模型页保存不混入旧字段与其它页面草稿', () => {
  const {ui, payload} = render();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.run('settingsPayload(draft, "settings")'))), {memory: {inject_limit: 2}});
  assert.deepEqual(Object.keys(payload()), ['llm']);
  assert.ok(!('models' in payload().llm)); assert.ok(!('api_key' in payload().llm.providers[0]));
  ui.run('draft.llm.role_pools.chat = []');
  assert.throws(payload, /至少要选一个模型/);
});

test('目录预览分段并可搜索，勾选只改草稿；单模型调用失败不改变列表证据', async () => {
  const {ui, root, payload} = render(), card = providers(root)[0];
  ui.run(`const diagnostics = [];
    api.post = async (path, body) => {
      diagnostics.push({path, body});
      if (path.endsWith('/probe')) return {status:'error', error:'http_401', checked_at:'2026-11-24T12:02:00Z'};
      return {status:'available', checked_at:'2026-11-24T12:01:00Z', cached_at:null,
        models:['shared', ...Array.from({length: 220}, (_, i) => 'test-' + i)]};
    }`);
  const rows = () => card.walk().filter(node => node.dataset.model);
  const shared = () => rows().find(node => node.dataset.model === 'shared');
  assert.equal(rows().length, 2);
  assert.match(card.textContent, /还没拉取过/);
  await named(card, '获取模型').fire('click');
  assert.equal(rows().length, 100);
  assert.deepEqual(payload().llm.providers[0].models, ['shared', 'org/vision']);
  assert.doesNotMatch(card.textContent, /null|undefined/);
  assert.match(rows()[1].textContent, /未在上游列表/);
  await named(card, '探测 shared').fire('click');
  assert.match(shared().textContent, /可用（上游列表）/);
  assert.match(shared().textContent, /HTTP 401/);
  assert.equal(shared().walk().filter(node => node.tagName === 'TIME').length, 2);
  const search = named(card, '搜索模型');
  await change(search, 'test-219', 'input');
  assert.equal(rows().length, 1);
  const check = named(card, '勾选 test-219');
  check.checked = true; await check.fire('change');
  assert.ok(payload().llm.providers[0].models.includes('test-219'));
  assert.equal(named(card, '探测 test-219').getAttribute('disabled'), '');
  await named(rows()[0], '加入角色池').fire('click');
  await change(named(card, 'test-219 加入角色'), 'fast');
  assert.deepEqual(payload().llm.role_pools.fast, [ref('a', 'test-219')]);
  assert.deepEqual(JSON.parse(ui.run('JSON.stringify(diagnostics)')), [
    {path:'/api/models/discover',body:{provider:'a',refresh:false}},
    {path:'/api/models/probe',body:{provider:'a',model:'shared'}},
  ]);
});

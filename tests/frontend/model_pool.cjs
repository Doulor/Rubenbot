// 模型池编辑器：每角色一条可增删、可调顺序的模型列表，策略可覆盖。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

function byClass(node, className) {
  return node.walk().filter((el) => el.className.split(' ').includes(className));
}

function render(models = {}, strategies = {}) {
  const ui = loadUI();
  const draft = { llm: { models, model_strategies: strategies } };
  ui.run(`const draft = ${JSON.stringify(draft)};`);
  const node = ui.run('modelsEditor(draft)');
  return { node, ui };
}

function roleItem(node, role) {
  return node.walk().find((el) => el.className.split(' ').includes('model-pool-item') && el.dataset.role === role);
}

function roleInputs(node, role) {
  return byClass(roleItem(node, role), 'model-pool-row').map((row) => row.walk().find((el) => el.tagName === 'INPUT'));
}

function button(node, label) {
  return node.walk().find((el) => el.tagName === 'BUTTON' && el.getAttribute('aria-label') === label);
}

function models(ui) {
  return JSON.parse(JSON.stringify(ui.run('draft.llm.models')));
}

test('旧单字符串配置显示成一个模型，未配置角色给出跟随回退链说明', () => {
  const { node } = render({ fast: 'm-fast' });
  assert.equal(roleInputs(node, 'fast').length, 1);
  assert.equal(roleInputs(node, 'fast')[0].value, 'm-fast');
  assert.equal(roleInputs(node, 'chat').length, 0);
  assert.match(roleItem(node, 'chat').textContent, /跟随回退链/);
});

test('模型列表按顺序展示，可上移、下移、删除', async () => {
  const { node, ui } = render({ chat: ['m-a', 'm-b', 'm-c'] });
  assert.deepEqual(roleInputs(node, 'chat').map((input) => input.value), ['m-a', 'm-b', 'm-c']);

  await button(node, '对话 第 2 个模型上移').fire('click');
  assert.deepEqual(models(ui).chat, ['m-b', 'm-a', 'm-c']);

  const { node: node2, ui: ui2 } = render({ chat: ['m-a', 'm-b', 'm-c'] });
  await button(node2, '对话 第 1 个模型下移').fire('click');
  assert.deepEqual(models(ui2).chat, ['m-b', 'm-a', 'm-c']);

  const { node: node3, ui: ui3 } = render({ chat: ['m-a', 'm-b', 'm-c'] });
  await button(node3, '删除 对话 的第 2 个模型').fire('click');
  assert.deepEqual(models(ui3).chat, ['m-a', 'm-c']);

  // 删到只剩一个时写回字符串（保持旧格式），删空则整项移除
  const { node: node4, ui: ui4 } = render({ chat: ['m-a', 'm-b'] });
  await button(node4, '删除 对话 的第 2 个模型').fire('click');
  assert.equal(models(ui4).chat, 'm-a');
  const { node: node5, ui: ui5 } = render({ chat: 'm-a' });
  await button(node5, '删除 对话 的第 1 个模型').fire('click');
  assert.equal(models(ui5).chat, undefined);
});

test('添加模型：回车加入列表、重复模型不重复添加', async () => {
  const { node, ui } = render({ chat: ['m-a'] });
  const addInput = node.walk().find(
    (el) => el.tagName === 'INPUT' && el.getAttribute('aria-label') === '对话 新增模型'
  );
  addInput.value = 'm-b';
  await addInput.fire('keydown', { key: 'Enter' });
  assert.deepEqual(models(ui).chat, ['m-a', 'm-b']);

  const { node: node2, ui: ui2 } = render({ chat: ['m-a'] });
  const addInput2 = node2.walk().find(
    (el) => el.tagName === 'INPUT' && el.getAttribute('aria-label') === '对话 新增模型'
  );
  addInput2.value = 'm-a';
  await addInput2.fire('keydown', { key: 'Enter' });
  assert.deepEqual(models(ui2).chat, ['m-a']);
});

test('编辑模型名走 input，空名保留给后端校验并报错', async () => {
  const { node, ui } = render({ chat: ['m-a', 'm-b'] });
  const [first] = roleInputs(node, 'chat');
  first.value = '';
  await first.fire('input');
  assert.deepEqual(models(ui).chat, ['', 'm-b']);
});

test('每角色策略可选：跟随全局 / failover / round_robin / random', async () => {
  const { node, ui } = render({ chat: ['m-a', 'm-b'] }, { chat: 'round_robin' });
  const select = roleItem(node, 'chat').walk().find((el) => el.tagName === 'SELECT');
  assert.ok(select);
  const values = select.childNodes.map((option) => option.getAttribute('value'));
  assert.deepEqual(values, ['', 'failover', 'round_robin', 'random']);
  const selected = select.childNodes.find((option) => option.getAttribute('selected') === '');
  assert.equal(selected.getAttribute('value'), 'round_robin');

  select.value = 'random';
  await select.fire('change');
  assert.deepEqual(JSON.parse(JSON.stringify(ui.run('draft.llm.model_strategies'))), { chat: 'random' });

  select.value = '';
  await select.fire('change');
  assert.deepEqual(JSON.parse(JSON.stringify(ui.run('draft.llm.model_strategies'))), {});
});

test('自定义角色名也显示为一行，不会被预置角色覆盖', () => {
  const { node } = render({ custom: ['c1', 'c2'] });
  assert.deepEqual(roleInputs(node, 'custom').map((input) => input.value), ['c1', 'c2']);
});

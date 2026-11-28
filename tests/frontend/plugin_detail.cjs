// 插件详情页：完整信息、schema 驱动表单、保存回读与缺 schema 的兼容。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

function detailPayload(overrides = {}) {
  const schema = [
    { key: 'triggers', type: 'list', default: ['点歌'], description: '触发词', label: '触发词' },
    { key: 'cooldown_seconds', type: 'number', default: 60, label: '冷却（秒）', min: 0, max: 86400 },
    { key: 'music_card', type: 'bool', default: false, label: '先试音乐卡片' },
    { key: 'source', type: 'string', default: 'netease', label: '音源', options: [{ value: 'netease', label: '网易云' }] },
    { key: 'api_key', type: 'string', default: '', label: '密钥', secret: true },
  ];
  const base = {
    id: 'music',
    plugin_name: 'music',
    name: '点歌',
    version: '1.1.0',
    latest_version: '1.1.0',
    description: '搜网易云发歌',
    author: 'RubenBot',
    category: '工具',
    icon: '🎵',
    repository: 'https://example.test/music',
    installed: true,
    enabled: false,
    loaded: false,
    valid: true,
    invalid_reason: null,
    core_too_old: false,
    updatable: false,
    state: 'installed',
    status_text: '已安装 · 未启用',
    can_configure: true,
    hot_config: false,
    local_path: 'plugins/music',
    readme: { name: 'README.md', text: '# 点歌\n\n发「点歌 晴天」。', truncated: false },
    permissions: {
      items: [
        { id: 'read_messages', label: '读消息', description: '读聊天', high_risk: false, declared: true, granted: true },
        { id: 'scheduler', label: '定时任务', description: '后台任务', high_risk: true, declared: true, granted: false },
      ],
      declared: ['read_messages', 'scheduler'],
      granted: ['read_messages'],
      all_granted: false,
    },
    hooks: [{ name: 'on_message', description: '每条消息', registered: 1 }],
    commands: [],
    triggers: ['「点歌 <歌名>」'],
    schema,
    schema_docs: schema.map((spec) => ({
      key: spec.key,
      label: spec.label,
      description: spec.description || '',
      type: spec.type,
      type_label: { list: '字符串列表', number: '数字', bool: '开关', string: '文本' }[spec.type],
      default: spec.default,
      secret: Boolean(spec.secret),
      options: spec.options || [],
      range: spec.key === 'cooldown_seconds' ? '0 – 86400' : '',
    })),
    config: {
      values: { triggers: ['点歌'], cooldown_seconds: 60, music_card: false, source: 'netease', api_key: '' },
      secrets: { api_key: true },
    },
    lifecycle: { hot_config: false, active: false },
  };
  return { ...base, ...overrides };
}

function setup({ detail = detailPayload(), market, saveError, saveResult } = {}) {
  const { root, run } = loadUI();
  const records = {
    '/api/market': market || { items: [
      { id: 'music', name: '点歌', state: 'installed' },
      { id: 'plain', name: '朴素插件', state: 'installed' },
    ] },
    // 统一插件页的左侧菜单就是这份已安装列表
    '/api/plugins': { items: [
      { name: 'music', valid: true, manifest: { id: 'music', name: '点歌' } },
      { name: 'plain', valid: true, manifest: { id: 'plain', name: '朴素插件' } },
    ] },
    '/api/plugins/music/detail': { ok: true, plugin: detail },
    '/api/plugins/plain/detail': { ok: true, plugin: {
      ...detail,
      id: 'plain',
      plugin_name: 'plain',
      name: '朴素插件',
      schema: [],
      schema_docs: [],
      config: { values: {}, secrets: {} },
      can_configure: false,
    } },
  };
  run(`const records = ${JSON.stringify(records)}; const calls = [];
    window.toasts = [];
    api.get = async path => { calls.push(path); if (!records[path]) throw Error(path); return records[path]; };
    api.put = async (path, body) => { calls.push([path, JSON.parse(JSON.stringify(body))]);
      if (${Boolean(saveError)}) throw Object.assign(Error('插件配置校验失败：cooldown_seconds 不能大于 86400'),
        {status: 422, fields: [{field: 'cooldown_seconds', message: '不能大于 86400'}]});
      const saved = ${JSON.stringify(saveResult || null)};
      return saved || {message: '插件配置已保存，重启 bot 后生效。', hot: false,
        values: body.values, secrets: {api_key: true}}; };`);
  const route = (href) => run(`location.hash = ${JSON.stringify(href)}; PAGES[currentRoute().page].render(root, currentRoute().param)`);
  const node = (tag, match = () => true) => {
    const found = root.walk().find((n) => n.tagName === tag && match(n));
    assert.ok(found, `没找到 ${tag}。页面内容：${root.textContent}`);
    return found;
  };
  return { root, run, route, node };
}

test('详情页渲染介绍、版本、作者、权限、触发方式、配置说明与启用状态', async () => {
  const { root, route, node } = setup();
  await route('#/plugins/music/detail');
  for (const fact of ['点歌', 'v1.1.0', 'RubenBot', '已安装 · 未启用', '读消息', '定时任务', '高危', '「点歌 <歌名>」', '冷却（秒）', '触发词']) {
    assert.match(root.textContent, new RegExp(fact.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
      `缺少：${fact}。实际：${root.textContent.slice(0, 800)}`);
  }
  // README 原文原样展示（不解析 HTML），说明与元信息齐全
  assert.ok(root.walk().some((n) => n.tagName === 'PRE' && n.textContent.includes('发「点歌 晴天」')));
  // 未授权的一项可见
  assert.match(root.textContent, /未授权/);
  // 详情菜单列出其它插件，可切换
  const link = node('A', (n) => n.textContent === '朴素插件');
  assert.equal(link.getAttribute('href'), '#/plugins/plain/detail');
  // 配置说明给出类型、范围、默认值
  assert.match(root.textContent, /字符串列表/);
  assert.match(root.textContent, /范围 0 – 86400/);
});

test('schema 驱动表单：控件类型跟随 schema，保存后以回读值回填', async () => {
  const { run, route, node } = setup();
  await route('#/plugins/music/detail');
  const field = (label) => node('INPUT', (n) => n.getAttribute('aria-label') === label);
  // number → number 输入，带 min/max
  const cooldown = field('冷却（秒）');
  assert.equal(cooldown.getAttribute('type'), 'number');
  assert.equal(cooldown.getAttribute('min'), '0');
  assert.equal(cooldown.getAttribute('max'), '86400');
  // boolean → 开关
  const toggle = node('BUTTON', (n) => n.getAttribute('role') === 'switch' && n.getAttribute('aria-label') === '先试音乐卡片');
  assert.equal(toggle.getAttribute('aria-checked'), 'false');
  // options → 下拉；测试宿主不实现 select.value 的选中联动，改看选中项
  const select = node('SELECT', (n) => n.getAttribute('aria-label') === '音源');
  const selected = select.walk().find((n) => n.tagName === 'OPTION' && n.getAttribute('selected') !== null);
  assert.equal(selected.textContent, '网易云');
  // secret → 密码框且不回显
  const secret = field('密钥');
  assert.equal(secret.getAttribute('type'), 'password');
  assert.equal(secret.value, '');
  assert.match(secret.getAttribute('placeholder'), /已设置/);
  // list → chip 编辑器
  field('添加触发词');

  cooldown.value = '30'; await cooldown.fire('input');
  await toggle.fire('click');
  await select.fire('change', { target: { value: 'netease' } });
  await node('FORM').fire('submit');
  const call = JSON.parse(run('JSON.stringify(calls.at(-1))'));
  assert.equal(call[0], '/api/plugins/music/config');
  assert.deepEqual(call[1], { values: {
    triggers: ['点歌'], cooldown_seconds: 30, music_card: true, source: 'netease', api_key: '',
  } });
  assert.match(run('JSON.stringify(window.toasts)'), /\[\]/);  // 保存成功不弹错误
  assert.match(node('DIV', (n) => n.getAttribute('role') === 'status').textContent, /已保存/);
});

test('保存后按服务端回读值回填（归一化、去重、密钥清空）', async () => {
  const savedValues = {
    triggers: ['点歌', '我想听'],
    cooldown_seconds: 45,
    music_card: false,
    source: 'netease',
    api_key: '',
  };
  const { route, node } = setup({
    saveResult: {
      message: '插件配置已保存并热生效，无需重启。',
      hot: true,
      values: savedValues,
      secrets: { api_key: true },
    },
  });
  await route('#/plugins/music/detail');
  const cooldown = node('INPUT', (n) => n.getAttribute('aria-label') === '冷却（秒）');
  cooldown.value = '45.5'; await cooldown.fire('input');
  const secret = node('INPUT', (n) => n.getAttribute('aria-label') === '密钥');
  secret.value = '新密钥'; await secret.fire('input');
  await node('FORM').fire('submit');
  // 回读值覆盖草稿：数字归一、密钥清空并保留「已设置」占位
  assert.equal(cooldown.value, '45');
  assert.equal(secret.value, '');
  assert.match(secret.getAttribute('placeholder'), /已设置/);
  assert.match(node('DIV', (n) => n.getAttribute('role') === 'status').textContent, /已保存并热生效/);
});

test('列表字段保存后按服务端归一结果重建标签组（去重、去空项）', async () => {
  const { root, route, node } = setup({
    saveResult: {
      message: '插件配置已保存，重启 bot 后生效。',
      hot: false,
      values: { triggers: ['点歌'], cooldown_seconds: 60, music_card: false, source: 'netease', api_key: '' },
      secrets: { api_key: true },
    },
  });
  await route('#/plugins/music/detail');
  const add = node('INPUT', (n) => n.getAttribute('aria-label') === '添加触发词');
  add.value = '我想听'; await add.fire('keydown', { key: 'Enter' });
  assert.ok(root.walk().some((n) => n.tagName === 'SPAN' && n.textContent.includes('我想听')));
  await node('FORM').fire('submit');
  // 服务端只保留了「点歌」：标签组按回读值重建，草稿里多出来的项不留在界面上
  const chips = root.walk().filter((n) => n.getAttribute('aria-label')?.startsWith('移除 '));
  assert.deepEqual(chips.map((n) => n.getAttribute('aria-label')), ['移除 点歌']);
});

test('保存失败按字段显示错误、保留草稿', async () => {
  const { root, route, node } = setup({ saveError: true });
  await route('#/plugins/music/detail');
  const cooldown = node('INPUT', (n) => n.getAttribute('aria-label') === '冷却（秒）');
  cooldown.value = '999999'; await cooldown.fire('input');
  await node('FORM').fire('submit');
  assert.equal(cooldown.value, '999999');
  assert.match(node('DIV', (n) => n.getAttribute('role') === 'status').textContent, /校验失败/);
  const alerts = root.walk().filter((n) => n.getAttribute('role') === 'alert');
  assert.ok(alerts.some((n) => /不能大于 86400/.test(n.textContent)), '没有按字段显示错误');
});

test('缺 schema 的插件不崩：显示无可配置项，详情照常', async () => {
  const { root, route } = setup();
  await route('#/plugins/plain/detail');
  assert.match(root.textContent, /朴素插件/);
  assert.match(root.textContent, /没有声明配置项/);
  assert.equal(root.walk().some((n) => n.tagName === 'FORM'), false);
});

test('详情请求失败显示重试；详情不再拉市场索引，菜单就是已安装列表', async () => {
  const { root, run, route, node } = setup({ market: { items: [] } });
  // 详情页不再为菜单拉 /api/market：菜单直接用已安装列表，市场挂了也不影响
  run(`api.get = async path => {
    if (path === '/api/market') throw Error('索引不可用');
    if (path === '/api/plugins') return {items: [{name: 'music', manifest: {id: 'music', name: '点歌'}}]};
    throw Error('详情读取失败');
  }`);
  await route('#/plugins/music/detail');
  assert.match(root.textContent, /读取详情失败/);
  assert.match(root.textContent, /重试/);
  // 左侧菜单是已安装列表里的插件
  assert.equal(node('A', (n) => n.textContent === '点歌').getAttribute('href'), '#/plugins/music/detail');
});

test('仓库链接只放行 http(s)，坏的转义 id 退回第一个插件', async () => {
  const { root, route, node } = setup({
    detail: detailPayload({ repository: 'javascript:alert(1)' }),
  });
  await route('#/plugins/music/detail');
  // 仓库地址不被信任：javascript: 不渲染成链接
  assert.equal(root.walk().some((n) => n.tagName === 'A' && n.textContent === '仓库'), false);
  // 地址栏里手改坏的转义不能让整页崩掉
  await route('#/plugins/%E0%A4%A/detail');
  assert.match(root.textContent, /点歌/);
});

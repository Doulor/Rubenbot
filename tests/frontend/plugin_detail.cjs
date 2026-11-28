// 插件详情页：完整信息与缺 schema 的兼容。详情页里没有配置表单，
// 配置在统一插件页上方的「配置」子 tab，表单行为由 plugins.cjs 覆盖。
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

function setup({ detail = detailPayload(), market } = {}) {
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
    api.get = async path => { calls.push(path); if (!records[path]) throw Error(path); return records[path]; };`);
  const route = (href) => run(`location.hash = ${JSON.stringify(href)}; PAGES[currentRoute().page].render(root, currentRoute().param)`);
  const node = (tag, match = () => true) => {
    const found = root.walk().find((n) => n.tagName === tag && match(n));
    assert.ok(found, `没找到 ${tag}。页面内容：${root.textContent}`);
    return found;
  };
  return { root, run, route, node };
}

test('详情页渲染介绍、版本、作者、权限、触发方式与启用状态', async () => {
  const { root, route, node } = setup();
  await route('#/plugins/music/detail');
  for (const fact of ['点歌', 'v1.1.0', 'RubenBot', '已安装 · 未启用', '读消息', '定时任务', '高危', '「点歌 <歌名>」']) {
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
  // 详情页里没有任何配置内容：没有配置表单，也没有「配置说明」卡片
  assert.equal(root.textContent.includes('配置说明'), false);
  assert.equal(root.walk().some((n) => n.tagName === 'FORM'), false);
});

test('缺 schema 的插件不崩：详情照常渲染，详情页里没有配置表单', async () => {
  const { root, route } = setup();
  await route('#/plugins/plain/detail');
  assert.match(root.textContent, /朴素插件/);
  // 详情页不再放配置表单（配置在上方的「配置」子 tab），所以这里既没有
  // 「没有声明配置项」的提示，也没有任何表单
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

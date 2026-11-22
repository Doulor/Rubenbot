// 检查实际渲染的 DOM 层级，不依赖截图或字符串模板。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

const group = {id: '610', platform: 'snowluma', kind: 'group', name: '周四书友会', avatar_url: 'https://p.qlogo.cn/gh/610/610/640'};
const member = {id: '610', platform: 'snowluma', kind: 'user', name: '小栗', avatar_url: 'https://q1.qlogo.cn/g?b=qq&nk=610&s=640'};
const person = {person_id: 'snowluma:610', platform_uid: '610', platform: 'snowluma', identity: member, memory_count: 1};
const byTag = (node, tag) => node.walk().filter(el => el.tagName === tag);
function badge(node, identity) {
  const name = byTag(node, 'STRONG').find(el => el.className === 'identity-name' && el.textContent === identity.name);
  assert.ok(name, `缺少名字：${identity.name}`);
  const block = name.parentNode.parentNode;
  assert.ok(byTag(block, 'SMALL')[0].textContent.includes(identity.id), 'ID 应在副行');
  assert.equal(byTag(block, 'IMG')[0]?.getAttribute('src'), identity.avatar_url || undefined);
  return block;
}
function fixture() {
  const ui = loadUI();
  for (const [key, value] of Object.entries({group, member, person})) ui.run(`const ${key} = ${JSON.stringify(value)}`);
  return ui;
}

test('群卡片显示名字和群号；图片失败仍显示名字首字，没有资料的群不留空', async () => {
  const { run, root } = fixture();
  run(`api.get = async path => path.startsWith('/api/chats') ? {items:[{chat_id: '610', platform: 'snowluma', chat_type:'group', chat_identity:group},{chat_id:'808',chat_type:'group',platform:''}]} : {}`);
  await run('renderGroups(root)');
  const block = badge(root, group);
  const image = byTag(block, 'IMG')[0];
  assert.equal(image.getAttribute('referrerpolicy'), 'no-referrer');
  await image.fire('error');
  assert.equal(block.childNodes[0].textContent, '周');
  assert.equal(byTag(block,'IMG').length, 0);
  badge(root, {id:'808', name:'未知群'});
});

test('人物列表、档案和群记忆即使同号也不混用头像', async () => {
  const { run, root } = fixture();
  run(`api.get = async path => path.includes('?') ? {items:[person]} : {person,memories:[{scope_type:'group',scope_id:'610',chat_identity:group,content:'喜欢读传记'}]}`);
  await run('renderPeople(root)');
  assert.equal(byTag(root, 'IMG').filter(el => el.getAttribute('src') === member.avatar_url).length, 2);
  badge(root, member);
  badge(root, group);
});

test('画像属于选中人物；时间线展示来源会话，过期近况不冒充现状', async () => {
  const { run, root } = fixture();
  run(`memoryState.tab = 'portrait'; memoryState.personId = person.person_id;
    api.get = async path => {
      if (path.startsWith('/api/people?')) return {items:[person]};
      if (path.startsWith('/api/reminders')) return {items:[]};
      if (path === '/api/people/snowluma%3A610') return {person,profile:{stable_text:'收藏旧书',status_text:'正在搬家',status_expires_at:1},timeline:[{ts:1000,summary:'交换了两本传记',chat_identity:group}]};
      throw Error(path);
    }`);
  await run('renderMemory(root)');
  badge(root, member); badge(root, group);
  assert.match(root.textContent, /收藏旧书/);
  assert.match(root.textContent, /交换了两本传记/);
  assert.doesNotMatch(root.textContent, /正在搬家/);
});

test('决策与日志使用结构化身份，普通日志不把数字识别为头像', () => {
  const { run } = fixture();
  const decision = run(`decisionItem({should_reply:true, hard_trigger:'mentioned', chat_identity:group, sender_identity:member, text:'借书'}, {showChat:true})`);
  badge(decision, group); badge(decision, member);
  const log = run(`logIdentities({chat_identity:group,person_identity:member})`);
  badge(log, group); badge(log, member);
  assert.equal(run(`logIdentities({message:'记录号 610'})`), null);
  const telegram = run(`identityBadge({id:'610',platform:'telegram',name:'晚风'})`);
  assert.equal(telegram.childNodes[0].textContent, '晚');
  assert.equal(byTag(telegram,'IMG').length, 0);
});


test('现役裁定展示原因，不把零占位当成得分；历史评分仍可读', () => {
  const { run } = fixture();
  const render = extra => run(`decisionItem({text:'借书',sender_identity:member,${extra}})`);
  const current = render("reason:'refractory',score:0,threshold:0,factors:{guard:'refractory'}");
  assert.match(current.textContent, /refractory/);
  assert.doesNotMatch(current.textContent, /0.00 \/ 0.00/);
  const old = render("score:0.5,threshold:0.8,factors:{base:0.1}");
  assert.match(old.textContent, /0.50 \/ 0.80/);
});

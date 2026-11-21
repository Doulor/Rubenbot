// 群详情里的群定位并排：手写备注可编辑、自归纳只读、空态与生效来源标清楚。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

const baseProfile = {
  ok: true,
  manual_note: '',
  manual_updated_at: 0,
  auto_summary: '氛围：安静\n常聊话题：工作与晚饭',
  auto_updated_at: 1780000000,
  manual_max_chars: 2000,
  auto_max_chars: 300,
  source: 'auto',
  auto_enabled: true,
};

function byClass(node, className) {
  return node.walk().filter((el) => el.className.split(" ").includes(className));
}

function render(profile) {
  const ui = loadUI();
  ui.run(`const profile = ${JSON.stringify(profile)};`);
  const card = ui.run('groupProfilePanel(profile, {encoded: "610", platformQuery: "?platform=snowluma"})');
  return { card, ui };
}

test('两份并排：左边手写备注可编辑，右边自归纳只读', () => {
  const { card } = render(baseProfile);
  const columns = byClass(card, 'profile-column');
  assert.equal(columns.length, 2);
  const [manual, auto] = columns;
  assert.match(manual.textContent, /手写群备注/);
  assert.match(manual.textContent, /未填写，用自归纳版/);
  assert.equal(byClass(manual, 'textarea').length, 1);
  assert.equal(byClass(manual, 'profile-readonly').length, 0);
  assert.match(manual.textContent, /当前生效：自归纳定位/);
  assert.match(manual.textContent, /注入时按 300 字上限截断/);

  assert.match(auto.textContent, /自归纳群定位/);
  assert.equal(byClass(auto, 'textarea').length, 0);
  assert.match(auto.textContent, /氛围：安静/);
  assert.match(auto.textContent, /最后更新：/);
});

test('手写有内容时标「优先注入」，自归纳仍然只读展示供对照', () => {
  const { card } = render({ ...baseProfile, manual_note: '工作群，别开玩笑' });
  const [manual, auto] = byClass(card, 'profile-column');
  assert.match(manual.textContent, /已填写，优先注入/);
  assert.match(manual.textContent, /当前生效：手写群备注/);
  const textarea = byClass(manual, 'textarea')[0];
  assert.equal(textarea.value, '工作群，别开玩笑');
  assert.match(auto.textContent, /氛围：安静/);
});

test('输入手写备注时生效提示实时切换，保存走 PUT 接口', async () => {
  const { card, ui } = render(baseProfile);
  const [manual] = byClass(card, 'profile-column');
  const textarea = byClass(manual, 'textarea')[0];
  const hint = byClass(manual, 'field-hint').find((node) => /生效/.test(node.textContent));
  assert.match(hint.textContent, /自归纳定位/);

  ui.run('const puts = []; api.put = async (path, body) => { puts.push([path, body]); return {ok:true, manual_note: body.manual_note}; };');
  const card2 = ui.run('groupProfilePanel(profile, {encoded: "610", platformQuery: "?platform=snowluma"})');
  const manual2 = byClass(card2, 'profile-column')[0];
  const textarea2 = byClass(manual2, 'textarea')[0];
  const hint2 = byClass(manual2, 'field-hint').find((node) => /生效/.test(node.textContent));

  textarea2.value = '熟人水群，可以放开聊';
  await textarea2.fire('input');
  assert.match(hint2.textContent, /手写群备注/);

  const save = byClass(manual2, 'profile-actions')[0].walk()
    .find((node) => node.tagName === 'BUTTON' && node.textContent.includes('保存'));
  await save.fire('click');
  // vm 沙箱里的对象与测试宿主不同 realm，用 JSON 比结构
  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.run('puts[puts.length - 1]'))),
    ['/api/chats/610/profile?platform=snowluma', { manual_note: '熟人水群，可以放开聊' }],
  );
  assert.ok(textarea);
});

test('保存后以服务端返回的生效来源为准', async () => {
  const { ui } = render(baseProfile);
  ui.run('const puts = []; api.put = async (path, body) => { puts.push([path, body]); return {ok:true, manual_note: body.manual_note.trim(), manual_updated_at: 1780000123, source: body.manual_note.trim() ? "manual" : "auto"}; };');
  const card = ui.run('groupProfilePanel(profile, {encoded: "610", platformQuery: "?platform=snowluma"})');
  const manual = byClass(card, 'profile-column')[0];
  const textarea = byClass(manual, 'textarea')[0];
  const hint = byClass(manual, 'field-hint').find((node) => /生效/.test(node.textContent));

  textarea.value = '  多留了空格  ';
  const save = byClass(manual, 'profile-actions')[0].walk()
    .find((node) => node.tagName === 'BUTTON' && node.textContent.includes('保存'));
  await save.fire('click');
  assert.equal(textarea.value, '多留了空格');
  assert.match(hint.textContent, /当前生效：手写群备注/);
  const hintTexts = byClass(manual, 'field-hint').map((node) => node.textContent).join(" | ");
  assert.match(hintTexts, /上次保存/);
});

test('两份都空：明确说暂不注入，自归纳区给出未归纳说明', () => {
  const { card } = render({ ...baseProfile, auto_summary: '', source: '' });
  const text = card.textContent;
  assert.match(text, /未填写，用自归纳版/);
  assert.match(text, /两份都空，暂不注入群定位/);
  assert.match(text, /后台任务还没给这个群归纳出定位/);
  assert.match(text, /还没归纳/);
});

test('自归纳关闭时提示只用手写备注', () => {
  const { card } = render({ ...baseProfile, auto_summary: '', auto_enabled: false, source: '' });
  assert.match(card.textContent, /自归纳已在设置里关闭，只用手写备注/);
});

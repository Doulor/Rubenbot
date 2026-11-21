// 表情库磁贴：真表情默认列出，被隔离的普通图片只提示数量，描述/情绪上磁贴。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadUI } = require('./dom.cjs');

function setup(items, photoCount = 0) {
  const { root, run } = loadUI();
  run(`const records = ${JSON.stringify({ items, photo_count: photoCount })};
    api.get = async (path) => { if (path !== '/api/stickers') throw Error(path); return records; };`);
  return { root, run, card: run('stickerLibraryCard()') };
}

/** 等卡片首次加载完成（api.get 是异步的）。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('磁贴先描述、再最多三个情绪芯片、最后标签', async () => {
  const { card } = setup([{
    id: 'st_1',
    kind: 'sticker',
    tags: ['猫猫'],
    description: '一只橘猫笑得前仰后合',
    emotions: ['开心', '大笑', '调皮', '得意'],
    image_url: '/api/stickers/st_1/image',
  }]);
  await settle();
  const text = card.textContent;
  assert.match(text, /一只橘猫笑得前仰后合/);
  const emotions = ['开心', '大笑', '调皮', '得意'].filter((name) => text.includes(name));
  assert.deepEqual(emotions, ['开心', '大笑', '调皮']);
  assert.match(text, /猫猫/);
  assert.ok(text.indexOf('一只橘猫') < text.indexOf('猫猫'));
  const desc = card.walk().find((node) => node.className === 'sticker-desc');
  assert.ok(desc && desc.getAttribute('title') === '一只橘猫笑得前仰后合');
});

test('没有描述与情绪时磁贴只显示标签', async () => {
  const { card } = setup([{ id: 'st_2', kind: 'sticker', tags: ['狗狗'], description: '', emotions: [], image_url: null }]);
  await settle();
  assert.match(card.textContent, /狗狗/);
  assert.ok(!card.walk().some((node) => node.className === 'sticker-desc'));
  assert.ok(!card.walk().some((node) => node.className === 'sticker-emotions'));
});

test('隔离的普通图片只显示计数小字，不出磁贴', async () => {
  const { card } = setup([
    { id: 'st_3', kind: 'sticker', tags: ['猫'], description: '', emotions: [], image_url: null },
  ], 71);
  await settle();
  assert.match(card.textContent, /另有 71 条普通图片已隔离/);
  assert.equal(card.walk().filter((node) => node.className === 'sticker-tile').length, 1);
});

test('全部隔离且没有真表情时只剩计数小字', async () => {
  const { card } = setup([], 5);
  await settle();
  assert.match(card.textContent, /另有 5 条普通图片已隔离/);
  assert.doesNotMatch(card.textContent, /表情库还是空的/);
});

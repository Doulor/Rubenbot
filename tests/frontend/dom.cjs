// 原生前端的轻量测试宿主：运行真实 app.js，只略过应用启动和后台轮询。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class TestNode {
  constructor(tagName, text = '') {
    this.tagName = tagName.toUpperCase();
    this.text = text;
    this.childNodes = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = '';
    this.dataset = {};
    this.style = {};
    this.value = '';
    this.hidden = false;
  }
  append(...children) {
    for (const child of children) {
      const node = child instanceof TestNode ? child : new TestNode('#text', String(child));
      this.childNodes.push(node);
      node.parentNode = this;
    }
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(node => node !== this);
    this.parentNode = null;
  }
  replaceChildren(...children) {
    this.childNodes = [];
    this.text = '';
    this.append(...children);
  }
  get textContent() { return this.text + this.childNodes.map(node => node.textContent).join(''); }
  set textContent(text) { this.replaceChildren(); this.text = String(text); }
  setAttribute(key, value) {
    this.attributes.set(key, String(value));
    if (key === 'value') this.value = String(value);
    if (key === 'class') this.className = String(value);
  }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  matches(selector) {
    if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith('#')) return this.getAttribute('id') === selector.slice(1);
    return this.tagName === selector.toUpperCase();
  }
  querySelector(selector) { return this.walk().slice(1).find(node => node.matches(selector)) ?? null; }
  closest(selectors) {
    for (let node = this; node; node = node.parentNode) {
      if (selectors.split(',').some(selector => node.matches(selector.trim()))) return node;
    }
    return null;
  }
  walk() { return [this, ...this.childNodes.flatMap(node => node.walk())]; }
  addEventListener(key, fn) {
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(fn);
  }
  async fire(type, extra = {}) {
    for (const fn of this.listeners.get(type) || []) {
      await fn({target: this, currentTarget: this, preventDefault() {}, ...extra});
    }
  }
}

function loadUI() {
  const root = new TestNode('main');
  const document = {
    createElement: tag => new TestNode(tag),
    createElementNS: (_, tag) => new TestNode(tag),
    createTextNode: text => new TestNode('#text', text),
    querySelector: selector => root.walk().find(node => '#' + node.getAttribute('id') === selector) ?? null,
  };
  const context = vm.createContext({root, document, Node: TestNode, location: {hash: ''},
    window: {}, console, URL, setTimeout, clearTimeout, performance,
    matchMedia: () => ({matches: false, addEventListener() {}})});
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/motion.js'), 'utf8'));
  const file = path.join(__dirname, '../../rubenbot/web/static/app.js');
  run(fs.readFileSync(file, 'utf8').replace(/\bmain\(\);\s*$/, ''));
  // 详情页在独立文件里，浏览器由 index.html 紧接着加载；测试宿主同样加载
  run(fs.readFileSync(path.join(__dirname, '../../rubenbot/web/static/plugin_detail.js'), 'utf8'));
  return {root, document, run};
}
module.exports = {loadUI};

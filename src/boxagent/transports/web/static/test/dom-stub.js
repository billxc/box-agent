// 用 `node --test` 测试原生 Web Components 的极简 DOM stub。
//
// 刻意不用 jsdom — 无 npm / 无工具链。只实现组件实际用到的表面
//（createElement、classList、append、按 tag+[data-attr] 的 querySelector、
// dataset↔attribute、自定义元素升级 + connectedCallback）。
// 够这些组件用；不是通用 DOM。

"use strict";

function camelToKebab(name) {
  return name.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
}

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(...cs) { for (const c of cs) if (c) this.set.add(c); this._sync(); }
  remove(...cs) { for (const c of cs) this.set.delete(c); this._sync(); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : force;
    if (on) this.set.add(c); else this.set.delete(c);
    this._sync();
  }
  contains(c) { return this.set.has(c); }
  _sync() { this.el._className = [...this.set].join(" "); }
}

class El {
  constructor(tagName) {
    const tag = tagName || ""; // 自定义元素子类调 super() 时不传 tag
    this.tagName = tag.toUpperCase();
    this.localName = tag.toLowerCase();
    this.children = [];
    this.parentNode = null;
    this.isConnected = false;
    this.attributes = {};
    this.style = {};
    this.title = "";
    this.dateTime = "";
    this._className = "";
    this._textContent = "";
    this._innerHTML = "";
    this._listeners = {};
    this.classList = new ClassList(this);
    this.dataset = new Proxy({}, {
      set: (t, key, value) => { this.attributes["data-" + camelToKebab(key)] = String(value); t[key] = value; return true; },
      get: (t, key) => t[key],
    });
  }
  // 赋值 innerHTML 会替换所有子节点（真 DOM 行为）。我们无法把 HTML 串
  // 解析成节点，所以清空 children、把原串当不透明值保留 —— 需要可查询结构的
  // 组件改用 createElement/append 构建。
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v); this.children = []; }
  get className() { return this._className; }
  set className(v) { this._className = v; this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v); this.children = []; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  hasAttribute(k) { return k in this.attributes; }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    if (this.isConnected) connectTree(child);
    return child;
  }
  append(...kids) { for (const k of kids) this.appendChild(k); }
  remove() {
    if (this.parentNode) {
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    }
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(event) {
    for (const fn of this._listeners[event.type] || []) fn.call(this, event);
    return true;
  }
  querySelector(selector) {
    const match = parseSelector(selector);
    return findDescendant(this, match);
  }
  // 遍历所有后代（供测试断言用）。
  _all() {
    const out = [];
    for (const c of this.children) { out.push(c); out.push(...c._all()); }
    return out;
  }
}

function connectTree(el) {
  el.isConnected = true;
  if (typeof el.connectedCallback === "function") el.connectedCallback();
  for (const c of el.children) connectTree(c);
}

function parseSelector(selector) {
  // 支持 `tag`、`tag[attr="val"]`、`[attr="val"]`、`.class`。
  const sel = selector.trim();
  if (sel.startsWith(".")) return { cls: sel.slice(1) };
  const m = sel.match(/^([a-z-]+)?(?:\[([a-z-]+)="([^"]*)"\])?$/i);
  if (!m) throw new Error("dom-stub: unsupported selector " + selector);
  return { tag: m[1] || null, attr: m[2] || null, val: m[3] };
}

function findDescendant(root, match) {
  for (const c of root.children) {
    const tagOk = !match.tag || c.localName === match.tag.toLowerCase();
    const attrOk = !match.attr || c.getAttribute(match.attr) === match.val;
    const clsOk = !match.cls || c.classList.contains(match.cls);
    if (tagOk && attrOk && clsOk) return c;
    const deeper = findDescendant(c, match);
    if (deeper) return deeper;
  }
  return null;
}

function install() {
  const registry = new Map();
  const customElements = {
    define: (name, cls) => { registry.set(name, cls); },
    get: (name) => registry.get(name),
  };
  const document = {
    createElement: (tag) => {
      const Cls = registry.get(tag.toLowerCase());
      const el = Cls ? new Cls() : new El(tag);
      // 自定义元素子类没给 super() 传 tag；这里补上，
      // 让 localName/tagName + querySelector 匹配可用。
      el.tagName = tag.toUpperCase();
      el.localName = tag.toLowerCase();
      return el;
    },
  };
  globalThis.window = globalThis;
  globalThis.HTMLElement = El;
  globalThis.document = document;
  globalThis.customElements = customElements;
  globalThis.CSS = { escape: (s) => String(s).replace(/["\\\]]/g, "\\$&") };
  // 同步执行 rAF 回调 — 测试不绘制，只需 scroll-to-bottom 逻辑跑起来。
  globalThis.requestAnimationFrame = (fn) => { fn(); return 0; };
  // 惰性 EventSource，让 openStream() 能在测试里运行而无需真连接
  //（永不投递事件；message handler 也就不会被触发）。
  globalThis.EventSource = class EventSource {
    constructor(url) { this.url = url; this.onopen = null; this.onerror = null; this.onmessage = null; }
    close() { this.closed = true; }
  };
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
  };
  // 最小 location，让构建 URL 的代码（multiplex ws）能在 Node 下运行。
  globalThis.location = { href: "https://host.example/ui/", protocol: "https:" };
  return { El, document, customElements, connectTree, makeRoot };
}

// 一个已连接的容器 — 往里 append 子节点会触发 connectedCallback。
function makeRoot() {
  const root = new El("div");
  root.isConnected = true;
  return root;
}

module.exports = { install, makeRoot, El };

// Minimal DOM stub for testing the vanilla Web Components under `node --test`.
//
// Deliberately NOT jsdom — no npm / no toolchain. Implements only the surface
// the components actually touch (createElement, classList, append, querySelector
// by tag+[data-attr], dataset↔attribute, custom-element upgrade + connectedCallback).
// Faithful enough for these components; not a general DOM.

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
    const tag = tagName || ""; // custom-element subclasses call super() with no tag
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
    this.innerHTML = "";
    this.classList = new ClassList(this);
    this.dataset = new Proxy({}, {
      set: (t, key, value) => { this.attributes["data-" + camelToKebab(key)] = String(value); t[key] = value; return true; },
      get: (t, key) => t[key],
    });
  }
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
  querySelector(selector) {
    const match = parseSelector(selector);
    return findDescendant(this, match);
  }
  // Walk all descendants (for assertions in tests).
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
  // Supports `tag`, `tag[attr="val"]`, `[attr="val"]`, `.class`.
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
      // Custom-element subclasses didn't pass a tag to super(); set it here so
      // localName/tagName + querySelector matching work.
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
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
  };
  return { El, document, customElements, connectTree, makeRoot };
}

// A connected container — children appended to it fire connectedCallback.
function makeRoot() {
  const root = new El("div");
  root.isConnected = true;
  return root;
}

module.exports = { install, makeRoot, El };

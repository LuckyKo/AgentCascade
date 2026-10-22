'use strict';
/**
 * Regression tests for BUG_0134 — Back navigation restored Chrome's session-history
 * form snapshot over the values loadSettings() had just read from localStorage.
 *
 * The fix has three parts, each pinned here:
 *   1. every settings control inside #side-panel opts out of session-history form
 *      state (`autocomplete="off"`), including dynamically rendered panels;
 *   2. a `pageshow` handler re-applies the store synchronously on bfcache restores
 *      and `back_forward` navigations (Chrome restores the snapshot BEFORE pageshow,
 *      so a timer-based re-apply would leave a stale frame);
 *   3. `pagehide` flushes the settings like `beforeunload` (which does not fire when
 *      a page enters the bfcache).
 *
 * Run:  node --test tests/test_settings_history_restore.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '..');
const APP_JS = fs.readFileSync(path.join(ROOT, 'web_ui', 'app.js'), 'utf8');
const INDEX_HTML = fs.readFileSync(path.join(ROOT, 'web_ui', 'index.html'), 'utf8');

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(', 'g');
  const m = re.exec(APP_JS);
  assert.ok(m, `Could not find "function ${name}(" in app.js`);
  let i = APP_JS.indexOf('{', m.index);
  assert.ok(i >= 0, `No body for ${name}`);
  let depth = 0;
  for (let j = i; j < APP_JS.length; j++) {
    const c = APP_JS[j];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return APP_JS.slice(m.index, j + 1); }
  }
  throw new Error(`Unbalanced braces for ${name}`);
}

// Extract the arrow function passed to the pageshow listener.
function extractPageshowHandler() {
  const marker = APP_JS.indexOf("addEventListener('pageshow'");
  assert.ok(marker >= 0, 'pageshow listener not found in app.js');
  const exprStart = APP_JS.indexOf('(event)', marker);
  assert.ok(exprStart >= 0, 'pageshow handler parameter not found');
  const brace = APP_JS.indexOf('{', exprStart);
  let depth = 0;
  for (let j = brace; j < APP_JS.length; j++) {
    const c = APP_JS[j];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return APP_JS.slice(exprStart, j + 1); }
  }
  throw new Error('Unbalanced braces for pageshow handler');
}

// ── Test 1: every settings control opts out of session-history form state ─────
test('audit: every settings control opts out of session-history form state', () => {
  const controls = [...INDEX_HTML.matchAll(/<(input|select|textarea)\b[^>]*>/gi)].map((m) => m[0]);
  assert.ok(controls.length >= 110, `expected the full settings surface, found only ${controls.length} controls`);

  // Transient text (chat draft / session filter) intentionally keeps Chrome's restore;
  // file inputs carry no value to restore.
  const EXEMPT_IDS = new Set(['chatInput', 'sessionSearch']);
  const idOf = (tag) => (tag.match(/\bid\s*=\s*["']([^"']+)["']/i) || [])[1];
  const audited = controls.filter((tag) =>
    !/\btype\s*=\s*["']file["']/i.test(tag) && !EXEMPT_IDS.has(idOf(tag)));

  const missing = audited.filter((tag) => !/\bautocomplete\s*=\s*["']off["']/i.test(tag));
  assert.deepEqual(missing, [], 'settings controls without autocomplete="off":\n' + missing.join('\n'));

  // Guard the exemptions themselves: the chat input must exist, otherwise the filter
  // above could silently skip everything on a markup rewrite.
  assert.ok(controls.some((tag) => idOf(tag) === 'chatInput'), 'chatInput not found');
});

// ── Test 2: the dynamic-panel sweep exists and is wired to #side-panel ───────
test('source: startup sweep + MutationObserver opt dynamically rendered controls out', () => {
  assert.match(APP_JS, /function disableSessionHistoryFormRestore\(root\)/);
  assert.match(APP_JS, /root\.querySelectorAll\('input, select, textarea'\)/);
  assert.match(APP_JS, /disableSessionHistoryFormRestore\(document\)/);
  assert.match(APP_JS, /new MutationObserver\(/);
  assert.match(APP_JS, /\.observe\(sidePanel,\s*\{\s*childList:\s*true,\s*subtree:\s*true\s*\}\)/);
});

// ── Test 3: the helper really tags controls (real extracted function) ────────
test('behaviour: disableSessionHistoryFormRestore tags settings but keeps transient text/file inputs', () => {
  const ctx = vm.createContext({});
  vm.runInContext(extractFunction('disableSessionHistoryFormRestore'), ctx, { filename: 'helper.js' });

  const makeEl = (attrs = {}, props = {}) => ({
    ...props,
    attrs: { ...attrs },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    setAttribute(k, v) { this.attrs[k] = v; },
  });
  const plain = makeEl();
  const preset = makeEl({ autocomplete: 'on' });
  const chat = makeEl({}, { id: 'chatInput' });
  const file = makeEl({}, { type: 'file' });
  const root = { querySelectorAll: () => [plain, preset, chat, file] };

  vm.runInContext('disableSessionHistoryFormRestore', ctx)(root);
  assert.equal(plain.attrs.autocomplete, 'off');
  assert.equal(preset.attrs.autocomplete, 'on', 'an explicit autocomplete value must not be overwritten');
  assert.equal(chat.attrs.autocomplete, undefined, 'chat draft must keep Chrome restore');
  assert.equal(file.attrs.autocomplete, undefined, 'file inputs have no state to restore');
});

// ── Test 4: pageshow re-applies the store for history restores only ──────────
function buildPageshowSandbox({ navType = 'navigate', persisted = false, connected = true, readyState = 1 } = {}) {
  const calls = { load: 0, send: [] };
  const box = { value: 'STALE' }; // models the DOM value loadSettings() restores
  const ctx = {
    settingsLoaded: true,
    state: { connected },
    ws: { readyState },
    WebSocket: { OPEN: 1 },
    performance: { getEntriesByType: () => [{ type: navType }] },
    calls,
    box,
    loadSettings() { calls.load++; box.value = 'SAVED'; },
    send(msg) { calls.send.push(msg); },
    getGenerateCfg() { return { model: 'm' }; },
  };
  vm.createContext(ctx);
  vm.runInContext(extractFunction('_isBackForwardNavigation'), ctx, { filename: 'isBackForwardNavigation.js' });
  const handler = vm.runInContext('(' + extractPageshowHandler() + ')', ctx, { filename: 'pageshow.js' });
  return { ctx, handler, calls, box, persisted };
}

test('behaviour: bfcache restore re-applies synchronously and re-pushes config', () => {
  const { handler, calls, box } = buildPageshowSandbox({ persisted: true, connected: true });
  handler({ persisted: true });
  // Synchronous (no timer): the stale snapshot must be gone as soon as the handler returns.
  assert.equal(box.value, 'SAVED');
  assert.equal(calls.load, 1);
  assert.equal(JSON.stringify(calls.send),
    JSON.stringify([{ type: 'update_config', generate_cfg: { model: 'm' } }]));
});

test('behaviour: back_forward reload re-applies the store', () => {
  const { handler, calls, box } = buildPageshowSandbox({ navType: 'back_forward', persisted: false });
  handler({ persisted: false });
  assert.equal(box.value, 'SAVED');
  assert.equal(calls.load, 1);
});

test('behaviour: a normal first load is not re-applied', () => {
  const { handler, calls, box } = buildPageshowSandbox({ navType: 'navigate', persisted: false });
  handler({ persisted: false });
  assert.equal(box.value, 'STALE');
  assert.equal(calls.load, 0);
  assert.equal(calls.send.length, 0);
});

test('behaviour: no server push when the socket is not open', () => {
  const { handler, calls, box } = buildPageshowSandbox({ persisted: true, connected: false, readyState: 3 });
  handler({ persisted: true });
  assert.equal(box.value, 'SAVED');
  assert.equal(calls.load, 1);
  assert.equal(calls.send.length, 0);
});

// ── Test 5: pagehide flushes like beforeunload ───────────────────────────────
test('source: beforeunload and pagehide share the settings flush', () => {
  assert.match(APP_JS, /function flushSettingsForPageExit\(\)/);
  assert.match(APP_JS, /addEventListener\('beforeunload',\s*flushSettingsForPageExit\)/);
  assert.match(APP_JS, /addEventListener\('pagehide',\s*flushSettingsForPageExit\)/);
});

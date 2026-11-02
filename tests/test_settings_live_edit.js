'use strict';
/**
 * Regression test for the "settings edited in UI revert mid-stream" bug (todo.md line 139).
 *
 * Root cause: POOL_SETTINGS_MAP used hyphenated `localKey`s for the tool char-limit family
 * ('grep-char-limit', ...), but saveSettings()/getGenerateCfg() persist those settings to
 * localStorage under UNDERSCORE keys ('grep_char_limit', ...). So the respect-local-preference
 * guard in syncPoolSettings() (`if (localKey && saved[localKey] !== undefined) continue;`) never
 * engaged for that family, and every ~150ms stream_update tick stomped the user's edit with the
 * stale server value (then saveSettings(false) persisted + re-broadcast it).
 *
 * This test extracts the REAL POOL_SETTINGS_MAP / syncPoolSettings / getGenerateCfg from
 * web_ui/app.js (so it always tests current source, not a copy) and runs them in a sandbox with
 * a minimal DOM stub. It does NOT load the whole app.js (which is browser-global and would throw).
 *
 * Run:  node tests/test_settings_live_edit.js   (exits 0 on pass / 1 on fail)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

// ── Extract the real source fragments from app.js ─────────────────────────────
const APP_JS = fs.readFileSync(
  path.join(__dirname, '..', 'web_ui', 'app.js'),
  'utf8'
);

function extractConstArray(name) {
  // Match `const NAME = [ ... ];` up to the first top-level `];`
  const re = new RegExp('const\\s+' + name + '\\s*=\\s*\\[', 'g');
  const m = re.exec(APP_JS);
  assert.ok(m, `Could not find "const ${name} = [" in app.js`);
  let i = m.index + m[0].length;
  while (i < APP_JS.length && !APP_JS.startsWith('];', i)) i++;
  assert.ok(i < APP_JS.length, `Unterminated array for ${name}`);
  return APP_JS.slice(m.index, i + 2); // include the closing ];
}

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

const MAP_SRC = extractConstArray('POOL_SETTINGS_MAP');
const SYNC_SRC = extractFunction('syncPoolSettings');
const GETCFG_SRC = extractFunction('getGenerateCfg');

// ── Minimal DOM / browser stubs ───────────────────────────────────────────────
function makeDom(ids) {
  const elements = {};
  for (const id of ids) elements[id] = { value: '', checked: false };
  // app.js defines `const $ = (sel) => document.querySelector(sel)` where sel is an id like '#foo'.
  const querySelector = (sel) => {
    if (typeof sel !== 'string') return null;
    const key = sel.startsWith('#') ? sel : '#' + sel;
    return elements[key] || null;
  };
  const documentStub = {
    activeElement: null,
    getElementById(id) { return elements[id] || null; },
    querySelector,
  };
  return { elements, documentStub, $: querySelector };
}

function buildSandbox({ dom, saveSettingsSpy }) {
  const localStorageData = {};
  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(localStorageData, k) ? localStorageData[k] : null; },
    setItem(k, v) { localStorageData[k] = String(v); },
    removeItem(k) { delete localStorageData[k]; },
  };
  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
    localStorage,
    state: { agents: [], connected: false },
    agentDisabledTools: {},
    send() {},
    updateTwoPhaseInputsEnabled() {},
    // Module-level element globals referenced by getGenerateCfg/saveSettings. They are all
    // guarded by truthiness in the source, so null is a safe no-op stub here.
    approvalTimeoutSeconds: null,
    approvalTimeoutEnabled: null,
    settingAsyncShellConsoleWindow: null,
    settingImageCaptionMode: null,
    workAccessFoldersRO: null,
    workAccessFoldersRW: null,
    defaultWorkspace: null,
    validateRetrySettings() {}, // called at the end of getGenerateCfg; no-op stub
  };
  sandbox.saveSettings = saveSettingsSpy || (() => {});
  const ctx = vm.createContext(sandbox);
  // Execute the real fragments in the sandbox.
  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
  vm.runInContext(GETCFG_SRC, ctx, { filename: 'getGenerateCfg.js' });
  return { sandbox, ctx, localStorageData };
}

// The char-limit family that was the subject of the bug.
const CHAR_LIMIT_FAMILY = [
  { id: '#setting-grep-char-limit', key: 'grep_char_limit' },
  { id: '#setting-grep-spillover', key: 'grep_spillover' },
  { id: '#setting-shell-char-limit', key: 'shell_char_limit' },
  { id: '#setting-code-char-limit', key: 'code_char_limit' },
  { id: '#setting-list-dir-char-limit', key: 'list_dir_char_limit' },
];

// ── Test 1: N stream_update ticks while the user edits #setting-grep-char-limit ─
test('server value wins over a stale local preference (Batch B: server is authoritative)', () => {
  const ids = CHAR_LIMIT_FAMILY.map((e) => e.id);
  const dom = makeDom(ids);

  // Server's OLD value for the whole family.
  const serverOld = {
    grep_char_limit: 100,
    grep_spillover: false,
    shell_char_limit: 200,
    code_char_limit: 300,
    list_dir_char_limit: 400,
  };

  // localStorage already holds the user's previously-saved value under the UNDERSCORE key.
  const initialSaved = { grep_char_limit: 150 };
  let saveCalls = 0;
  const { sandbox, ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => { saveCalls++; } });
  localStorageData['agent-cascade-settings'] = JSON.stringify(initialSaved);

  // Seed the DOM with the user's current value (as if they just typed/committed it).
  dom.elements['#setting-grep-char-limit'].value = '150';

  // Simulate N stream_update ticks carrying the server's OLD pool_settings.
  const N = 20;
  for (let i = 0; i < N; i++) {
    vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: serverOld }));
  }

  // The control must adopt the SERVER value — the stale local preference no longer wins.
  assert.equal(
    String(dom.elements['#setting-grep-char-limit'].value), '100',
    `grep char limit did not adopt the server value (got '${dom.elements['#setting-grep-char-limit'].value}')`
  );

  // saveSettings is spied in this test (it does not write localStorage), so the stored value
  // is unchanged — the meaningful check is that the DOM control adopted the server value above.
  const persisted = JSON.parse(localStorageData['agent-cascade-settings']);
  assert.equal(persisted.grep_char_limit, 150, 'spied saveSettings leaves localStorage untouched');

  // Churn check: once the DOM has converged with the server values, subsequent ticks must be
  // pure no-ops (no further saveSettings calls). We let it converge over a few ticks, then
  // confirm the remaining ticks produce zero write-backs. (The first tick legitimately syncs
  // the *other* char-limit fields that have no saved local pref and aren't focused.)
  const before = saveCalls;
  for (let i = 0; i < 10; i++) {
    vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: serverOld }));
  }
  assert.equal(saveCalls, before,
    `saveSettings(false) fired ${saveCalls - before}x on converged (unchanged) ticks — churn bug`);
});

// ── Test 2: focus suppression protects an actively-edited field even w/o a saved local pref ─
test('syncPoolSettings skips the focused element (defense-in-depth against stale-tick stomps)', () => {
  const ids = CHAR_LIMIT_FAMILY.map((e) => e.id);
  const dom = makeDom(ids);
  let saveCalls = 0;
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => { saveCalls++; } });

  // No saved local preference for this key — only the focus guard can protect it.
  // User is actively editing (element is focused) and has typed a new value.
  dom.elements['#setting-grep-char-limit'].value = '999';
  dom.documentStub.activeElement = dom.elements['#setting-grep-char-limit'];

  const serverOld = { grep_char_limit: 100 };
  for (let i = 0; i < 5; i++) {
    vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: serverOld }));
  }

  assert.equal(dom.elements['#setting-grep-char-limit'].value, '999',
    'focused element was stomped by a stale tick');
  assert.equal(saveCalls, 0, 'saveSettings fired while the user is actively editing');
});

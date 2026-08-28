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
test('user edit of grep char limit survives N stale stream_update ticks (no stomp-revert)', () => {
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

  // The control must retain the user value — NOT be reverted to the stale server value.
  assert.equal(
    dom.elements['#setting-grep-char-limit'].value, '150',
    `grep char limit was stomped back to server value (got '${dom.elements['#setting-grep-char-limit'].value}')`
  );

  // localStorage must still hold the user's grep value (no stomped persistence).
  const persisted = JSON.parse(localStorageData['agent-cascade-settings']);
  assert.equal(persisted.grep_char_limit, 150, 'localStorage reverted to stale server value');

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

// ── Test 3 (invariant): every POOL_SETTINGS_MAP.localKey equals the exact key saveSettings() writes ─
test('invariant: POOL_SETTINGS_MAP.localKey matches the localStorage key saveSettings() writes', () => {
  // Derive, for each mapped control, the storage key that getGenerateCfg() produces.
  // getGenerateCfg reads $('#<id>').value/checked and writes cfg[key]. We run the REAL
  // getGenerateCfg with a DOM stub whose elements exist, then confirm the resulting cfg
  // object contains an entry for the map's `key` — and that localKey (the localStorage key)
  // is consistent: it must equal either the generate-cfg key or a key saveSettings() writes.
  const ids = [];
  const entries = vm.runInContext('POOL_SETTINGS_MAP', buildCtxOnly());
  for (const e of entries) { if (e.id && !ids.includes(e.id)) ids.push(e.id); }

  const dom = makeDom(ids);
  // Give every element a distinct non-empty value so getGenerateCfg picks them up.
  ids.forEach((id, n) => {
    const el = dom.elements[id];
    if (el) { el.value = String(n + 1); el.checked = true; }
  });

  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  const cfg = vm.runInContext('getGenerateCfg()', ctx);

  // The invariant that would have caught this bug class: for each map entry whose storage is
  // delegated to getGenerateCfg() (i.e. the char-limit family + other generate-cfg keys), the
  // localKey must be a key that actually appears in the saved settings object. We reconstruct
  // the saved object the way saveSettings() does: it starts from getGenerateCfg() output and
  // adds explicit `s['<localKey>'] = ...` assignments for the rest. The simplest robust check:
  //   localKey === key  (underscore form, written by getGenerateCfg)  OR
  //   saveSettings explicitly writes s[localKey] (hyphenated UI-key form).
  // We verify the former is satisfied for the char-limit family specifically, and that NO entry
  // has a hyphenated localKey while its `key` is an underscore generate-cfg key with no explicit
  // saveSettings write — the exact mismatch that caused the bug.

  const explicitSaveKeys = extractExplicitSaveKeys(); // keys written via s['...'] in saveSettings()

  // (a) HARD invariant for the char-limit family that caused this bug: localKey MUST equal the
  //     underscore storage key that getGenerateCfg()/saveSettings() writes. This is what makes
  //     the respect-local-preference guard in syncPoolSettings() actually engage.
  const familyKeys = new Set(CHAR_LIMIT_FAMILY.map((e) => e.key));
  for (const { key } of CHAR_LIMIT_FAMILY) {
    const entry = entries.find((e) => e.key === key);
    assert.ok(entry, `map entry for ${key} missing`);
    assert.equal(entry.localKey, key,
      `${key}: localKey '${entry.localKey}' must equal the underscore storage key '${key}'`);
  }

  // (b) SOFT audit across the whole map: report (but do not fail on) any OTHER entry whose
  //     localKey is not written by saveSettings() — these are latent instances of the same bug
  //     class, out of scope for this fix but worth surfacing. A localKey is "safe" if
  //     saveSettings() writes it explicitly OR via getGenerateCfg (localKey === key).
  const latent = [];
  for (const e of entries) {
    if (!e.localKey || !e.key) continue;
    if (familyKeys.has(e.key)) continue; // covered by the hard assertion above
    const inCfg = Object.prototype.hasOwnProperty.call(cfg, e.key);
    const inExplicit = explicitSaveKeys.has(e.localKey);
    const safe = inExplicit || (inCfg && e.localKey === e.key);
    if (!safe) {
      latent.push(`id=${e.id} key=${e.key} localKey=${e.localKey}`);
    }
  }
  if (latent.length) {
    console.warn(
      `\n[audit] ${latent.length} POOL_SETTINGS_MAP entr${latent.length === 1 ? 'y has' : 'ies have'} ` +
      `a localKey that saveSettings() does not write (latent stomp-guard gaps, out of scope):\n  - ` +
      latent.join('\n  - ')
    );
  }
});

// Build a sandbox just to read POOL_SETTINGS_MAP.
function buildCtxOnly() {
  const dom = makeDom([]);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  return ctx;
}

// Parse saveSettings() for explicit `s['<key>'] =` assignments (the hyphenated UI-key writes).
function extractExplicitSaveKeys() {
  const fnSrc = extractFunction('saveSettings');
  const keys = new Set();
  const re = /s\[\s*(['"])([^'"]+)\1\s*\]\s*=/g;
  let m;
  while ((m = re.exec(fnSrc)) !== null) keys.add(m[2]);
  return keys;
}

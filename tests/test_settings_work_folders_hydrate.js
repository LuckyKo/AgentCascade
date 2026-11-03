'use strict';
/**
 * Regression: extra work folders set in the settings UI must survive a page refresh.
 *
 * Root cause (settings-subsystem refactor): the work-folder textareas
 * (`#workAccessFoldersRW` / `#workAccessFoldersRO`) were absent from POOL_SETTINGS_MAP,
 * so `syncPoolSettings()` — the ONLY place the authoritative server snapshot
 * (`serverSettingsSnapshot` / `pendingSettings`) is hydrated into the DOM — never
 * populated them. After a refresh the textareas fell back to their empty HTML default,
 * even though the server persisted the folders to pool_settings.json and re-emitted them
 * in the state frame.
 *
 * This test extracts the REAL `syncPoolSettings` + `POOL_SETTINGS_MAP` from web_ui/app.js
 * and runs them in a sandbox with a minimal DOM stub (same technique as
 * test_settings_pageshow_rehydrate.js):
 *   1. Source-level: both work-folder keys are wired into POOL_SETTINGS_MAP.
 *   2. Behavioural: a snapshot carrying the arrays hydrates the textareas as
 *      newline-joined strings (the exact shape the textareas render).
 *   3. Empty-list clear: an empty array round-trips to an empty textarea value.
 *
 * Run:  node tests/test_settings_work_folders_hydrate.js   (exits 0 on pass / 1 on fail)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const APP_JS = fs.readFileSync(path.join(__dirname, '..', 'web_ui', 'app.js'), 'utf8');

function extractConstArray(name) {
  const re = new RegExp('const\\s+' + name + '\\s*=\\s*\\[', 'g');
  const m = re.exec(APP_JS);
  assert.ok(m, `Could not find "const ${name} = [" in app.js`);
  let i = m.index + m[0].length;
  while (i < APP_JS.length && !APP_JS.startsWith('];', i)) i++;
  assert.ok(i < APP_JS.length, `Unterminated array for ${name}`);
  return APP_JS.slice(m.index, i + 2);
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
const REVERT_SRC = extractFunction('revertControl');

function makeDom(ids) {
  const elements = {};
  for (const id of ids) elements[id] = { value: '', checked: false };
  const querySelector = (sel) => {
    if (typeof sel !== 'string') return null;
    const key = sel.startsWith('#') ? sel : '#' + sel;
    return elements[key] || null;
  };
  const documentStub = {
    activeElement: null,
    getElementById: (id) => elements[id] || null,
    querySelector,
  };
  return { elements, documentStub, $: querySelector };
}

function buildSandbox(dom) {
  const localStorage = {
    getItem() { return null; },
    setItem() {},
    removeItem() {},
  };
  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
    localStorage,
    state: { agents: [], connected: true },
    agentDisabledTools: {},
    send() {},
    updateTwoPhaseInputsEnabled() {},
    renderToolsForSelectedAgent() {},
    afkToggle: null,
    settingAfkMessage: null,
    afkModeSelect: null,
    settingToolsList: null,
    settingAgentSelect: null,
    showInSystemToastBar() {},
    serverSettingsSnapshot: null,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
  vm.runInContext(REVERT_SRC, ctx, { filename: 'revertControl.js' });
  return ctx;
}

test('POOL_SETTINGS_MAP wires both work-folder keys (source-level)', () => {
  // The regression is a missing map entry, so guard the exact seams: each key must be
  // present with its textarea DOM id, or syncPoolSettings() will silently skip it.
  assert.ok(MAP_SRC.includes("key: 'work_access_folders_rw'"),
    "work_access_folders_rw missing from POOL_SETTINGS_MAP (hydration seam)");
  assert.ok(MAP_SRC.includes("key: 'work_access_folders_ro'"),
    "work_access_folders_ro missing from POOL_SETTINGS_MAP (hydration seam)");
  assert.ok(MAP_SRC.includes("#workAccessFoldersRW"),
    "#workAccessFoldersRW DOM id missing from POOL_SETTINGS_MAP");
  assert.ok(MAP_SRC.includes("#workAccessFoldersRO"),
    "#workAccessFoldersRO DOM id missing from POOL_SETTINGS_MAP");
  // Same family: scalar server-backed keys that were also absent from the map and
  // silently fell back to their HTML defaults (3 / checked / 2) on refresh.
  assert.ok(MAP_SRC.includes("key: 'max_parallel_agents'"),
    "max_parallel_agents missing from POOL_SETTINGS_MAP (hydration seam)");
  assert.ok(MAP_SRC.includes("key: 'auto_continue'"),
    "auto_continue missing from POOL_SETTINGS_MAP (hydration seam)");
  assert.ok(MAP_SRC.includes("key: 'max_images_for_llm'"),
    "max_images_for_llm missing from POOL_SETTINGS_MAP (hydration seam)");
});

test('syncPoolSettings hydrates the scalar pool keys (max_parallel_agents / auto_continue / max_images_for_llm)', () => {
  const dom = makeDom(['#setting-max-parallel', '#setting-auto-continue', '#setting-max-images-for-llm']);
  const ctx = buildSandbox(dom);

  // Pre-seed the controls with stale (HTML-default) values, then hydrate from the snapshot.
  dom.elements['#setting-max-parallel'].value = '3';
  dom.elements['#setting-auto-continue'].checked = true;
  dom.elements['#setting-max-images-for-llm'].value = '2';

  const snapshot = { max_parallel_agents: 5, auto_continue: false, max_images_for_llm: 7 };
  ctx.snapshot = snapshot;
  vm.runInContext('syncPoolSettings(snapshot)', ctx);

  assert.equal(String(dom.elements['#setting-max-parallel'].value), '5',
    'max_parallel_agents was not hydrated from the snapshot');
  assert.equal(dom.elements['#setting-auto-continue'].checked, false,
    'auto_continue was not hydrated from the snapshot');
  assert.equal(String(dom.elements['#setting-max-images-for-llm'].value), '7',
    'max_images_for_llm was not hydrated from the snapshot');
});

test('syncPoolSettings hydrates work-folder textareas from the server snapshot', () => {
  const dom = makeDom(['#workAccessFoldersRW', '#workAccessFoldersRO']);
  const ctx = buildSandbox(dom);

  // The authoritative server snapshot after a state frame (arrays, newline-free).
  const snapshot = {
    work_access_folders_rw: ['C:\\work\\rw', 'N:\\data'],
    work_access_folders_ro: ['/mnt/ro'],
  };
  ctx.snapshot = snapshot;
  vm.runInContext('syncPoolSettings(snapshot)', ctx);

  assert.equal(dom.elements['#workAccessFoldersRW'].value, 'C:\\work\\rw\nN:\\data',
    'RW textarea was not hydrated from the snapshot array');
  assert.equal(dom.elements['#workAccessFoldersRO'].value, '/mnt/ro',
    'RO textarea was not hydrated from the snapshot array');
});

test('syncPoolSettings round-trips an empty work-folder list (intentional clear)', () => {
  const dom = makeDom(['#workAccessFoldersRW', '#workAccessFoldersRO']);
  const ctx = buildSandbox(dom);

  // Pre-seed the textareas with stale values, then hydrate with empty arrays — the
  // clear must be reflected (Batch A deliverable: empty list round-trips).
  dom.elements['#workAccessFoldersRW'].value = 'stale-rw';
  dom.elements['#workAccessFoldersRO'].value = 'stale-ro';

  const snapshot = { work_access_folders_rw: [], work_access_folders_ro: [] };
  ctx.snapshot = snapshot;
  vm.runInContext('syncPoolSettings(snapshot)', ctx);

  assert.equal(dom.elements['#workAccessFoldersRW'].value, '',
    'empty RW list did not clear the textarea');
  assert.equal(dom.elements['#workAccessFoldersRO'].value, '',
    'empty RO list did not clear the textarea');
});

test('revertControl reverts an array-valued work-folder key to a joined string (not comma-garbage)', () => {
  const dom = makeDom(['#workAccessFoldersRW', '#workAccessFoldersRO', '#setting-grep-char-limit', '#setting-enable-skills']);
  const ctx = buildSandbox(dom);

  // After a per-key rejection the control reverts to the authoritative snapshot value.
  // The snapshot carries an array; without the map's transform, String(array) would
  // produce a comma-joined single bogus path.
  dom.elements['#workAccessFoldersRW'].value = 'user-typed-wrong';
  ctx.serverSettingsSnapshot = {
    work_access_folders_rw: ['N:\\data', 'N:\\other'],
    grep_char_limit: 12000,
    default_load_skill_mode: 'MANUAL',
  };

  vm.runInContext('revertControl("work_access_folders_rw")', ctx);
  assert.equal(dom.elements['#workAccessFoldersRW'].value, 'N:\\data\nN:\\other',
    'revertControl must apply the map transform to an array value (newline-joined, not comma-joined)');

  // A scalar key with no transform keeps the historical String() coercion path.
  dom.elements['#setting-grep-char-limit'].value = '999';
  vm.runInContext('revertControl("grep_char_limit")', ctx);
  assert.equal(String(dom.elements['#setting-grep-char-limit'].value), '12000',
    'scalar revert must keep the String() coercion for non-transform keys');

  // A checked+transform key reverts through the transform (default_load_skill_mode
  // transform is (v) => v === 'AUTO'): 'MANUAL' must yield checked=false, not !!'MANUAL'=true.
  dom.elements['#setting-enable-skills'].checked = true;
  vm.runInContext('revertControl("default_load_skill_mode")', ctx);
  assert.equal(dom.elements['#setting-enable-skills'].checked, false,
    'checked+transform revert must apply the transform, not a raw truthy coercion');
});

test('syncPoolSettings skips the actively-focused textarea (no mid-edit stomp)', () => {
  const dom = makeDom(['#workAccessFoldersRW', '#workAccessFoldersRO']);
  const ctx = buildSandbox(dom);

  // The user is focused in the RW textarea while a frame arrives — it must NOT be stomped,
  // while the unfocused RO textarea IS hydrated.
  dom.elements['#workAccessFoldersRW'].value = 'user is typing…';
  dom.documentStub.activeElement = dom.elements['#workAccessFoldersRW'];

  ctx.snapshot = { work_access_folders_rw: ['N:\\data'], work_access_folders_ro: ['/mnt/ro'] };
  vm.runInContext('syncPoolSettings(snapshot)', ctx);

  assert.equal(dom.elements['#workAccessFoldersRW'].value, 'user is typing…',
    'focused textarea must not be overwritten by a server frame');
  assert.equal(dom.elements['#workAccessFoldersRO'].value, '/mnt/ro',
    'unfocused textarea must still be hydrated from the snapshot');
});

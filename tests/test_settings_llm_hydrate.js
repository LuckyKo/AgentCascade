'use strict';
/**
 * Regression: General-tab LLM settings (sampling, max tokens, context size) must survive a
 * page refresh and must not be stomped by the next settings save.
 *
 * Root cause (BUG_0274): every LLM key is persisted to
 * `config/api_endpoints.json::default_llm_cfg`, but the state snapshot only re-emitted
 * `api_base` and `model` (BUG_0252). The corresponding controls were absent from
 * `POOL_SETTINGS_MAP`, so `syncPoolSettings()` never hydrated them; after a refresh they
 * kept their HTML defaults, and `saveSettings()` re-sent those defaults through
 * `queueSettingChange()` (which only suppressed values already in `pendingSettings`),
 * overwriting the persisted values on the server.
 *
 * This test extracts the REAL `POOL_SETTINGS_MAP` / `syncPoolSettings` /
 * `queueSettingChange` from web_ui/app.js and runs them in a sandbox with a minimal DOM
 * stub (same technique as test_settings_work_folders_hydrate.js):
 *   1. Source-level: every LLM control is wired into POOL_SETTINGS_MAP.
 *   2. Behavioural: a snapshot with non-default values hydrates every control (including
 *      the range readouts and the log_api_post checkbox).
 *   3. Write hardening: no delta is queued before the first authoritative snapshot, and
 *      an unchanged hydrated value is not re-sent.
 *
 * Run:  node tests/test_settings_llm_hydrate.js   (exits 0 on pass / 1 on fail)
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
const QUEUE_SRC = extractFunction('queueSettingChange');

// Keys the General-tab (plus log_api_post) LLM controls write through getGenerateCfg().
// Each must be hydrated from the server snapshot in default_llm_cfg.
const LLM_CONTROLS = [
  { id: '#setting-temperature', key: 'temperature', prop: 'value' },
  { id: '#setting-top-p', key: 'top_p', prop: 'value' },
  { id: '#setting-top-k', key: 'top_k', prop: 'value' },
  { id: '#setting-min-p', key: 'min_p', prop: 'value' },
  { id: '#setting-repeat-penalty', key: 'repeat_penalty', prop: 'value' },
  { id: '#setting-presence-penalty', key: 'presence_penalty', prop: 'value' },
  { id: '#setting-frequency-penalty', key: 'frequency_penalty', prop: 'value' },
  { id: '#setting-max-tokens', key: 'max_tokens', prop: 'value' },
  { id: '#setting-max-context', key: 'max_input_tokens', prop: 'value' },
  { id: '#setting-log-api-post', key: 'log_api_post', prop: 'checked' },
];

function makeDom(ids, steps = {}) {
  const elements = {};
  for (const id of ids) elements[id] = { value: '', checked: false, step: steps[id] || '' };
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
  const timers = [];
  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
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
    _flushSettingChanges() {},
    serverSettingsSnapshot: null,
    pendingSettings: null,
    settingsRevision: 0,
    _pendingSettingChanges: {},
    _settingFlushTimer: null,
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
  vm.runInContext(QUEUE_SRC, ctx, { filename: 'queueSettingChange.js' });
  ctx.timers = timers;
  return ctx;
}

test('POOL_SETTINGS_MAP wires every LLM control (source-level)', () => {
  for (const { id, key, prop } of LLM_CONTROLS) {
    assert.ok(MAP_SRC.includes(`id: '${id}'`),
      `${id} missing from POOL_SETTINGS_MAP (hydration seam)`);
    assert.ok(MAP_SRC.includes(`key: '${key}'`),
      `${key} missing from POOL_SETTINGS_MAP (hydration seam)`);
    assert.ok(new RegExp(`id: '${id.replace('#', '\\#')}'[^}]*prop: '${prop}'`).test(MAP_SRC),
      `${id} must be hydrated via prop '${prop}'`);
  }
});

test('syncPoolSettings hydrates every General-tab LLM control from the snapshot', () => {
  const ids = LLM_CONTROLS.map(c => c.id)
    .concat(['#val-temperature', '#val-top-p', '#val-top-k', '#val-min-p',
      '#val-repeat-penalty', '#val-presence-penalty', '#val-frequency-penalty']);
  const steps = {
    '#setting-temperature': '0.05',
    '#setting-top-p': '0.01',
    '#setting-top-k': '1',
    '#setting-min-p': '0.01',
    '#setting-repeat-penalty': '0.01',
    '#setting-presence-penalty': '0.1',
    '#setting-frequency-penalty': '0.1',
  };
  const dom = makeDom(ids, steps);
  const ctx = buildSandbox(dom);

  // Pre-seed every control with its stale HTML default (as after a refresh) to prove the
  // snapshot overwrites it.
  dom.elements['#setting-temperature'].value = '0.8';
  dom.elements['#setting-top-p'].value = '0.95';
  dom.elements['#setting-top-k'].value = '40';
  dom.elements['#setting-min-p'].value = '0.05';
  dom.elements['#setting-repeat-penalty'].value = '1.05';
  dom.elements['#setting-presence-penalty'].value = '0';
  dom.elements['#setting-frequency-penalty'].value = '0';
  dom.elements['#setting-max-tokens'].value = '8192';
  dom.elements['#setting-max-context'].value = '65536';
  dom.elements['#setting-log-api-post'].checked = false;
  dom.elements['#val-temperature'].textContent = '0.8';

  const snapshot = {
    temperature: 1.3, top_p: 0.5, top_k: 80, min_p: 0.1,
    repeat_penalty: 1.2, presence_penalty: 0.5, frequency_penalty: 0.3,
    max_tokens: 16384, max_input_tokens: 131072, log_api_post: true,
  };
  ctx.snapshot = snapshot;
  vm.runInContext('syncPoolSettings(snapshot)', ctx);

  for (const { id, key, prop } of LLM_CONTROLS) {
    const actual = dom.elements[id][prop];
    assert.equal(String(actual), String(snapshot[key]),
      `${key} was not hydrated into ${id} (kept stale HTML default)`);
  }
  // Range readouts must follow the hydrated value, not stay on the HTML default.
  assert.equal(dom.elements['#val-temperature'].textContent, '1.30',
    'temperature range readout was not refreshed by hydration');
  assert.equal(dom.elements['#val-top-k'].textContent, '80',
    'top_k range readout was not refreshed by hydration');
});

test('queueSettingChange drops changes before the first authoritative snapshot', () => {
  // A save fired before the state frame (or any early edit) must not send HTML defaults:
  // pendingSettings is null until the server snapshot arrives.
  const dom = makeDom([]);
  const ctx = buildSandbox(dom);
  assert.equal(ctx.pendingSettings, null);

  vm.runInContext("queueSettingChange('temperature', 0.8)", ctx);
  assert.deepEqual(Object.keys(ctx._pendingSettingChanges), [],
    'a delta was queued before the authoritative snapshot arrived (default-stomp risk)');
  assert.equal(ctx.timers.length, 0, 'no flush timer must be scheduled before hydration');
});

test('queueSettingChange suppresses an unchanged hydrated value and queues a real edit', () => {
  const dom = makeDom([]);
  const ctx = buildSandbox(dom);
  // Simulate the state frame: server value 0.8, and the DOM was hydrated to it.
  ctx.pendingSettings = { temperature: 0.8, max_tokens: 8192 };

  vm.runInContext("queueSettingChange('temperature', 0.8)", ctx);
  assert.deepEqual(Object.keys(ctx._pendingSettingChanges), [],
    're-sending an unchanged hydrated value would overwrite the server unnecessarily');

  vm.runInContext("queueSettingChange('temperature', 1.3)", ctx);
  assert.equal(ctx._pendingSettingChanges.temperature, 1.3,
    'a real edit must still be queued after hydration');
});

'use strict';
/**
 * Batch B (Phase 2): the server is the single source of truth for settings.
 *
 * This test proves that a stale LOCAL preference no longer blocks a newer server value:
 * after the local-preference guards were removed from syncPoolSettings() (the `localKey`
 * skip and the `hasLocalDisabledTools` guard), a server snapshot value overwrites a stale
 * localStorage value on the controls it covers.
 *
 * It extracts the REAL syncPoolSettings + POOL_SETTINGS_MAP from web_ui/app.js (so it always
 * tests current source, not a copy) and runs them in a sandbox with a minimal DOM stub.
 *
 * Run:  node tests/test_settings_server_wins.js   (exits 0 on pass / 1 on fail)
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
    renderCliAgentControls() {},  // called at the end of syncPoolSettings (CLI Agents)
    cliAgentEnabledMap: {},       // getGenerateCfg reads it; syncPoolSettings re-assigns it
    cliAgentModelsMap: {},
    renderToolsForSelectedAgent() {},
    afkToggle: null,
    settingAfkMessage: null,
    afkModeSelect: null,
    settingToolsList: null,
    settingAgentSelect: null,
  };
  sandbox.saveSettings = saveSettingsSpy || (() => {});
  const ctx = vm.createContext(sandbox);
  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
  return { sandbox, ctx, localStorageData };
}

test('server value wins over a stale localStorage preference (no local-preference guard)', () => {
  const dom = makeDom(['#setting-grep-char-limit']);
  const { ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // A stale LOCAL preference under the underscore key (what the user saved in a prior session).
  localStorageData['agent-cascade-settings'] = JSON.stringify({ grep_char_limit: 150 });
  // The DOM currently shows the stale local value.
  dom.elements['#setting-grep-char-limit'].value = '150';

  // The server's authoritative value is DIFFERENT (newer).
  vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: { grep_char_limit: 999 } }));

  // Server value must win over the stale local value. (The DOM stub keeps numbers as numbers;
  // a real input coerces to string, so compare in string form.)
  assert.equal(String(dom.elements['#setting-grep-char-limit'].value), '999',
    `stale localStorage value beat the server value (got '${dom.elements['#setting-grep-char-limit'].value}')`);
});

test('server disabled_tools hydrate even when a stale local list is present', () => {
  const dom = makeDom([]);
  const { ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // A stale local disabled-tools list is present (would have blocked the sync pre-Batch B).
  localStorageData['agent-cascade-disabled-tools'] = JSON.stringify({ orchestrator: ['shell'] });
  ctx.state.agents = [{ agent_type: 'orchestrator', name: 'o' }];

  vm.runInContext('syncPoolSettings(ps)',
    Object.assign(ctx, { ps: { disabled_tools: { orchestrator: ['grep', 'shell'] } } }));

  // Server is authoritative (Batch B/C): the in-memory map is updated from the server value.
  // We no longer write to localStorage — the server is the single source of truth.
  assert.deepEqual(ctx.agentDisabledTools.orchestrator, ['grep', 'shell'],
    'stale local disabled_tools blocked the authoritative server value');
});

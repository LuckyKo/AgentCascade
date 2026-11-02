'use strict';
/**
 * Batch B (Phase 2): on Back/Forward (bfcache) restore, the UI re-hydrates from the SERVER's
 * last snapshot (`pendingSettings`), not from localStorage, and does NOT push a full
 * `update_config` back to the server.
 *
 * This test proves:
 *   1. Source-level: the `pageshow` listener body references `pendingSettings` /
 *      `syncPoolSettings` and contains no `update_config` send.
 *   2. Behavioural: re-running the hydration path (`syncPoolSettings(pendingSettings)`)
 *      overwrites stale bfcache-restored DOM values with the server snapshot.
 *
 * It extracts the REAL syncPoolSettings + POOL_SETTINGS_MAP and the pageshow listener from
 * web_ui/app.js and runs them in a sandbox with a minimal DOM stub.
 *
 * Run:  node tests/test_settings_pageshow_rehydrate.js   (exits 0 on pass / 1 on fail)
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

function extractPageshowBlock() {
  const start = APP_JS.indexOf("addEventListener('pageshow'");
  assert.ok(start >= 0, 'pageshow listener not found in app.js');
  const end = APP_JS.indexOf('});', start);
  assert.ok(end > start, 'pageshow listener close not found');
  return APP_JS.slice(start, end + 3);
}

const MAP_SRC = extractConstArray('POOL_SETTINGS_MAP');
const SYNC_SRC = extractFunction('syncPoolSettings');
const PAGESHOW_SRC = extractPageshowBlock();

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
  };
  sandbox.saveSettings = saveSettingsSpy || (() => {});
  const ctx = vm.createContext(sandbox);
  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
  return { sandbox, ctx, localStorageData };
}

test('pageshow listener re-hydrates from pendingSettings and sends no update_config', () => {
  assert.ok(PAGESHOW_SRC.includes('pendingSettings'),
    'pageshow handler must re-hydrate from the server snapshot (pendingSettings)');
  assert.ok(PAGESHOW_SRC.includes('syncPoolSettings'),
    'pageshow handler must call syncPoolSettings to re-hydrate');
  // Check for an update_config SEND (not a comment mentioning it): strip line comments first.
  const code = PAGESHOW_SRC.replace(/\/\/[^\n]*/g, '');
  assert.ok(!/update_config/.test(code),
    'pageshow handler must not push a full update_config back to the server');
});

test('bfcache re-hydration overwrites stale DOM values with the server snapshot', () => {
  const dom = makeDom(['#setting-grep-char-limit']);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // Simulate a bfcache restore: the DOM shows a stale value from the previous forward/back.
  dom.elements['#setting-grep-char-limit'].value = '42';

  // The server's last authoritative snapshot (what pendingSettings holds after a state frame).
  const pendingSettings = { grep_char_limit: 777 };
  ctx.pendingSettings = pendingSettings;

  // The pageshow handler's re-hydration step: syncPoolSettings(pendingSettings).
  vm.runInContext('syncPoolSettings(pendingSettings)', ctx);

  assert.equal(String(dom.elements['#setting-grep-char-limit'].value), '777',
    `bfcache re-hydration did not restore the server value (got '${dom.elements['#setting-grep-char-limit'].value}')`);
});

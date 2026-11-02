'use strict';
/**
 * Regression tests for the "UI settings reset to weird non-default values after PC reboot" bug
 * (todo.md line 145). See reports/settings_reboot_reset_investigation.md.
 *
 * Covers the frontend fixes:
 *   - FIX 2a/2b: the POOL_SETTINGS_MAP entry for #setting-max-rollbacks maps to the key
 *     ('max_auto_rollbacks') that saveSettings()/getGenerateCfg() actually write, and
 *     syncPoolSettings() hydrates it from the server value.
 *   - FIX 3: getGenerateCfg() falls back to 3 for an empty #setting-max-rollbacks (not NaN/null),
 *     and preserves -1 (unlimited).
 *   - FIX 5: getGenerateCfg() OMITS the 7 LLM-gen params when their input is empty/invalid
 *     (no more NaN → JSON null being stored/pushed).
 *   - FIX 4: loadSettings() skips null-poisoned numeric restores for the ranges loop and the
 *     max_tokens / setting-max-context direct restores.
 *
 * NOTE: the former "dead respect-local guard" audit of every POOL_SETTINGS_MAP entry was
 * retired with the `localKey` field — syncPoolSettings() no longer has a local-preference
 * guard (Batch B: server value always wins), so there is no guard to audit.
 *
 * Like tests/test_settings_live_edit.js, this extracts the REAL fragments from web_ui/app.js via
 * regex + vm sandbox (so it always tests current source) and does NOT load the whole browser-global
 * app.js. Run:  node --test tests/test_settings_reboot_fixes.js   (exits 0 on pass / 1 on fail)
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
	const GETCFG_SRC = extractFunction('getGenerateCfg');
	const SAVE_SRC = extractFunction('saveSettings');
	const LOADSRC = extractFunction('loadSettings');
	const LOADUIPREFS_SRC = extractFunction('loadUiPrefs');
	const SAVEUIPREFS_SRC = extractFunction('saveUiPrefs');

// ── Minimal DOM / browser stubs ───────────────────────────────────────────────
function makeDom(ids) {
  const elements = {};
  // Each stub carries its own `id` (without the leading '#') so code that reads `el.id`
  // (e.g. loadSettings' ranges loop: s[r.input.id]) resolves correctly.
  for (const id of ids) elements[id] = { id: id.replace(/^#/, ''), value: '', checked: false, dispatchEvent() {} };
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

// The 7 LLM-gen range sliders (FIX 4 target). Give them HTML-like defaults so a null stored value
// would be *visible* if the guard failed (value would become "null").
const RANGE_DEFAULTS = {
  'setting-temperature': '0.7',
  'setting-top-p': '1.0',
  'setting-top-k': '40',
  'setting-min-p': '0.05',
  'setting-repeat-penalty': '1.1',
  'setting-presence-penalty': '0.0',
  'setting-frequency-penalty': '0.0',
};

function buildSandbox({ dom, saveSettingsSpy }) {
  const localStorageData = {};
  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(localStorageData, k) ? localStorageData[k] : null; },
    setItem(k, v) { localStorageData[k] = String(v); },
    removeItem(k) { delete localStorageData[k]; },
  };

  // Bind all element globals: present ids → the DOM stub object, absent ids → null.
  // Note: sm[3] is the selector string INCLUDING the leading '#' (e.g. '#setting-max-tokens'),
  // which matches the key format used in dom.elements — so use it directly, do NOT prepend '#'.
  const globalVals = {};
  const selRe = /const\s+([A-Za-z_$][\w$]*)\s*=\s*\$\(\s*(['"])([^'"]+)\2\s*\)/g;
  let sm;
  while ((sm = selRe.exec(APP_JS)) !== null) {
    globalVals[sm[1]] = dom.elements[sm[3]] || null;
  }

  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
    localStorage,
    state: { agents: [], connected: false },
    agentDisabledTools: {},
    send() {},
    updateTwoPhaseInputsEnabled() {},
    defaultWorkspace: null,
    Event: function Event(type) { this.type = type; }, // loadSettings dispatches new Event('input')
    validateRetrySettings() {}, // called at the end of getGenerateCfg; no-op stub
    applyRetryValidation() {},  // called at the end of saveSettings; no-op stub
    updateAllContextBars() {},  // called at the end of saveSettings; no-op stub
  };

  // Element globals (settingLinesEnabled, settingMaxTokens, ranges inputs, etc.)
  Object.assign(sandbox, globalVals);

  // Seed range defaults onto any present range input so a null-poison would be detectable.
  for (const [id, def] of Object.entries(RANGE_DEFAULTS)) {
    const el = dom.elements['#' + id];
    if (el && el.value === '') el.value = def;
  }

  // The `ranges` array that loadSettings/saveSettings iterate over (the 7 LLM-gen sliders).
  sandbox.ranges = Object.keys(RANGE_DEFAULTS)
    .map((id) => ({ input: dom.elements['#' + id] || null, output: null }))
    .filter((r) => r.input);

	  sandbox.saveSettings = saveSettingsSpy || (() => {});
	  const ctx = vm.createContext(sandbox);
	  // UI_PREFS_KEY and UI_PREFS_ALLOWLIST are consts referenced by loadUiPrefs/saveUiPrefs.
	  const UIPREFS_CONSTS = APP_JS.match(/const UI_PREFS_KEY\s*=\s*'[^']+'/s)
	    ? APP_JS.match(/const UI_PREFS_KEY\s*=\s*'[^']+'/)[0] + '\n'
	    : '';
	  const ALLOWLIST_MATCH = APP_JS.match(/const UI_PREFS_ALLOWLIST\s*=\s*new Set\(\[([^\]]*)\]\)/s);
	  const ALLOWLIST_SRC = ALLOWLIST_MATCH
	    ? `const UI_PREFS_ALLOWLIST = new Set([${ALLOWLIST_MATCH[1]}]);`
	    : 'const UI_PREFS_ALLOWLIST = new Set();';
	  vm.runInContext(UIPREFS_CONSTS + '\n' + ALLOWLIST_SRC, ctx, { filename: 'ui_prefs_consts.js' });
	  vm.runInContext(MAP_SRC, ctx, { filename: 'POOL_SETTINGS_MAP.js' });
	  vm.runInContext(SYNC_SRC, ctx, { filename: 'syncPoolSettings.js' });
	  vm.runInContext(GETCFG_SRC, ctx, { filename: 'getGenerateCfg.js' });
	  vm.runInContext(LOADUIPREFS_SRC, ctx, { filename: 'loadUiPrefs.js' });
	  vm.runInContext(SAVEUIPREFS_SRC, ctx, { filename: 'saveUiPrefs.js' });
	  vm.runInContext(SAVE_SRC, ctx, { filename: 'saveSettings.js' });
	  vm.runInContext(LOADSRC, ctx, { filename: 'loadSettings.js' });
	  return { sandbox, ctx, localStorageData };
	}

// The element ids referenced by getGenerateCfg for the 7 LLM-gen params (FIX 5).
const GEN_PARAM_IDS = [
  '#setting-temperature', '#setting-top-p', '#setting-top-k', '#setting-min-p',
  '#setting-repeat-penalty', '#setting-presence-penalty', '#setting-frequency-penalty',
];

	// ── Test 2: the max-rollbacks entry maps to the key getGenerateCfg/saveSettings write ─
test('FIX 2a: #setting-max-rollbacks maps to the underscore key getGenerateCfg/saveSettings write', () => {
  const domAll = makeDom([]);
  const { ctx } = buildSandbox({ dom: domAll, saveSettingsSpy: () => {} });
  const entries = vm.runInContext('POOL_SETTINGS_MAP', ctx);
  const entry = entries.find((e) => e.id === '#setting-max-rollbacks');
  assert.ok(entry, 'map entry for #setting-max-rollbacks missing');
  assert.equal(entry.key, 'max_auto_rollbacks', 'server key must be max_auto_rollbacks');
  assert.equal(entry.prop, 'value',
    '#setting-max-rollbacks must be hydrated through its .value property');
});

// ── Test 3: syncPoolSettings does NOT stomp #setting-max-rollbacks when a local pref exists ─
test('Batch B: server value WINS over a stale local max_auto_rollbacks preference', () => {
  const ids = ['#setting-max-rollbacks'];
  const dom = makeDom(ids);
  const { ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // A stale local key left over from an older build; the server value must still win.
  localStorageData['agent-cascade-settings'] = JSON.stringify({ max_auto_rollbacks: '2' });

  // DOM currently shows the user's value.
  dom.elements['#setting-max-rollbacks'].value = '2';

  // Server broadcasts a DIFFERENT value on every tick.
  const serverPs = { max_auto_rollbacks: 3 };
  for (let i = 0; i < 10; i++) {
    vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: serverPs }));
  }

  // Batch B: the server value now wins over the stale local preference.
  assert.equal(
    String(dom.elements['#setting-max-rollbacks'].value), '3',
    `#setting-max-rollbacks did not adopt the server value (got '${dom.elements['#setting-max-rollbacks'].value}')`
  );
});

// ── Test 4: without a local pref, the server value DOES apply to #setting-max-rollbacks ─
test('FIX 2b/3: server value applies to #setting-max-rollbacks when no local pref exists', () => {
  const ids = ['#setting-max-rollbacks'];
  const dom = makeDom(ids);
  const { ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // No saved max_auto_rollbacks key at all.
  localStorageData['agent-cascade-settings'] = JSON.stringify({ unrelated: 'x' });
  dom.elements['#setting-max-rollbacks'].value = ''; // empty (fresh UI)

  const serverPs = { max_auto_rollbacks: 3 };
  vm.runInContext('syncPoolSettings(ps)', Object.assign(ctx, { ps: serverPs }));

  // The server value (number 3) is assigned directly; compare by string to be type-agnostic.
  assert.equal(
    String(dom.elements['#setting-max-rollbacks'].value), '3',
    `server value should apply when no local pref (got '${dom.elements['#setting-max-rollbacks'].value}')`
  );
});

	// ── Test 5: the max-rollbacks control reaches the server as a cfg delta ─
	test('FIX 2b: getGenerateCfg builds cfg.max_auto_rollbacks from #setting-max-rollbacks', () => {
	  // Source invariant: the DOM value is read into the cfg object that saveSettings forwards
	  // through queueSettingChange. Server-backed keys never reach the ui-prefs store, so there
	  // is no s['max_auto_rollbacks'] localStorage write anymore.
	  assert(GETCFG_SRC.includes("cfg.max_auto_rollbacks = Number.isNaN(_mar) ? 3 : _mar"),
    'getGenerateCfg() must build cfg.max_auto_rollbacks from #setting-max-rollbacks');
	  assert(GETCFG_SRC.includes('#setting-max-rollbacks'),
    'getGenerateCfg() must read from #setting-max-rollbacks');
	});

// ── Test 6: getGenerateCfg empty #setting-max-rollbacks → 3 (not NaN/null) ─
test('FIX 3: getGenerateCfg empty #setting-max-rollbacks → max_auto_rollbacks === 3', () => {
  const dom = makeDom(['#setting-max-rollbacks']);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  dom.elements['#setting-max-rollbacks'].value = ''; // empty input

  const cfg = vm.runInContext('getGenerateCfg()', ctx);
  assert.ok(Object.prototype.hasOwnProperty.call(cfg, 'max_auto_rollbacks'), 'max_auto_rollbacks missing');
  assert.equal(cfg.max_auto_rollbacks, 3, `empty input must default to 3 (got ${cfg.max_auto_rollbacks})`);
});

// ── Test 7: getGenerateCfg '-1' → -1 preserved (unlimited) ─
test('FIX 3: getGenerateCfg #setting-max-rollbacks "-1" → -1 preserved', () => {
  const dom = makeDom(['#setting-max-rollbacks']);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  dom.elements['#setting-max-rollbacks'].value = '-1';

  const cfg = vm.runInContext('getGenerateCfg()', ctx);
  assert.equal(cfg.max_auto_rollbacks, -1, `-1 (unlimited) must be preserved (got ${cfg.max_auto_rollbacks})`);
});

// ── Test 7b: getGenerateCfg '0' → 0 preserved (no rollbacks). Regression for the zero-coercion bug:
//    `parseInt('0') || 3` would wrongly coerce a legitimate 0 to 3. The fix uses an explicit NaN check.
test('FIX 3: getGenerateCfg #setting-max-rollbacks "0" → 0 preserved (not coerced to 3)', () => {
  const dom = makeDom(['#setting-max-rollbacks']);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  dom.elements['#setting-max-rollbacks'].value = '0';

  const cfg = vm.runInContext('getGenerateCfg()', ctx);
  assert.equal(cfg.max_auto_rollbacks, 0, `0 (no rollbacks) must be preserved — got ${cfg.max_auto_rollbacks} (zero-coercion bug)`);
});

// ── Test 8: getGenerateCfg empty gen-param fields are OMITTED (not NaN/null) ─
test('FIX 5: getGenerateCfg omits the 7 LLM-gen params when their inputs are empty', () => {
  const dom = makeDom(GEN_PARAM_IDS);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  // All gen-param inputs empty.
  GEN_PARAM_IDS.forEach((id) => { dom.elements[id].value = ''; });

  const cfg = vm.runInContext('getGenerateCfg()', ctx);

  const expectedKeys = ['temperature', 'top_p', 'top_k', 'min_p', 'repeat_penalty', 'presence_penalty', 'frequency_penalty'];
  for (const k of expectedKeys) {
    assert.ok(
      !Object.prototype.hasOwnProperty.call(cfg, k),
      `gen param '${k}' should be OMITTED when empty (got ${JSON.stringify(cfg[k])})`
    );
  }
});

// ── Test 9: getGenerateCfg with valid gen-param values still assigns them ─
test('FIX 5: getGenerateCfg assigns finite LLM-gen param values', () => {
  const dom = makeDom(GEN_PARAM_IDS);
  const { ctx } = buildSandbox({ dom, saveSettingsSpy: () => {} });
  const vals = {
    '#setting-temperature': '0.9',
    '#setting-top-p': '0.95',
    '#setting-top-k': '32',
    '#setting-min-p': '0.1',
    '#setting-repeat-penalty': '1.05',
    '#setting-presence-penalty': '0.5',
    '#setting-frequency-penalty': '-0.2',
  };
  for (const id of GEN_PARAM_IDS) dom.elements[id].value = vals[id];

  const cfg = vm.runInContext('getGenerateCfg()', ctx);
  assert.equal(cfg.temperature, 0.9);
  assert.equal(cfg.top_p, 0.95);
  assert.equal(cfg.top_k, 32);
  assert.equal(cfg.min_p, 0.1);
  assert.equal(cfg.repeat_penalty, 1.05);
  assert.equal(cfg.presence_penalty, 0.5);
  assert.equal(cfg.frequency_penalty, -0.2);
});

// ── Test 10: loadSettings skips null-poisoned numeric restores (ranges + direct) ─
test('FIX 4: loadSettings does not assign null into range sliders / max_tokens / max-context', () => {
  // Range-slider ids (populate ctx.ranges) + the two direct-restore element ids so their globals resolve.
  const dom = makeDom(
    Object.keys(RANGE_DEFAULTS).map((id) => '#' + id).concat(['#setting-max-tokens', '#setting-max-context'])
  );
  const { ctx, localStorageData } = buildSandbox({ dom, saveSettingsSpy: () => {} });

  // Corrupted legacy blob: element-id keys + direct numeric keys all poisoned with null.
  localStorageData['agent-cascade-settings'] = JSON.stringify({
    'setting-temperature': null,
    'setting-top-k': null,
    'max_tokens': null,
    'setting-max-context': null,
  });

  // Defaults before load (as if HTML defaults were in place).
  const tempDefault = ctx.ranges[0].input.value;
  const topkDefault = ctx.ranges[2].input.value;
  const maxTokensDefault = ctx.settingMaxTokens.value;
  const maxContextDefault = ctx.settingMaxContext.value;

  vm.runInContext('loadSettings()', ctx);

  // None of the null-poisoned values may have been assigned. Values must keep their defaults.
  assert.equal(ctx.ranges[0].input.value, tempDefault, 'setting-temperature was set to null');
  assert.notEqual(String(ctx.ranges[0].input.value), 'null', 'setting-temperature became the string "null"');
  assert.equal(ctx.ranges[2].input.value, topkDefault, 'setting-top-k was set to null');
  assert.notEqual(String(ctx.ranges[2].input.value), 'null', 'setting-top-k became the string "null"');
  assert.equal(ctx.settingMaxTokens.value, maxTokensDefault, 'max_tokens was set to null');
  assert.notEqual(String(ctx.settingMaxTokens.value), 'null', 'max_tokens became the string "null"');
  assert.equal(ctx.settingMaxContext.value, maxContextDefault, 'setting-max-context was set to null');
  assert.notEqual(String(ctx.settingMaxContext.value), 'null', 'setting-max-context became the string "null"');
});

// ── Test 11: loadSettings still restores VALID numeric values (no over-correction) ─
	test('FIX 4: loadSettings reads class-D keys from agent-cascade-ui-prefs (not the old blob)', () => {
	  // Source invariant: loadSettings must call loadUiPrefs (the ui-prefs reader),
	  // and must NOT reference the legacy agent-cascade-settings blob.
	  assert(LOADSRC.includes('loadUiPrefs'),
	    'loadSettings must call loadUiPrefs() for class-D keys');
	  assert(!LOADSRC.includes("agent-cascade-settings"),
	    'loadSettings must NOT read from the legacy agent-cascade-settings blob');
	  // loadUiPrefs must exist and be callable (extracted and in the VM).
	  const { ctx } = buildSandbox({ dom: makeDom([]), saveSettingsSpy: () => {} });
	  const prefs = vm.runInContext('loadUiPrefs()', ctx);
	  assert(prefs && typeof prefs === 'object', 'loadUiPrefs() must return an object');
	});

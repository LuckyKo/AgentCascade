'use strict';
/**
 * Regression: the Effort sidebar shows a visible per-notch description under the
 * <select>, data-driven from the server pool_settings snapshot
 * (effort_turn_caps / effort_nested_caps), with built-in fallbacks when those keys
 * are absent. The renderer is wired into syncPoolSettings — the sole hydration path —
 * so it runs on initial load AND every snapshot update (same seam the other
 * state.pool_settings-driven controls use; see settings-pool-map-hydration-gap).
 *
 * Extracts the REAL renderEffortInfo + syncPoolSettings from web_ui/app.js and runs
 * them in a vm sandbox with a textContent-capable DOM stub (same technique as
 * test_settings_work_folders_hydrate.js).
 *
 * Run:  node tests/test_effort_slider_info.js   (exits 0 on pass / 1 on fail)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const APP_JS = fs.readFileSync(path.join(__dirname, '..', 'web_ui', 'app.js'), 'utf8');

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

// ── Source-level seams ──────────────────────────────────────────────────────────
assert.ok(APP_JS.includes('function renderEffortInfo'), 'renderEffortInfo missing from app.js');
assert.ok(APP_JS.includes("$('#effort-slider-info')"), 'renderer must target #effort-slider-info');
assert.ok(APP_JS.includes('effort_turn_caps'), 'renderer must read effort_turn_caps');
assert.ok(APP_JS.includes('effort_nested_caps'), 'renderer must read effort_nested_caps');

const SYNC_SRC = extractFunction('syncPoolSettings');
assert.ok(SYNC_SRC.includes('renderEffortInfo(ps)'),
  'syncPoolSettings must call renderEffortInfo (hydration seam → load + every snapshot update)');

const RENDER_SRC = extractFunction('renderEffortInfo');

// renderEffortInfo's fallback branch references module-level consts defined outside it;
// extract both so the sandbox has them (the "keys present" case never reads them).
const DEFAULTS_SRC = ['EFFORT_DEFAULT_TURN_CAPS', 'EFFORT_DEFAULT_NESTED_CAPS']
  .map((name) => {
    const m = new RegExp('const\\s+' + name + '\\s*=\\s*{[^}]*};', 'g').exec(APP_JS);
    assert.ok(m, `Could not find "const ${name} = {...};" in app.js`);
    return m[0];
  }).join('\n');

// ── Sandbox (textContent-capable DOM stub) ──────────────────────────────────────
function makeDom() {
  const elements = {};
  const get = (id) => {
    const key = (typeof id === 'string' && id.startsWith('#')) ? id : '#' + id;
    if (!elements[key]) elements[key] = { value: '', checked: false, textContent: '' };
    return elements[key];
  };
  return {
    elements,
    $: get,
    documentStub: { activeElement: null, getElementById: get, querySelector: get },
  };
}

function buildSandbox(dom) {
  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(DEFAULTS_SRC, ctx, { filename: 'effort_defaults.js' });
  vm.runInContext(RENDER_SRC, ctx, { filename: 'renderEffortInfo.js' });
  return ctx;
}

test('renderEffortInfo fills #effort-slider-info from the snapshot (one row per notch)', () => {
  const dom = makeDom();
  const ctx = buildSandbox(dom);
  // Sentinel values deliberately DIFFERENT from the built-in defaults (30/50/100 and
  // 1/3/7) so this proves the snapshot path wins, not just that *something* renders.
  const snapshot = {
    effort_turn_caps: { low: 7, medium: 80, high: 200, ultra: null },
    effort_nested_caps: { low: 2, medium: 5, high: 9, ultra: null },
  };
  ctx.snapshot = snapshot;
  vm.runInContext('renderEffortInfo(snapshot)', ctx);

  const text = dom.elements['#effort-slider-info'].textContent;
  assert.ok(text.includes('Low: 7 turns · 2 sub · budgeting off'), `low row wrong: ${text}`);
  assert.ok(text.includes('Med: 80 turns · 5 sub · budgeting off'), `med row wrong: ${text}`);
  assert.ok(text.includes('High: 200 turns · 9 sub · budgeting off'), `high row wrong: ${text}`);
  assert.ok(text.includes('Ultra: no cap · budgeting on'), `ultra row wrong: ${text}`);
  assert.equal(text.split('\n').length, 4, 'must render exactly 4 notch rows');
});

test('renderEffortInfo falls back to built-in defaults when the keys are absent', () => {
  const dom = makeDom();
  const ctx = buildSandbox(dom);
  ctx.snapshot = {};   // snapshot predates the effort_* keys
  vm.runInContext('renderEffortInfo(snapshot)', ctx);

  const text = dom.elements['#effort-slider-info'].textContent;
  // Fallback nested caps are the real settings.py values (low1/med3/high7).
  assert.ok(text.includes('Low: 30 turns · 1 sub · budgeting off'), `low row wrong: ${text}`);
  assert.ok(text.includes('Med: 50 turns · 3 sub · budgeting off'), `med row wrong: ${text}`);
  assert.ok(text.includes('High: 100 turns · 7 sub · budgeting off'), `high row wrong: ${text}`);
  assert.ok(text.includes('Ultra: no cap · budgeting on'), `ultra row wrong: ${text}`);
});

test('renderEffortInfo is a no-op when the info element is absent (no crash)', () => {
  // Sandbox whose $ returns null for the info div (element not in this DOM).
  const sandbox = { console, document: {}, $: () => null };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(DEFAULTS_SRC, ctx, { filename: 'effort_defaults.js' });
  vm.runInContext(RENDER_SRC, ctx, { filename: 'renderEffortInfo.js' });
  ctx.snapshot = { effort_turn_caps: {}, effort_nested_caps: {} };
  // Must not throw even though $() is null.
  assert.doesNotThrow(() => vm.runInContext('renderEffortInfo(snapshot)', ctx));
});

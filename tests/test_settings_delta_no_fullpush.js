'use strict';
/**
 * Batch B (Phase 2/3): settings are sent to the server as per-key deltas via `update_settings`,
 * never as the legacy full-push `update_config`.
 *
 * `queueSettingChange` is the SINGLE write path (MAJOR-2): saveSettings() routes its
 * getGenerateCfg() delta through it, and it coalesces into one debounced `update_settings`
 * send. This test proves:
 *   1. Changing ONE control queues ONE key; flushing sends exactly one `update_settings`
 *      message whose `changes` map contains only that key (not a full snapshot).
 *   2. A no-op change (value equals the server value) is not queued.
 *   3. No `update_config` message is ever produced by the delta path (source invariant).
 *   4. saveSettings routes its delta through queueSettingChange — the only `update_settings`
 *      send lives in _flushSettingChanges (single write path, no direct send elsewhere).
 *
 * It extracts the REAL queueSettingChange + _flushSettingChanges from web_ui/app.js and runs
 * them in a sandbox with a captured `send` spy.
 *
 * Run:  node tests/test_settings_delta_no_fullpush.js   (exits 0 on pass / 1 on fail)
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

const QUEUE_SRC = extractFunction('queueSettingChange');
const FLUSH_SRC = extractFunction('_flushSettingChanges');

function buildSandbox() {
  const sent = [];
  const sandbox = {
    console,
    state: { connected: true },
    // Module-scoped state that the extracted functions read/write (seeded as context globals).
    pendingSettings: { grep_char_limit: 100 }, // prior server baseline
    settingsRevision: 7,
    _pendingSettingChanges: {},
    _settingFlushTimer: null,
    send(msg) { sent.push(msg); },
    // Capture the debounce callback instead of scheduling a real timer.
    setTimeout(cb) { sandbox._flushCb = cb; return 0; },
    clearTimeout() {},
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(QUEUE_SRC, ctx, { filename: 'queueSettingChange.js' });
  vm.runInContext(FLUSH_SRC, ctx, { filename: '_flushSettingChanges.js' });
  return { ctx, sent };
}

test('changing one control sends exactly one key via update_settings (no full push)', () => {
  const { ctx, sent } = buildSandbox();

  // The user changes grep_char_limit to 150 (server baseline was 100 → a real change).
  vm.runInContext('queueSettingChange(key, value)',
    Object.assign(ctx, { key: 'grep_char_limit', value: 150 }));

  // Flush the debounced delta synchronously.
  vm.runInContext('_flushSettingChanges()', ctx);

  // Exactly one message, of type update_settings, carrying ONLY the changed key.
  // (The `changes` object is created inside the vm context, so compare keys/values directly
  // rather than deepStrictEqual, which would trip on the cross-realm Object prototype.)
  assert.equal(sent.length, 1, `expected 1 send, got ${sent.length}`);
  assert.equal(sent[0].type, 'update_settings');
  assert.equal(sent[0].base_revision, 7, 'delta must carry the last known settings revision');
  assert.deepEqual(Object.keys(sent[0].changes), ['grep_char_limit'],
    'delta must contain only the changed key, not a full snapshot');
  assert.equal(sent[0].changes.grep_char_limit, 150);
  assert.ok(!('update_config' in sent[0]), 'delta path must not emit update_config');
});

test('a no-op change (value equals server) is not queued', () => {
  const { ctx, sent } = buildSandbox();
  // grep_char_limit server value is already 100 → queuing 100 is a no-op.
  vm.runInContext('queueSettingChange(key, value)',
    Object.assign(ctx, { key: 'grep_char_limit', value: 100 }));
  vm.runInContext('_flushSettingChanges()', ctx);
  assert.equal(sent.length, 0, 'no-op change must not produce a send');
});

test('source invariant: no update_config send remains in app.js', () => {
  const re = /type:\s*['"]update_config['"]/g;
  const hits = APP_JS.match(re) || [];
  assert.equal(hits.length, 0,
    `expected zero update_config sends, found ${hits.length} (full-pushes must be deltas)`);
});

test('saveSettings routes its delta through queueSettingChange (single write path)', () => {
  // The saveSettings delta block must delegate to queueSettingChange (not send directly).
  assert.ok(
    /for\s*\(\s*const\s+k\s+of\s+Object\.keys\(cfg\)\s*\)\s*queueSettingChange\(k,\s*cfg\[k\]\)/.test(APP_JS),
    'saveSettings must route its getGenerateCfg() delta through queueSettingChange');
  // Exactly ONE update_settings send in the whole file, and it lives in _flushSettingChanges.
  const us = APP_JS.match(/type:\s*['"]update_settings['"]/g) || [];
  assert.equal(us.length, 1,
    `expected exactly one update_settings send (in _flushSettingChanges), found ${us.length}`);
});

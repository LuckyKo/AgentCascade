'use strict';
/**
 * Batch B (Phase 2/3): when the server rejects a per-key setting change, `revertControl`
 * must reset the control to the AUTHORITATIVE server value — NOT the rejected value the user
 * just typed.
 *
 * Regression this guards (MAJOR-1): `pendingSettings` is optimistically merged with the
 * pending value before the server responds, so reverting from it would reset the control to
 * the same rejected value. The fix keeps a separate authoritative snapshot
 * (`serverSettingsSnapshot`, updated only from state frames / settings_changed) and reverts
 * from THAT.
 *
 * It extracts the REAL queueSettingChange + _flushSettingChanges + revertControl from
 * web_ui/app.js and runs them in a sandbox with a DOM stub + captured `send` spy.
 *
 * Run:  node tests/test_settings_reject_revert.js   (exits 0 on pass / 1 on fail)
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
const REVERT_SRC = extractFunction('revertControl');

function buildSandbox(serverValue) {
  const el = { id: '#setting-max-rollbacks', value: String(serverValue), checked: false };
  const sent = [];
  const sandbox = {
    console,
    state: { connected: true },
    // Optimistic working copy (diffs) + the authoritative snapshot (revert source).
    pendingSettings: { max_auto_rollbacks: serverValue },
    serverSettingsSnapshot: { max_auto_rollbacks: serverValue },
    settingsRevision: 7,
    _pendingSettingChanges: {},
    _settingFlushTimer: null,
    POOL_SETTINGS_MAP: [{ id: '#setting-max-rollbacks', prop: 'value', key: 'max_auto_rollbacks' }],
    $(id) { return id === '#setting-max-rollbacks' ? el : null; },
    showInSystemToastBar() {},
    send(msg) { sent.push(msg); },
    setTimeout(cb) { sandbox._flushCb = cb; return 0; },
    clearTimeout() {},
    _el: el,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(QUEUE_SRC, ctx, { filename: 'queueSettingChange.js' });
  vm.runInContext(FLUSH_SRC, ctx, { filename: '_flushSettingChanges.js' });
  vm.runInContext(REVERT_SRC, ctx, { filename: 'revertControl.js' });
  return { ctx, sent, el };
}

test('a rejected change reverts the control to the SERVER value, not the rejected value', () => {
  const { ctx, sent, el } = buildSandbox(3); // server value = 3

  // 1. User types 5 (which the server will reject). Queue + flush the delta.
  vm.runInContext('queueSettingChange(key, value)', Object.assign(ctx, { key: 'max_auto_rollbacks', value: 5 }));
  vm.runInContext('_flushSettingChanges()', ctx);
  assert.equal(sent.length, 1, 'the change must be sent once');
  assert.equal(sent[0].changes.max_auto_rollbacks, 5);
  // The DOM now shows the user's (pending) value.
  el.value = '5';

  // 2. Server rejects max_auto_rollbacks → revertControl must restore the server value (3).
  vm.runInContext('revertControl(key)', Object.assign(ctx, { key: 'max_auto_rollbacks' }));

  assert.equal(el.value, '3',
    `revertControl must reset to the server value (3), not the rejected value (5); got ${el.value}`);
});

test('revertControl is a no-op when there is no authoritative snapshot', () => {
  const { ctx, el } = buildSandbox(3);
  // Wipe the authoritative snapshot; a reject with no server baseline must not stomp the control.
  vm.runInContext('serverSettingsSnapshot = null', ctx);
  el.value = '9';
  vm.runInContext('revertControl(key)', Object.assign(ctx, { key: 'max_auto_rollbacks' }));
  assert.equal(el.value, '9', 'no snapshot → revertControl leaves the control untouched');
});

function extractCase(caseName) {
  const idx = APP_JS.indexOf(`case '${caseName}':`);
  assert.ok(idx >= 0, `Could not find case '${caseName}' in app.js`);
  const bodyStart = APP_JS.indexOf('{', idx);
  let depth = 0;
  for (let j = bodyStart; j < APP_JS.length; j++) {
    const c = APP_JS[j];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return APP_JS.slice(idx, j + 1);
    }
  }
  throw new Error(`Unbalanced braces for case ${caseName}`);
}

const APPLIED_SRC = extractCase('settings_applied');

test('a rejected key rolls the optimistic baseline back to the authoritative snapshot', () => {
  const { ctx } = buildSandbox(3);

  // Optimistic merge happens at flush time: the baseline now holds the not-yet-accepted value.
  vm.runInContext('queueSettingChange(key, value); _flushSettingChanges()',
    Object.assign(ctx, { key: 'max_auto_rollbacks', value: 5 }));
  assert.equal(ctx.pendingSettings.max_auto_rollbacks, 5,
    'pendingSettings is the optimistic baseline before the server replies');

  // The server replies with settings_applied (broadcast to every client) rejecting the key.
  ctx.data = { type: 'settings_applied', revision: 8, applied: [],
               rejected: { max_auto_rollbacks: 'value out of range' } };
  vm.runInContext(`switch (data.type) { ${APPLIED_SRC} }`, ctx);

  assert.equal(ctx.pendingSettings.max_auto_rollbacks, ctx.serverSettingsSnapshot.max_auto_rollbacks,
    'rejected key must be rolled back to the authoritative snapshot, else the diff baseline drifts');
  assert.ok(!(ctx._pendingSettingChanges && 'max_auto_rollbacks' in ctx._pendingSettingChanges),
    'rejected key must not stay queued for a later flush');
});

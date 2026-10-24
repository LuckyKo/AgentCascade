'use strict';
/**
 * Regression test for BUG_0142 — client-side healing of a stuck `state.generating`.
 *
 * Background: `state.generating` is a one-way latch in web_ui/app.js. Every
 * `stream_update` sets it true; only a full `state`/`done` frame (or `error`) lowers it.
 * If the closing `done` frame is lost, the UI stayed on "Generating..." until a page
 * refresh, blocking new input (messages got injected into the "active" run) and
 * disabling Continue/Retry.
 *
 * The server now pushes a tiny `status` heartbeat during quiet periods
 * (api_server._idle_status_loop). This test extracts the REAL `applyServerStatus`
 * handler AND the shared turn-end helpers it delegates to (`finalizeGeneration`,
 * `renderAndResetThrottle`) from web_ui/app.js (so it always tests current source, not a
 * copy) and runs them in a sandbox, asserting the healing rules:
 *   1. Stale flag + server idle + no active instance  → heal.
 *   2. Server idle BUT an instance is still active    → do NOT heal (never kill a live run).
 *   3. Status frame without an instance map           → do NOT heal (incomplete evidence).
 *   4. Instance flags are applied even when generating stays true.
 *   5. Already-consistent frame                       → no DOM/render work.
 *   6. generating:true frame on an idle UI            → never RAISE the flag (still applies
 *      per-instance flags).
 *
 * Run:  node tests/test_generating_flag_heal.js   (exits 0 on pass / 1 on fail)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

// ── Extract the real source fragment from app.js ──────────────────────────────
const APP_JS = fs.readFileSync(
  path.join(__dirname, '..', 'web_ui', 'app.js'),
  'utf8'
);

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(', 'g');
  const m = re.exec(APP_JS);
  assert.ok(m, `Could not find "function ${name}(" in app.js`);
  const open = APP_JS.indexOf('{', m.index);
  assert.ok(open >= 0, `No body for ${name}`);
  let depth = 0;
  for (let j = open; j < APP_JS.length; j++) {
    const c = APP_JS[j];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return APP_JS.slice(m.index, j + 1); }
  }
  throw new Error(`Unbalanced braces for ${name}`);
}

const HANDLER_SRC = extractFunction('applyServerStatus');
// applyServerStatus delegates turn-end work to these shared helpers (BUG_0142); load the
// real source so the test exercises the actual integration, not a reimplementation.
const FINALIZE_SRC = extractFunction('finalizeGeneration');
const RENDER_SRC = extractFunction('renderAndResetThrottle');

// ── Sandbox ───────────────────────────────────────────────────────────────────
function buildSandbox({ generating = true, subAgents = {} } = {}) {
  const calls = { updateGenStats: 0, renderSubAgents: 0, updateControls: 0, fetchTelemetry: 0 };
  const warnings = [];
  const sandbox = {
    console: {
      warn: (...args) => warnings.push(args.join(' ')),
      log: () => {},
      error: () => {},
      debug: () => {},
    },
    state: {
      generating,
      subAgents,
      activeStack: [],
      genStats: {},
      _lastActivityPreviewKey: 'stale-preview-key',
      _lastActivityPreview: { stale: true },
    },
    getActiveAgentName: () => 'root',
    updateGenStats: () => { calls.updateGenStats++; },
    renderSubAgents: () => { calls.renderSubAgents++; },
    updateControls: () => { calls.updateControls++; },
    fetchTelemetry: () => { calls.fetchTelemetry++; },
    performance: { now: () => 12345 },
    calls,
    warnings,
  };
  const ctx = vm.createContext(sandbox);
  // Define the shared helpers first (function declarations), then the handler that uses them.
  vm.runInContext(FINALIZE_SRC, ctx, { filename: 'finalizeGeneration.js' });
  vm.runInContext(RENDER_SRC, ctx, { filename: 'renderAndResetThrottle.js' });
  vm.runInContext(HANDLER_SRC, ctx, { filename: 'applyServerStatus.js' });

  function apply(frame) {
    ctx.__frame = frame;
    vm.runInContext('applyServerStatus(__frame)', ctx);
  }
  return { ctx, calls, warnings, apply, state: ctx.state };
}

const activeRoot = () => ({ active: true, agent_state: 'RUNNING', is_halted: false });

// ── Test 1: the bug — stale flag + idle server → healed ──────────────────────
test('stale generating flag is cleared when the server says idle and nothing is active', () => {
  const { state, calls, warnings, apply } = buildSandbox({ subAgents: { root: activeRoot() } });
  assert.equal(state.generating, true, 'precondition: UI thinks it is generating');

  apply({ type: 'status', generating: false, instances: { root: { active: false, agent_state: 'IDLE', is_halted: false } } });

  assert.equal(state.generating, false, 'stale flag was NOT cleared — UI stays stuck on "Generating..."');
  assert.equal(state.subAgents.root.active, false, 'per-instance active flag was not repaired');
  assert.equal(state.subAgents.root.agent_state, 'IDLE', 'agent_state was not repaired');
  assert.equal(calls.updateGenStats, 1, 'final gen-stats pass missing');
  assert.equal(calls.renderSubAgents, 1, 'UI was not re-rendered after healing');
  assert.equal(calls.updateControls, 1, 'controls (send/continue/status line) were not refreshed');
  assert.equal(calls.fetchTelemetry, 1, 'turn-end telemetry refresh missing');
  assert.equal(state.genStats.active, false);
  assert.equal(state._lastActivityPreviewKey, undefined, 'activity preview cache not invalidated');
  assert.ok(warnings.some(w => w.includes('clearing stale flag')), 'heal was not logged');
});

// ── Test 2: never kill a live run ────────────────────────────────────────────
test('generating flag is NOT cleared while any instance is still active', () => {
  const subAgents = {
    root: activeRoot(),
    worker: { active: false, agent_state: 'IDLE', is_halted: false },
  };
  const { state, calls, apply } = buildSandbox({ subAgents });

  // Server session flag can lag behind an in-flight sub-agent run — the instance map wins.
  apply({ type: 'status', generating: false, instances: {
    root: { active: true, agent_state: 'RUNNING', is_halted: false },
    worker: { active: false, agent_state: 'IDLE', is_halted: false },
  } });

  assert.equal(state.generating, true, 'a live instance was wrongly declared idle');
  assert.equal(calls.updateGenStats, 0, 'heal bookkeeping ran despite a live instance');
  assert.equal(calls.renderSubAgents, 0, 'unnecessary re-render on an unchanged frame');
});

// ── Test 3: incomplete evidence → no heal ────────────────────────────────────
test('generating flag is NOT cleared when the frame has no instance map', () => {
  const { state, calls, apply } = buildSandbox({ subAgents: { root: activeRoot() } });

  apply({ type: 'status', generating: false });

  assert.equal(state.generating, true, 'healed on incomplete evidence (missing instances)');
  assert.equal(calls.updateControls, 0);
});

// ── Test 4: instance flags are authoritative even while generating ───────────
test('per-instance flags are applied even when generating stays true', () => {
  const subAgents = { worker: { active: true, agent_state: 'RUNNING', is_halted: false } };
  const { state, calls, apply } = buildSandbox({ subAgents });

  apply({ type: 'status', generating: true, instances: {
    worker: { active: false, agent_state: 'SLEEPING', is_halted: true },
  } });

  assert.equal(state.generating, true, 'generating must never be raised/lowered on a generating=true frame');
  assert.equal(state.subAgents.worker.active, false, 'inactive worker still pulsing');
  assert.equal(state.subAgents.worker.agent_state, 'SLEEPING');
  assert.equal(state.subAgents.worker.is_halted, true);
  assert.equal(calls.updateControls, 1, 'controls not refreshed after instance flag change');
  assert.equal(calls.renderSubAgents, 1, 'tabs not re-rendered after instance flag change');
});

// ── Test 5: idempotent no-op ────────────────────────────────────────────────
test('a consistent status frame does no DOM work', () => {
  const subAgents = { root: { active: false, agent_state: 'IDLE', is_halted: false } };
  const { state, calls, apply } = buildSandbox({ generating: false, subAgents });

  apply({ type: 'status', generating: false, instances: {
    root: { active: false, agent_state: 'IDLE', is_halted: false },
  } });

  assert.equal(state.generating, false);
  assert.equal(calls.updateGenStats, 0);
  assert.equal(calls.renderSubAgents, 0);
  assert.equal(calls.updateControls, 0);
});

// ── Test 6: never RAISE a false flag (criterion 2, upper half) ──────────────
test('a status frame with generating:true never raises an idle client flag', () => {
  // The UI is idle (e.g. it just healed, or a run never started). A heartbeat that still
  // reports the session as generating must NOT flip the idle flag back to true — only a
  // stream_update may raise it. The per-instance flags are still applied.
  const subAgents = { root: { active: false, agent_state: 'IDLE', is_halted: false } };
  const { state, calls, apply } = buildSandbox({ generating: false, subAgents });

  apply({ type: 'status', generating: true, instances: {
    root: { active: true, agent_state: 'RUNNING', is_halted: false },
  } });

  assert.equal(state.generating, false, 'a status frame wrongly raised the generating flag');
  assert.equal(calls.updateGenStats, 0, 'turn-end bookkeeping ran on a non-heal frame');
  assert.equal(state.subAgents.root.active, true, 'per-instance active flag was not applied');
});

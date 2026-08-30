'use strict';
/**
 * Regression test for the "pause stops streaming" bug (todo.md line 140).
 *
 * Root cause: the UI conflated two distinct concepts into one boolean. The global
 * `pause` (a tool-boundary hold) was folded into the per-instance `is_halted` flag, and
 * the `stream_update` handler breaks (discards ticks) whenever the ACTIVE agent reports
 * is_halted. So clicking Pause made the active agent report is_halted=true and froze the
 * visible token stream — even though the backend keeps streaming while paused.
 *
 * The fix decouples them:
 *   - `is_halted` (per-instance) = genuine halt ONLY (compression/manual stop).
 *   - `paused`  (global)         = the tool-boundary hold.
 * and `createPauseButton` no longer mutates per-agent is_halted locally.
 *
 * This test extracts the REAL stream-gate line and createPauseButton from web_ui/app.js
 * (so it always tests current source, not a copy) and runs them in a sandbox with a
 * minimal DOM stub. It does NOT load the whole app.js (browser-global, would throw).
 *
 * Run:  node tests/test_pause_streaming.js   (exits 0 on pass / 1 on fail)
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

// The single stream gate line inside the 'stream_update' case. It is the exact predicate
// that decides whether a tick is discarded: `if (<PREDICATE>) break;`. We extract just the
// predicate so it can be evaluated against controlled state in isolation.
function extractStreamGatePredicate() {
  const idx = APP_JS.indexOf("case 'stream_update':");
  assert.ok(idx >= 0, "Could not find `case 'stream_update':` in app.js");
  // The gate is the first `if (...) break;` after the case label.
  const re = /if\s*\(([^)]*)\)\s*break\s*;/;
  const m = re.exec(APP_JS.slice(idx, idx + 600));
  assert.ok(m, "Could not find the stream gate `if (...) break;` in the stream_update case");
  return m[1]; // e.g. `state.subAgents[activeName]?.is_halted`
}

const GATE_PREDICATE = extractStreamGatePredicate();
// The gate references a local `activeName` (from getActiveAgentName()); bind it when we
// evaluate the predicate in isolation so it resolves to our controlled active agent.
const GATE_EXPR = `const activeName = 'w1'; (${GATE_PREDICATE})`;
const PAUSE_BTN_SRC = extractFunction('createPauseButton');

// ── Minimal DOM / browser stubs ───────────────────────────────────────────────
function makeDom() {
  const elements = {};
  const querySelector = (sel) => {
    if (typeof sel !== 'string') return null;
    const key = sel.startsWith('#') ? sel : '#' + sel;
    return elements[key] || null;
  };
  const documentStub = { activeElement: null, getElementById: () => null, querySelector };
  return { elements, documentStub, $: querySelector };
}

function buildSandbox() {
  const dom = makeDom();
  const sent = [];
  // A controllable button stub so createPauseButton can be exercised.
  const btn = { textContent: '⏸ Pause', listeners: {}, addEventListener(ev, fn) { this.listeners[ev] = fn; } };
  const sandbox = {
    console,
    document: dom.documentStub,
    $: dom.$,
    state: { subAgents: {}, paused: false },
    getActiveAgentName: () => 'w1',
    getActiveInstanceName: () => 'w1',
    send(msg) { sent.push(msg); },
    // Expose the recorder as a global so it can be read back via JSON.stringify(sent) inside
    // the vm context (host-side arrays are not visible to sandbox code by default).
    sent,
    btn,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(PAUSE_BTN_SRC, ctx, { filename: 'createPauseButton.js' });
  return { sandbox, ctx, sent, btn };
}

// ── Test 1: global pause does NOT break the stream gate (the actual bug) ──────
test('stream_update gate does NOT break when paused but agent is not genuinely halted', () => {
  const { ctx } = buildSandbox();
  // Global pause active, but the active agent has NO genuine halt.
  vm.runInContext(
    'state.paused = true; state.subAgents.w1 = { is_halted: false, active: true };',
    ctx
  );
  const shouldBreak = vm.runInContext(GATE_EXPR, ctx);
  assert.equal(shouldBreak, false,
    `stream gate broke while merely paused (is_halted=false) — this is the freeze bug. predicate="${GATE_PREDICATE}"`);
});

// ── Test 2: a genuine per-instance halt STILL breaks the stream gate (invariant) ─
test('stream_update gate DOES break when the active agent is genuinely halted', () => {
  const { ctx } = buildSandbox();
  // Genuine per-instance halt (compression/stop), regardless of pause state.
  vm.runInContext(
    'state.paused = false; state.subAgents.w1 = { is_halted: true, active: true };',
    ctx
  );
  const shouldBreak = vm.runInContext(GATE_EXPR, ctx);
  assert.equal(shouldBreak, true,
    `stream gate did NOT break on a genuine halt — the invariant (halt freezes stream) is broken. predicate="${GATE_PREDICATE}"`);
});

// ── Test 3: createPauseButton no longer mutates per-agent is_halted locally ───
test('createPauseButton sends pause/resume and does NOT mutate subAgents[].is_halted', () => {
  const { ctx, sent, btn } = buildSandbox();
  // Seed an active agent that must remain untouched by the button click.
  vm.runInContext(
    'state.subAgents.w1 = { is_halted: false, active: true }; state.paused = false;',
    ctx
  );

  // Wire the real factory onto our stub button (mirrors app.js: `if (pauseBtn) createPauseButton(pauseBtn, ...)`).
  vm.runInContext('createPauseButton(btn, () => getActiveInstanceName())', ctx);

  // Read the recorded sends from INSIDE the sandbox as JSON — avoids cross-realm object
  // identity issues when comparing vm-created objects against host literals.
  const sentJson = () => vm.runInContext('JSON.stringify(sent)', ctx);

  // Click while showing "Pause" → should send {type:'pause'} and flip label to Resume.
  btn.listeners.click();
  assert.equal(sentJson(), JSON.stringify([{ type: 'pause' }]), 'pause click must send exactly {type:"pause"}');
  assert.equal(btn.textContent, '▶️ Resume', 'button label must flip to Resume on pause');
  let halted = vm.runInContext('state.subAgents.w1.is_halted', ctx);
  assert.equal(halted, false, 'createPauseButton mutated is_halted=true on pause click (regression)');

  // Click while showing "Resume" → should send {type:'resume_all'} and flip label back.
  btn.listeners.click();
  assert.equal(sentJson(), JSON.stringify([{ type: 'pause' }, { type: 'resume_all' }]), 'resume click must send {type:"resume_all"}');
  assert.equal(btn.textContent, '⏸ Pause', 'button label must flip back to Pause on resume');
  halted = vm.runInContext('state.subAgents.w1.is_halted', ctx);
  assert.equal(halted, false, 'createPauseButton mutated is_halted on resume click (regression)');
});

// ── Test 4: stream_update wires data.paused → state.paused (review round-2 fix) ──
// The backend sends `paused` on EVERY stream_update payload (state_builder.py), so the
// handler must keep state.paused fresh there too — not only in the full-state handler.
// Otherwise a pause during a live stream would leave the button/status stale for ~100 ticks.
function extractStreamUpdatePausedWiring() {
  const idx = APP_JS.indexOf("case 'stream_update':");
  assert.ok(idx >= 0, "Could not find `case 'stream_update':` in app.js");
  // The wiring is the first `if (data.paused !== undefined) state.paused = data.paused;`
  // that appears inside the stream_update case (before the next top-level `case`).
  const nextCase = APP_JS.indexOf('case ', idx + 20);
  const region = APP_JS.slice(idx, nextCase > idx ? nextCase : idx + 4000);
  const m = /if\s*\(\s*data\.paused\s*!==\s*undefined\s*\)\s*state\.paused\s*=\s*data\.paused\s*;/.exec(region);
  assert.ok(m, "stream_update case does not wire data.paused → state.paused");
  return m[0];
}

test('stream_update handler wires data.paused into state.paused (keeps pause UI fresh)', () => {
  const WIRING = extractStreamUpdatePausedWiring(); // throws if the line is missing
  const sandbox = { state: { paused: false }, data: {} };
  const ctx = vm.createContext(sandbox);

  // Backend sends a stream_update payload with paused=true (user paused mid-stream).
  vm.runInContext('data.paused = true;', ctx);
  vm.runInContext(WIRING, ctx);
  assert.equal(vm.runInContext('state.paused', ctx), true,
    'stream_update did not propagate data.paused=true into state.paused');

  // A later payload with paused=false (resumed) must clear it.
  vm.runInContext('data.paused = false;', ctx);
  vm.runInContext(WIRING, ctx);
  assert.equal(vm.runInContext('state.paused', ctx), false,
    'stream_update did not propagate data.paused=false into state.paused');

  // A payload without the field must leave state.paused untouched (undefined guard).
  vm.runInContext('data.paused = undefined;', ctx);
  vm.runInContext(WIRING, ctx);
  assert.equal(vm.runInContext('state.paused', ctx), false,
    'stream_update overwrote state.paused when data.paused was undefined');
});

'use strict';
/**
 * Regression test for the "New Session / Refresh Soul stay locked while the active session is
 * HELD (paused or per-instance-halted) — so the user can't start a new session or refresh the
 * soul without a page reload" part of the halted/paused-generating Web-UI bug.
 *
 * `state.generating` is a one-way latch that pause does NOT lower (pause_fix). New Session and
 * Refresh Soul were gated purely on `state.generating`, so a paused/halted-but-still-"generating"
 * agent left them disabled. The fix unlocks them while the session is HELD, BUT New Session
 * (`stacks_new`) parks the active stack and force-halts a running fleet's in-flight turns
 * (snapshot.park_active_stack), so the unlock is gated additionally on "no instance is still
 * actively streaming" — see .agent_lessons/stacks-new-park-active-stack-force-halts-inflight.
 *
 * This extracts the REAL `updateControls` from web_ui/app.js (so it always tests current source,
 * never a copy) and runs it in a sandbox with inert DOM stubs, asserting the two button flags for
 * held vs. running cases.
 *
 * Run:  node tests/test_updatecontrols_held_buttons.js   (exits 0 on pass / 1 on fail)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { test } = require('node:test');
const assert = require('node:assert/strict');

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

const UPDATE_CONTROLS_SRC = extractFunction('updateControls');

// ── Sandbox: inert DOM stubs, with the two buttons under test captured as stable refs ──────
function mkEl() {
  return {
    classList: { add() {}, remove() {} },
    style: {},
    dataset: {},
    title: '',
    textContent: '',
    placeholder: '',
    value: '',
    disabled: false,
    querySelector: () => null,
  };
}

function runUpdateControls(state) {
  const refreshBtn = mkEl();
  const resetBtn = mkEl();          // New Session (module global)
  const mainRB = mkEl();
  const terminateBtn = mkEl();
  const byId = { refreshBtn, mainRetryBtn: mainRB, terminateBtn };

  const sandbox = {
    state,
    sendBtn: mkEl(),
    resetBtn,                        // the New Session button
    continueBtn: mkEl(),
    statusText: mkEl(),
    chatInput: mkEl(),
    sessionNameInput: mkEl(),
    stopBtn: null,                   // guarded `if (stopBtn)` → inert
    pauseBtn: null,                  // guarded → inert
    saveKvBtn: null,                 // guarded → inert
    loadKvBtn: null,                 // guarded → inert
    mainTabBar: { querySelector: () => null },   // skip the tab-icon block
    document: {
      body: { classList: { add() {}, remove() {} } },
      activeElement: null,
      getElementById: (id) => (id in byId ? byId[id] : null),
    },
    getActiveAgentName: () => 'root',
    getAgentTabId: (n) => n,
    getActiveInstanceName: () => 'root',
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(UPDATE_CONTROLS_SRC, ctx, { filename: 'updateControls.js' });
  vm.runInContext('updateControls()', ctx);
  return { refreshBtn, resetBtn };
}

// Session is generating; active agent is HELD (per-instance halt) and no instance is streaming.
test('held (halted) active agent unlocks New Session + Refresh Soul while generating', () => {
  const { refreshBtn, resetBtn } = runUpdateControls({
    generating: true, connected: true, paused: false, sessionName: 's',
    subAgents: { root: { active: false, is_halted: true, agent_state: 'RUNNING' } },
  });
  assert.equal(refreshBtn.disabled, false, 'Refresh Soul must unlock for a held (halted) agent');
  assert.equal(resetBtn.disabled, false, 'New Session must unlock for a held (halted) agent');
});

// Session is generating; active agent is globally PAUSED (is_halted stays false) and not streaming.
test('global pause unlocks New Session + Refresh Soul while generating', () => {
  const { refreshBtn, resetBtn } = runUpdateControls({
    generating: true, connected: true, paused: true, sessionName: 's',
    subAgents: { root: { active: false, is_halted: false, agent_state: 'RUNNING' } },
  });
  assert.equal(refreshBtn.disabled, false, 'Refresh Soul must unlock while globally paused');
  assert.equal(resetBtn.disabled, false, 'New Session must unlock while globally paused');
});

// Session is generating; active agent is genuinely RUNNING (not held) → buttons stay locked.
test('a running (not held) agent keeps New Session + Refresh Soul locked while generating', () => {
  const { refreshBtn, resetBtn } = runUpdateControls({
    generating: true, connected: true, paused: false, sessionName: 's',
    subAgents: { root: { active: true, is_halted: false, agent_state: 'RUNNING' } },
  });
  assert.equal(refreshBtn.disabled, true, 'Refresh Soul must stay locked while actively generating');
  assert.equal(resetBtn.disabled, true, 'New Session must stay locked while actively generating');
});

// Held active agent, but a SIBLING is still actively streaming → keep locked (park would interrupt it).
test('a held agent with a running sibling does NOT unlock New Session (park would halt the sibling)', () => {
  const { refreshBtn, resetBtn } = runUpdateControls({
    generating: true, connected: true, paused: false, sessionName: 's',
    subAgents: {
      root: { active: false, is_halted: true, agent_state: 'RUNNING' },   // active agent, held
      worker: { active: true, is_halted: false, agent_state: 'RUNNING' }, // live sibling still streaming
    },
  });
  assert.equal(refreshBtn.disabled, true, 'Refresh Soul must stay locked over a live sibling');
  assert.equal(resetBtn.disabled, true, 'New Session must stay locked over a live sibling');
});

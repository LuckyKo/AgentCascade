// Frontend render-cadence test for the AgentCascade streaming UI (TEST-ONLY).
//
// WHAT THIS MEASURES
// ------------------
// The BACKEND e2e test (test_streaming_e2e_reasoning.py) already proved the backend
// faithfully forwards whatever chunk cadence the LLM produces. This test isolates the
// FRONTEND render path in web_ui/app.js: an adaptive render throttle + a full re-render
// of the thinking block whenever reasoning_content is present. We MEASURE whether that
// path collapses a real stream into an apparent stall (few renders despite many updates).
//
// FINDING (locked in by the assertions below): the frontend does NOT collapse a VISIBLE
// stream. renderSubAgents auto-selects the streaming agent's tab, so from the first paint
// onward `isVisibleActiveAgentContentChanged` (app.js L2180) is true on every partial tick
// and FORCES a render — bypassing the ~250ms time throttle. A smooth ~40ms stream therefore
// renders ~per-update (43/49), with paint gaps ≈ the 40ms update interval, NOT ~250ms. The
// "apparent stall" only appears when the backend itself batches/blobs the chunks (all-burst /
// single-reasoning-blob), in which case the frontend faithfully shows one blob.
//
// APPROACH — REAL app.js, shimmed browser globals
// -----------------------------------------------
// We load the REAL web_ui/app.js source and evaluate it inside a Node `vm` context with
// shimmed browser globals. Nothing about the stream_update handler or renderSubAgents is
// reimplemented — both run for real. The only things faked are:
//   * document / window / localStorage / crypto / WebSocket / fetch  (enough that app.js's
//     top-level init does not throw and connect() no-ops),
//   * marked + hljs + DOMPurify  (real ones are browser libs; we supply faithful-ish
//     stand-ins so renderMarkdown/renderThinkingBlock run. They do NOT affect the throttle
//     gate or the render-entry count — those live in handleServerMessage / renderSubAgents).
//   * a CONTROLLABLE monotonic clock: performance.now() reads our virtual time, which we
//     advance explicitly per update (with realistic wall-clock spacing). This is what lets
//     us reproduce the throttle math deterministically.
//
// We wrap renderSubAgents in the context to count how many times it FIRES and to record,
// per fire, the reasoning_content length of the live partial message (so we can tell whether
// reasoning grew visibly on screen incrementally or appeared as one blob).
//
// No production code is modified. Run:  node --test tests/frontend_stream_render.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.resolve(__dirname, '..', 'web_ui', 'app.js');
const APP_SRC = fs.readFileSync(APP_JS, 'utf8');

// ── Minimal DOM element shim ────────────────────────────────────────────────
// Just enough for app.js top-level init + the stream_update → renderSubAgents path.
function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach(x => x && set.add(x)),
    remove: (...c) => c.forEach(x => x && set.delete(x)),
    toggle: (c, force) => {
      const want = force === undefined ? !set.has(c) : !!force;
      if (want) set.add(c); else set.delete(c);
      return want;
    },
    contains: c => set.has(c),
  };
}

function makeEl(tag = 'div') {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    dataset: {},
    style: {},
    classList: makeClassList(),
    _innerHTML: '',
    _text: '',
    value: '',
    checked: false,
    disabled: false,
    title: '',
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    selectionStart: 0,
    selectionEnd: 0,
    _attrs: {},
    setAttribute(k, v) { el._attrs[k] = String(v); },
    getAttribute(k) { return el._attrs[k] ?? null; },
    removeAttribute(k) { delete el._attrs[k]; },
    _listeners: {},
    appendChild(child) {
      if (child && child.parentNode) child.parentNode.children = child.parentNode.children.filter(c => c !== child);
      child.parentNode = el;
      el.children.push(child);
      return child;
    },
    remove() {
      if (el.parentNode) {
        el.parentNode.children = el.parentNode.children.filter(c => c !== el);
        el.parentNode = null;
      }
    },
    contains(other) {
      let n = other;
      while (n) { if (n === el) return true; n = n.parentNode; }
      return false;
    },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    dispatchEvent() { return true; },
    focus() {},
    blur() {},
    querySelector(sel) { return el.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const out = [];
      (function walk(node) {
        for (const c of node.children) { if (matches(c, sel)) out.push(c); walk(c); }
      })(el);
      return out;
    },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._innerHTML; },
    set(v) { el._innerHTML = String(v); el.children = []; }, // setting innerHTML clears children (approximation)
  });
  Object.defineProperty(el, 'textContent', {
    get() { return el._text; },
    set(v) { el._text = String(v); },
  });
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el.classList).join(' '); },
    set(v) { el.classList = makeClassList(); String(v).split(/\s+/).filter(Boolean).forEach(c => el.classList.add(c)); },
  });
  Object.defineProperty(el, 'firstElementChild', { get() { return el.children[0] || null; } });
  Object.defineProperty(el, 'lastElementChild', { get() { return el.children.length ? el.children[el.children.length - 1] : null; } });
  return el;
}

// Tiny selector matcher supporting: tag, #id, .class, [attr], [attr^="x"], [attr="x"]
function matches(el, sel) {
  if (!el || !sel) return false;
  sel = sel.trim();
  // compound like "pre:not(.mermaid-container)" — handle :not() by stripping for our needs
  let s = sel;
  const notM = s.match(/:not\(([^)]*)\)/);
  if (notM) {
    if (matches(el, notM[1])) return false;
    s = s.replace(notM[0], '');
  }
  // [attr^="x"] / [attr="x"] / [attr]
  const attrRe = /\[([^\]=]+)(?:\^(=)?\s*"?([^"\]]*)"?)?\]/;
  let m;
  while ((m = s.match(attrRe))) {
    const [, name, op, val] = m;
    if (op === '^') { if (!String(el.dataset[name] ?? '').startsWith(val)) return false; }
    else if (val !== undefined) { if (String(el.dataset[name] ?? '') !== val) return false; }
    s = s.replace(m[0], ' ');
  }
  const parts = s.split(/\s+/).filter(Boolean);
  for (const p of parts) {
    if (p.startsWith('#')) { if (el.id !== p.slice(1)) return false; }
    else if (p.startsWith('.')) { if (!el.classList.contains(p.slice(1))) return false; }
    else if (p === '*') { /* any */ }
    else { if (el.tagName !== p.toUpperCase()) return false; }
  }
  return true;
}

// ── Build the vm sandbox ────────────────────────────────────────────────────
function buildSandbox() {
  const body = makeEl('body');
  // Elements app.js grabs at module load and that the render path touches.
  const byId = {
    mainTabBar: makeEl('div'),      // tab bar — renderSubAgents queries .main-tab here
    'main-tab-panels': makeEl('div'), // mainTabPanels = $('.main-tab-panels') — panels appended here
    chatInput: makeEl('textarea'),
    globalActivityBar: makeEl('div'),
    'status-words': makeEl('span'),
    'status-tokens': makeEl('span'),
    'status-model': makeEl('span'),
    approvalBar: makeEl('div'),     // renderApprovals() touches bar.style/innerHTML every tick
    statusText: makeEl('span'),     // updateControls() sets statusText.textContent unguarded
    // Elements app.js wires up at top level WITHOUT null guards — must exist to load.
    continueBtn: makeEl('button'),
    sendBtn: makeEl('button'),
    resetBtn: makeEl('button'),
    agentSelect: makeEl('select'),
    sessionName: makeEl('input'),   // sessionNameInput = $('#sessionName')
  };
  byId.globalActivityBar.children = [makeEl('div')]; // .activity-fifo (so pushImmediate/render no-op cleanly)

  const ids = new Set(Object.keys(byId));
  const document = {
    body,
    activeElement: null,
    visibilityState: 'visible',
    _listeners: {},
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) {
      if (sel && sel.startsWith('#') && ids.has(sel.slice(1))) return byId[sel.slice(1)];
      // class-based selectors: mainTabPanels is grabbed as $('.main-tab-panels')
      if (sel === '.main-tab-panels') return byId['main-tab-panels'];
      // other class-based selectors at top level → none present in our minimal DOM
      return null;
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeEl(tag); },
    createDocumentFragment() { return makeEl('#fragment'); },
    addEventListener(type, fn) { (document._listeners[type] ||= []).push(fn); },
    removeEventListener() {},
  };

  const storage = new Map();
  const localStorage = {
    getItem: k => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k),
  };

  // Global event sink so top-level `window.addEventListener('beforeunload', ...)` etc. don't throw.
  const globalListeners = {};
  function addEventListener(type, fn) { (globalListeners[type] ||= []).push(fn); }
  function removeEventListener() {}

  // Controllable monotonic clock — the heart of the deterministic timing.
  const clock = { now: 0, advance(ms) { this.now += ms; } };

  const sandbox = {
    console,
    addEventListener, removeEventListener,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    performance: { now: () => clock.now },
    document,
    localStorage,
    crypto: { randomUUID: () => 'uuid-' + Math.random().toString(16).slice(2) },
    location: { protocol: 'http:', host: 'localhost', reload() {} },
    WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} },
    fetch: async () => ({ ok: false, status: 404, json: async () => ({}), text: async () => '' }),
    Event: class { constructor(type) { this.type = type; } },
    navigator: { onLine: true, userAgent: 'node-test' },
    // markdown / sanitize stand-ins (see header). Faithful enough for renderThinkingBlock.
    marked: { setOptions() {}, parse(t) { return `<p>${String(t)}</p>`; } },
    hljs: { getLanguage: () => false, highlight: () => ({ value: '' }), highlightAuto: () => ({ value: '' }) },
    DOMPurify: { setConfig() {}, sanitize: (t) => String(t) },
    // expose for the harness
    __clock: clock,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, document, byId, clock };
}

// Load app.js and instrument renderSubAgents. Returns a fresh handle per scenario.
function loadApp() {
  const { sandbox, document, byId, clock } = buildSandbox();
  const ctx = vm.createContext(sandbox);
  // Run the REAL app.js top-level (init, connect(), ActivityBar.init(), etc.).
  vm.runInContext(APP_SRC, ctx, { filename: 'app.js' });

  // Grab the real functions. NOTE: top-level `const`/`function` declarations in a vm
  // script live in the context's lexical scope, NOT on the sandbox object — so we read
  // them back via runInContext expressions.
  const handleServerMessage = vm.runInContext('handleServerMessage', ctx);
  const state = vm.runInContext('state', ctx);

  // Instrument renderSubAgents: count fires + record live reasoning length per fire.
  // We wrap the REAL function and re-expose it in the context so handleServerMessage's
  // internal call to renderSubAgents() hits our wrapper (same lexical scope).
  vm.runInContext('__origRender = renderSubAgents;', ctx);
  vm.runInContext('__records = [];', ctx);
  // Record the reasoning length of the VISIBLE agent's live partial message per fire.
  // For these scenarios the visible tab is 'sub-<sessionName>' (the streaming primary), so this
  // reads the growing partial's reasoning length → tells us if it grew incrementally on screen.
  vm.runInContext(
    `renderSubAgents = function instrumentedRender() {
       var name = state.activeSubTab ? String(state.activeSubTab).replace(/^sub-/, '') : state.sessionName;
       var a = state.subAgents[name];
       var m = a && a.messages ? a.messages[a.messages.length - 1] : null;
       __records.push({ t: __clock.now, reasoningLen: m ? (m.reasoning_content || '').length : 0 });
       return __origRender.apply(this, arguments);
     }`,
    ctx
  );

  // Pre-seed the primary agent with a prior assistant message that already has
  // non-trivial reasoning_content — the trigger condition for the reported regression.
  const SEED_REASONING = 'seed-reasoning-baseline '.repeat(20).trim(); // ~460 chars
  state.subAgents[state.sessionName] = {
    messages: [
      { role: 'user', content: 'Prior user prompt.' },
      { role: 'assistant', content: 'Prior answer.', reasoning_content: SEED_REASONING, name: state.sessionName },
    ],
    history_count: 2,
    is_partial: false,
    active: false,
    agent_class: 'coder',
    agent_state: 'IDLE',
    _lastHistoryCount: 2,
  };

  // Read the render records back out of the context.
  const getRenders = () => vm.runInContext('__records', ctx);

  // feedScenario must call handleServerMessage THROUGH the context so it resolves to the
  // current (wrapped) binding in the context's lexical scope.
  const callHandle = (data) => { sandbox.__data = data; vm.runInContext('handleServerMessage(__data)', ctx); };

  return { sandbox, document, byId, clock, state, handleServerMessage: callHandle, getRenders, sessionName: state.sessionName };
}

// ── stream_update payload builder (matches build_stream_update_from_pool shape) ──
// `extraAgents` lets us include OTHER agents in the payload so cleanupStaleSubAgents()
// (which deletes any agent absent from agent_instances) does not remove a decoy visible tab.
function makeUpdate({ name, reasoning = '', content = '', historyCount, isPartial, active = true, agentState = 'RUNNING', stack = [], extraAgents = {} }) {
  const messages = [
    // prior user + prior assistant (constant history while the live partial grows)
    { role: 'user', content: 'Prior user prompt.' },
    { role: 'assistant', content: 'Prior answer.', reasoning_content: 'seed-reasoning-baseline '.repeat(20).trim(), name },
    // the growing live partial assistant message
    { role: 'assistant', content, reasoning_content: reasoning, name },
  ];
  const agent_instances = {
    [name]: {
      messages,
      history_count: historyCount,
      is_partial: isPartial,
      active,
      agent_class: 'coder',
      agent_state: agentState,
    },
  };
  for (const [en, edata] of Object.entries(extraAgents)) agent_instances[en] = edata;
  return {
    type: 'stream_update',
    agent_instances,
    active_stack: stack, // empty → root/primary path (root throttle)
    pool_settings: {},
    total_tokens: 100 + reasoning.length + content.length,
    current_model: 'test-model',
    paused: false,
    approvals: [],
  };
}

// Feed a sequence of {dtMs, payload} and return measured stats.
function feedScenario(app, steps) {
  const { clock, handleServerMessage, getRenders, state } = app;
  let updates = 0;
  for (const step of steps) {
    clock.advance(step.dtMs); // realistic wall-clock spacing between arrivals
    handleServerMessage(step.payload);
    updates++;
  }
  const renders = getRenders();
  const renderTimes = renders.map(r => r.t);
  let maxRenderGap = 0;
  for (let i = 1; i < renderTimes.length; i++) maxRenderGap = Math.max(maxRenderGap, renderTimes[i] - renderTimes[i - 1]);
  const distinctReasoningLens = [...new Set(renders.map(r => r.reasoningLen))].sort((a, b) => a - b);
  return {
    updates,
    renders: renders.length,
    maxRenderGap,
    renderTimes,
    reasoningLens: renders.map(r => r.reasoningLen),
    distinctReasoningLens,
    incremental: distinctReasoningLens.length > 1, // reasoning grew visibly on screen across renders
    finalReasoningLen: (() => {
      const name = state.activeSubTab ? String(state.activeSubTab).replace(/^sub-/, '') : state.sessionName;
      return state.subAgents[name]?.messages?.at(-1)?.reasoning_content?.length ?? 0;
    })(),
  };
}

// ── Scenario generators ─────────────────────────────────────────────────────
const R = (i) => 'r'.repeat(24) + i; // a chunk of reasoning text per step

function scenarioIncremental(name) {
  const steps = [];
  let reasoning = '';
  for (let i = 1; i <= 45; i++) {           // ~45 small reasoning updates ~40ms apart
    reasoning += R(i);
    steps.push({ dtMs: 40, payload: makeUpdate({ name, reasoning, content: '', historyCount: 3, isPartial: true }) });
  }
  let content = '';
  for (let i = 1; i <= 4; i++) {             // short content phase ~40ms apart
    content += 'word' + i + ' ';
    steps.push({ dtMs: 40, payload: makeUpdate({ name, reasoning, content, historyCount: 3, isPartial: true }) });
  }
  return steps;
}

function scenarioAllBurst(name) {
  const fullReasoning = Array.from({ length: 45 }, (_, i) => R(i + 1)).join('');
  const content = 'word1 word2 word3 word4 ';
  // one tiny early tick, then a single accumulated blob at the end
  return [
    { dtMs: 40, payload: makeUpdate({ name, reasoning: R(1), content: '', historyCount: 3, isPartial: true }) },
    { dtMs: 1800, payload: makeUpdate({ name, reasoning: fullReasoning, content, historyCount: 3, isPartial: false, active: false, agentState: 'IDLE' }) },
  ];
}

function scenarioChunked(name) {
  const steps = [];
  let reasoning = '';
  for (let b = 1; b <= 4; b++) {             // 4 big batches ~600ms apart
    for (let i = 1; i <= 12; i++) reasoning += R(b * 12 + i);
    steps.push({ dtMs: 600, payload: makeUpdate({ name, reasoning, content: '', historyCount: 3, isPartial: true }) });
  }
  const content = 'final answer here ';
  steps.push({ dtMs: 400, payload: makeUpdate({ name, reasoning, content, historyCount: 3, isPartial: false, active: false, agentState: 'IDLE' }) });
  return steps;
}

function scenarioSingleReasoningBlob(name) {
  const blob = Array.from({ length: 45 }, (_, i) => R(i + 1)).join(''); // one big reasoning blob
  const steps = [
    { dtMs: 40, payload: makeUpdate({ name, reasoning: blob.slice(0, 24), content: '', historyCount: 3, isPartial: true }) },
    { dtMs: 500, payload: makeUpdate({ name, reasoning: blob, content: '', historyCount: 3, isPartial: true }) }, // whole blob arrives at once
    { dtMs: 400, payload: makeUpdate({ name, reasoning: blob, content: 'the answer ', historyCount: 3, isPartial: false, active: false, agentState: 'IDLE' }) },
  ];
  return steps;
}

// ── Test ────────────────────────────────────────────────────────────────────
test('frontend render-cadence: does the adaptive throttle collapse a real stream?', () => {
  // Primary/visible scenarios: the streaming agent is the one on screen (activeStack empty →
  // root path; renderSubAgents auto-selects its tab so the visible-content bypass is active).
  const scenarios = [
    ['incremental', scenarioIncremental],
    ['all-burst', scenarioAllBurst],
    ['chunked', scenarioChunked],
    ['single-reasoning-blob', scenarioSingleReasoningBlob],
  ];

  const results = {};
  for (const [label, gen] of scenarios) {
    const app = loadApp();
    const steps = gen(app.sessionName);
    results[label] = feedScenario(app, steps);
  }

  // ── Report per-scenario numbers ────────────────────────────────────────────
  console.log('\n════════ FRONTEND RENDER-CADENCE RESULTS ════════');
  for (const [label, r] of Object.entries(results)) {
    const collapse = r.updates / Math.max(1, r.renders);
    console.log(`\n[${label}]`);
    console.log(`  updates fed in : ${r.updates}`);
    console.log(`  renders fired  : ${r.renders}   (collapse ratio ≈ ${collapse.toFixed(1)}:1)`);
    console.log(`  max render gap : ${r.maxRenderGap} ms  (user-perceived "frozen" window between paints)`);
    console.log(`  reasoning on screen: ${r.incremental ? 'INCREMENTAL' : 'ONE BLOB'}  (distinct rendered lengths=${r.distinctReasoningLens.length})`);
    console.log(`  final reasoning len: ${r.finalReasoningLen}`);
  }
  console.log('\n═══════════════════════════════════════════════════\n');

  const inc = results['incremental'];
  const burst = results['all-burst'];
  const chunked = results['chunked'];
  const blob = results['single-reasoning-blob'];

  // ── Assertions on OBSERVED behavior (lock in the baseline) ────────────────
  // Sanity: the real handler + render path actually ran.
  assert.ok(inc.updates === 49, `incremental should feed 49 updates, got ${inc.updates}`);
  assert.ok(inc.renders > 0, 'renderSubAgents must fire at least once on a smooth stream');

  // ══════════════════════════════════════════════════════════════════════════
  // THE FINDING — the frontend does NOT collapse a VISIBLE stream.
  //
  // The task hypothesized the adaptive throttle would collapse a smooth ~40ms stream to
  // ~8-12 renders with ~250ms gaps. That is FALSE for the agent the user is looking at:
  // renderSubAgents auto-selects the streaming agent's tab (app.js L4065), so from the
  // first paint onward `isVisibleActiveAgentContentChanged` (L2180) is true on every
  // partial update and FORCES a render — bypassing the time throttle entirely. The result:
  // renders track updates almost 1:1, with gaps equal to the inter-UPDATE interval (~40ms),
  // not the ~250ms root throttle.
  assert.ok(inc.renders >= inc.updates - 6,
    `VISIBILITY BYPASS — a smooth stream viewed on-screen renders ~per-update: ${inc.updates} updates → ${inc.renders} renders. ` +
    `The 250ms time throttle does NOT apply to the visible agent (isVisibleActiveAgentContentChanged forces a render each tick).`);
  assert.ok(inc.maxRenderGap <= 80,
    `No perceived stall on a visible smooth stream: max gap between paints is ${inc.maxRenderGap}ms (≈ the 40ms update interval), NOT the ~250ms root throttle.`);
  // The thinking block grows visibly incrementally on screen (many distinct lengths).
  assert.ok(inc.incremental && inc.distinctReasoningLens.length >= 3,
    `On a visible smooth stream the thinking block should grow across many renders — got ${inc.distinctReasoningLens.length} distinct lengths`);

  // all-burst: backend sends one big blob at the end → frontend can only show it as a blob.
  assert.ok(burst.renders <= 3, `all-burst should render very few times (got ${burst.renders})`);
  assert.equal(burst.incremental, false, 'all-burst reasoning appears as ONE blob on screen');

  // chunked: renders track the batch cadence (~600ms) — a clear collapse vs per-update.
  assert.ok(chunked.renders <= 6, `chunked should render ~once per batch (got ${chunked.renders})`);
  assert.ok(chunked.maxRenderGap >= 400, `chunked max render gap should be ~600ms (got ${chunked.maxRenderGap}ms)`);

  // single-reasoning-blob: one big reasoning blob then content → appears as a blob.
  assert.ok(blob.distinctReasoningLens.length <= 2,
    `single-reasoning-blob should show ≤2 distinct reasoning lengths (got ${blob.distinctReasoningLens.length})`);

  console.log('✔ All render-cadence assertions passed — baseline locked in.');
});

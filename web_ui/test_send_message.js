/**
 * Sanity checks for send_message-related frontend logic.
 *
 * Run in Node.js (no browser needed) to validate core functions:
 *   node web_ui/test_send_message.js
 *
 * These tests mock localStorage and DOM elements where needed,
 * then verify the behavior of loadAgentMessages/saveAgentMessages,
 * duplicate detection, @mention parsing, and badge logic.
 */

'use strict';

// ─── Mock environment ──────────────────────────────────────────────────────

const state = {
  agentMessages: [],
  subAgents: {}
};

const AGENT_MESSAGES_STORAGE_KEY = 'agent-cascade-agent-messages';
const MAX_AGENT_MESSAGES = 200;
const AGENT_MESSAGES_SEEN_IDS = new Set();

// Minimal localStorage mock backed by an in-memory Map
const storage = new Map();
global.localStorage = {
  getItem(key) { return storage.get(key) || null; },
  setItem(key, value) { storage.set(key, value); },
  removeItem(key) { storage.delete(key); },
  clear() { storage.clear(); }
};

// Minimal DOM mock for badge tests
const badgeEl = { style: { display: 'none' }, textContent: '' };
global.document = {
  getElementById(name) {
    if (name === 'agentMessagesBadge') return badgeEl;
    return null;
  }
};

// crypto.randomUUID mock for addAgentMessage
if (!global.crypto) global.crypto = {};
let uidCounter = 1;
global.crypto.randomUUID = () => `test-uid-${uidCounter++}`;

// ─── Functions under test (copied from app.js logic for isolated testing) ──

function loadAgentMessages() {
  try {
    const raw = localStorage.getItem(AGENT_MESSAGES_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        state.agentMessages = parsed.slice(-MAX_AGENT_MESSAGES);
        state.agentMessages.forEach(m => { if (m.id) AGENT_MESSAGES_SEEN_IDS.add(m.id); });
        return;
      }
    }
  } catch (e) {
    console.warn('[AgentMessages] Failed to load from localStorage:', e);
  }
  state.agentMessages = [];
}

function saveAgentMessages() {
  try {
    if (state.agentMessages.length > MAX_AGENT_MESSAGES) {
      const removed = state.agentMessages.slice(0, state.agentMessages.length - MAX_AGENT_MESSAGES);
      removed.forEach(m => AGENT_MESSAGES_SEEN_IDS.delete(m.id));
      state.agentMessages = state.agentMessages.slice(-MAX_AGENT_MESSAGES);
    }
    localStorage.setItem(AGENT_MESSAGES_STORAGE_KEY, JSON.stringify(state.agentMessages));
  } catch (e) {
    console.warn('[AgentMessages] Failed to save to localStorage:', e);
  }
}

function addAgentMessage(msg) {
  if (!state.agentMessages) state.agentMessages = [];
  if (!msg.id || !msg.sender || !msg.message) {
    return false;
  }
  if (AGENT_MESSAGES_SEEN_IDS.has(msg.id)) {
    return false;
  }
  AGENT_MESSAGES_SEEN_IDS.add(msg.id);
  state.agentMessages.push(msg);
  saveAgentMessages();
  return true;
}

function markAllMessagesRead() {
  if (!state.agentMessages?.length) return;
  const hadUnread = state.agentMessages.some(m => !m.read);
  state.agentMessages.forEach(m => m.read = true);
  if (hadUnread) {
    saveAgentMessages();
    updateAgentMessagesBadge();
  }
}

function updateAgentMessagesBadge() {
  const badge = document.getElementById('agentMessagesBadge');
  if (!badge) return;
  const unreadCount = (state.agentMessages || []).filter(m => !m.read).length;
  if (unreadCount > 0) {
    badge.style.display = 'inline-flex';
    badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
  } else {
    badge.style.display = 'none';
  }
}

function parseMentionRouting(text) {
  if (!text) return { targetAgent: null, cleanedText: text };
  const mentionMatch = text.match(/^\s*@(\S+)\s+(.*)$/s);
  if (!mentionMatch) {
    return { targetAgent: null, cleanedText: text.trim() };
  }
  const mentionedName = mentionMatch[1];
  const cleanedText = mentionMatch[2].trim();
  if (state.subAgents && state.subAgents[mentionedName]) {
    return { targetAgent: mentionedName, cleanedText: cleanedText || text.trim() };
  }
  console.log('[AgentMessages] @mention for unknown agent:', mentionedName);
  return { targetAgent: null, cleanedText: text.trim() };
}

// ─── Test harness ──────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (!condition) {
    console.error(`❌ FAIL: ${msg}`);
    failed++;
  } else {
    console.log(`✓ PASS: ${msg}`);
    passed++;
  }
}

function reset() {
  storage.clear();
  state.agentMessages = [];
  AGENT_MESSAGES_SEEN_IDS.clear();
  badgeEl.style.display = 'none';
  badgeEl.textContent = '';
}

// ─── Tests: State management (load/save round-trip) ────────────────────────

function test_load_save_roundtrip() {
  console.log('\n--- State Management: load/save round-trip ---');
  reset();

  const msgs = [
    { id: 'm1', sender: 'worker1', message: 'Hello', read: false, timestamp: Date.now() },
    { id: 'm2', sender: 'worker2', message: 'World', read: true, timestamp: Date.now() }
  ];

  localStorage.setItem(AGENT_MESSAGES_STORAGE_KEY, JSON.stringify(msgs));
  loadAgentMessages();

  assert(state.agentMessages.length === 2, 'loadAgentMessages loads stored messages');
  assert(state.agentMessages[0].id === 'm1', 'First message ID matches');
  assert(state.agentMessages[1].read === true, 'Read flag preserved on load');
  assert(AGENT_MESSAGES_SEEN_IDS.has('m1'), 'Seen IDs populated from loaded messages');
}

function test_save_persists_to_storage() {
  console.log('\n--- State Management: save persists to storage ---');
  reset();

  state.agentMessages = [{ id: 'x1', sender: 'a', message: 'test', read: false }];
  AGENT_MESSAGES_SEEN_IDS.add('x1');
  saveAgentMessages();

  const stored = JSON.parse(localStorage.getItem(AGENT_MESSAGES_STORAGE_KEY));
  assert(Array.isArray(stored), 'Saved data is an array');
  assert(stored.length === 1 && stored[0].id === 'x1', 'Correct message saved to storage');
}

function test_load_empty_storage() {
  console.log('\n--- State Management: empty storage ---');
  reset();
  localStorage.removeItem(AGENT_MESSAGES_STORAGE_KEY);
  loadAgentMessages();

  assert(Array.isArray(state.agentMessages) && state.agentMessages.length === 0, 'Empty array when no stored data');
}

function test_load_corrupt_storage() {
  console.log('\n--- State Management: corrupt storage ---');
  reset();
  localStorage.setItem(AGENT_MESSAGES_STORAGE_KEY, 'not valid json {{{');
  loadAgentMessages();

  assert(Array.isArray(state.agentMessages) && state.agentMessages.length === 0, 'Graceful fallback on corrupt JSON');
}

// ─── Tests: Duplicate detection ────────────────────────────────────────────

function test_duplicate_id_rejected() {
  console.log('\n--- Duplicate Detection ---');
  reset();

  const msg = { id: 'dup1', sender: 'w1', message: 'first', read: false };
  assert(addAgentMessage(msg) === true, 'First add succeeds');

  const result = addAgentMessage(msg);
  assert(result === false, 'Duplicate ID rejected');
  assert(state.agentMessages.length === 1, 'Only one copy stored');
}

function test_duplicate_after_reload() {
  console.log('\n--- Duplicate Detection after reload ---');
  reset();

  const msg = { id: 'reload1', sender: 'w1', message: 'hello', read: false };
  addAgentMessage(msg);

  // Simulate page reload
  loadAgentMessages();

  const result = addAgentMessage(msg);
  assert(result === false, 'Duplicate rejected after reload (seen IDs restored)');
}

// ─── Tests: @mention parsing ───────────────────────────────────────────────

function test_mention_valid_agent() {
  console.log('\n--- @mention Parsing ---');
  reset();

  state.subAgents = { worker1: { name: 'worker1' } };

  const result = parseMentionRouting('@worker1 please do this task');
  assert(result.targetAgent === 'worker1', 'Valid @mention extracts agent name');
  assert(result.cleanedText === 'please do this task', '@mention removed from cleaned text');
}

function test_mention_unknown_agent() {
  console.log('\n--- @mention Parsing: unknown agent ---');
  reset();

  state.subAgents = {};

  const result = parseMentionRouting('@nonexistent do stuff');
  assert(result.targetAgent === null, 'Unknown agent → no routing');
  assert(result.cleanedText === '@nonexistent do stuff', 'Original text preserved for unknown agent');
}

function test_mention_no_trailing_text() {
  console.log('\n--- @mention Parsing: no trailing text ---');
  reset();

  state.subAgents = { worker1: {} };

  // Regex requires whitespace + content after @name, so this shouldn't match as a mention
  const result = parseMentionRouting('@worker1');
  assert(result.targetAgent === null, 'Bare @mention without message → no routing');
}

function test_mention_leading_whitespace() {
  console.log('\n--- @mention Parsing: leading whitespace ---');
  reset();

  state.subAgents = { worker1: {} };

  const result = parseMentionRouting('   @worker1 hello there');
  assert(result.targetAgent === 'worker1', 'Leading whitespace allowed before @mention');
  assert(result.cleanedText === 'hello there', 'Cleaned text trimmed correctly');
}

function test_no_mention_normal_text() {
  console.log('\n--- @mention Parsing: normal text ---');
  reset();

  const result = parseMentionRouting('just a regular message');
  assert(result.targetAgent === null, 'No routing for normal text');
  assert(result.cleanedText === 'just a regular message', 'Text returned as-is (trimmed)');
}

// ─── Tests: Badge logic ────────────────────────────────────────────────────

function test_badge_shows_unread() {
  console.log('\n--- Badge Logic ---');
  reset();

  state.agentMessages = [
    { id: 'b1', sender: 'w1', message: 'm1', read: false },
    { id: 'b2', sender: 'w2', message: 'm2', read: false }
  ];

  updateAgentMessagesBadge();
  assert(badgeEl.style.display === 'inline-flex', 'Badge visible with unread messages');
  assert(badgeEl.textContent === '2', 'Badge shows correct unread count');
}

function test_badge_hidden_when_all_read() {
  console.log('\n--- Badge Logic: all read ---');
  reset();

  state.agentMessages = [
    { id: 'b3', sender: 'w1', message: 'm1', read: true }
  ];

  updateAgentMessagesBadge();
  assert(badgeEl.style.display === 'none', 'Badge hidden when no unread messages');
}

function test_badge_99_plus() {
  console.log('\n--- Badge Logic: 99+ cap ---');
  reset();

  state.agentMessages = Array.from({ length: 150 }, (_, i) => ({
    id: `b${i}`, sender: 'w', message: 'm', read: false
  }));

  updateAgentMessagesBadge();
  assert(badgeEl.style.display === 'inline-flex', 'Badge visible for large unread count');
  assert(badgeEl.textContent === '99+', 'Badge caps at 99+');
}

function test_mark_all_read_clears_badge() {
  console.log('\n--- Badge Logic: mark all read ---');
  reset();

  state.agentMessages = [
    { id: 'b10', sender: 'w1', message: 'm1', read: false },
    { id: 'b11', sender: 'w2', message: 'm2', read: false }
  ];

  updateAgentMessagesBadge();
  assert(badgeEl.style.display === 'inline-flex', 'Badge visible before markAllRead');

  markAllMessagesRead();
  assert(badgeEl.style.display === 'none', 'Badge hidden after markAllRead');
}

// ─── Run all tests ─────────────────────────────────────────────────────────

console.log('=== send_message Frontend Sanity Checks ===\n');

test_load_save_roundtrip();
test_save_persists_to_storage();
test_load_empty_storage();
test_load_corrupt_storage();
test_duplicate_id_rejected();
test_duplicate_after_reload();
test_mention_valid_agent();
test_mention_unknown_agent();
test_mention_no_trailing_text();
test_mention_leading_whitespace();
test_no_mention_normal_text();
test_badge_shows_unread();
test_badge_hidden_when_all_read();
test_badge_99_plus();
test_mark_all_read_clears_badge();

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
process.exit(failed > 0 ? 1 : 0);

/**
 * Sanity checks for the renderSubAgentPanel contentKey logic (app.js ~4337-4359).
 *
 * Run in Node.js (no browser needed):  node web_ui/test_content_key.js
 *
 * Regression: streaming froze after the FIRST tool bubble in a parallel (multi) tool-call turn.
 * Root cause: contentKey only reflected the LAST message's function_call args (funcCallLen), so
 * when a NON-last parallel tool bubble changed/collapsed, the key was unchanged and the render
 * was skipped. The fix appends a `toolSig` segment — the name+arguments of EVERY tool-call
 * bubble joined by '|' — so any change to any tool bubble forces a re-render.
 *
 * These tests replicate the exact contentKey expression from app.js and assert:
 *   1. changing a NON-last tool bubble's args changes the key (the fix);
 *   2. dropping a non-last tool bubble changes the key;
 *   3. an identical message list yields the SAME key (no spurious re-render).
 */

'use strict';

let failures = 0;
function check(name, cond) {
  if (cond) {
    console.log('  PASS ' + name);
  } else {
    console.error('  FAIL ' + name);
    failures++;
  }
}

// ── Replicate the EXACT contentKey expression from app.js (lines 4337-4359) ──────────
// Mirrors: lastMsgTextLen, reasoningLen, funcCallLen, activeFlag, tokPart, toolSig, contentKey.
function computeContentKey(displayMsgs, { generating = false, activeFlag = false, tokCount = 0 } = {}) {
  const lastMsg = displayMsgs.length ? displayMsgs[displayMsgs.length - 1] : null;

  const lastMsgTextLen = lastMsg ? (Array.isArray(lastMsg.content)
    ? lastMsg.content.reduce((s, item) => s + (item.text?.length || 0), 0)
    : (lastMsg.content || '').length) : 0;
  const reasoningLen = lastMsg ? (lastMsg.reasoning_content || '').length : 0;
  const funcCallLen = lastMsg?.function_call?.arguments ? (lastMsg.function_call.arguments + '').length : 0;

  const tokPart = (typeof tokCount === 'number' && !isNaN(tokCount)) ? tokCount : '0';

  // THE FIX: tool-bubble signature over EVERY tool-call bubble, not just the last message.
  const toolSig = displayMsgs.filter(m => m.function_call)
    .map(m => (m.function_call.name || '') + ':' + (m.function_call.arguments || ''))
    .join('|');

  return displayMsgs.length + ':' + lastMsgTextLen + ':' + reasoningLen + ':' + funcCallLen + ':' +
    activeFlag + ':' + (generating ? '1' : '0') + ':' + tokPart + ':' + toolSig;
}

// ── Helper to build a message list ────────────────────────────────────────────────
function toolMsg(name, args) {
  return { role: 'assistant', content: '', function_call: { name, arguments: args } };
}
function textMsg(text) {
  return { role: 'assistant', content: text };
}

// ── Test 1: changing a NON-last tool bubble's args changes the key (the fix) ────────
console.log('Test 1: non-last tool bubble arg change forces re-render');
{
  // Two parallel tool bubbles + a trailing text message (so the last msg is NOT a tool call).
  const before = [toolMsg('search', '{"q":"a"}'), toolMsg('write_file', '{"p":"/x"}'), textMsg('done')];
  // Change ONLY the FIRST (non-last) tool bubble's args — same length, different value.
  const after = [toolMsg('search', '{"q":"b"}'), toolMsg('write_file', '{"p":"/x"}'), textMsg('done')];

  const keyBefore = computeContentKey(before, { generating: true });
  const keyAfter = computeContentKey(after, { generating: true });

  check('key changes when non-last tool bubble args change', keyBefore !== keyAfter);

  // PROVE the old (pre-fix) key would NOT have changed — i.e. this test actually catches the bug.
  // The old key had no toolSig segment; with same lengths it stayed identical.
  function oldKey(displayMsgs, { generating = false, activeFlag = false, tokCount = 0 } = {}) {
    const lastMsg = displayMsgs.length ? displayMsgs[displayMsgs.length - 1] : null;
    const lastMsgTextLen = lastMsg ? (lastMsg.content || '').length : 0;
    const reasoningLen = lastMsg ? (lastMsg.reasoning_content || '').length : 0;
    const funcCallLen = lastMsg?.function_call?.arguments ? (lastMsg.function_call.arguments + '').length : 0;
    const tokPart = (typeof tokCount === 'number' && !isNaN(tokCount)) ? tokCount : '0';
    return displayMsgs.length + ':' + lastMsgTextLen + ':' + reasoningLen + ':' + funcCallLen + ':' +
      activeFlag + ':' + (generating ? '1' : '0') + ':' + tokPart;  // no toolSig
  }
  check('old key did NOT change (confirms the bug this test guards)', oldKey(before) === oldKey(after));
}

// ── Test 2: dropping a non-last tool bubble changes the key ────────────────────────
console.log('Test 2: dropped non-last tool bubble forces re-render');
{
  const before = [toolMsg('search', '{"q":"a"}'), toolMsg('write_file', '{"p":"/x"}'), textMsg('done')];
  // The second (non-last) tool bubble is dropped.
  const after = [toolMsg('search', '{"q":"a"}'), textMsg('done')];

  check('key changes when non-last tool bubble is dropped',
    computeContentKey(before, { generating: true }) !== computeContentKey(after, { generating: true }));
}

// ── Test 3: identical message list yields the SAME key (no spurious re-render) ─────
console.log('Test 3: unchanged message list keeps the same key');
{
  const a = [toolMsg('search', '{"q":"a"}'), toolMsg('write_file', '{"p":"/x"}'), textMsg('done')];
  const b = [toolMsg('search', '{"q":"a"}'), toolMsg('write_file', '{"p":"/x"}'), textMsg('done')];

  check('identical lists produce identical keys',
    computeContentKey(a, { generating: true }) === computeContentKey(b, { generating: true }));
}

// ── Test 4: last-message tool bubble change still detected (regression guard) ───────
console.log('Test 4: last tool bubble arg change still forces re-render');
{
  const before = [textMsg('thinking'), toolMsg('search', '{"q":"a"}')];
  const after = [textMsg('thinking'), toolMsg('search', '{"q":"b"}')];

  check('key changes when last tool bubble args change',
    computeContentKey(before, { generating: true }) !== computeContentKey(after, { generating: true }));
}

console.log('');
if (failures === 0) {
  console.log('ALL PASS');
  process.exit(0);
} else {
  console.error(failures + ' FAILURE(S)');
  process.exit(1);
}

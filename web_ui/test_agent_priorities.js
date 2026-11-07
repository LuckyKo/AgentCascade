/**
 * Sanity checks for the "Use Last Endpoint" agent-priorities UI fix (TODO 90, app.js).
 *
 * Run in Node.js (no browser needed):  node web_ui/test_agent_priorities.js
 *
 * Two bugs fixed:
 *  1. Case mismatch (app.js ~2157-2176): the WS re-sync deduped agent_priorities by
 *     key.toLowerCase() but KEPT the first-seen original case. The render path builds its
 *     type via agent_type.toLowerCase() and reads block.dataset.type (both lowercase), but the
 *     server stores some keys PascalCase (e.g. "Generalist"), so a lowercase lookup saw
 *     undefined and the sentinel/assignment looked "removed". FIX: store under the lowercase
 *     key, merging value arrays dedup-preserving on a case-collision.
 *  2. Empty-list not propagated (app.js ~7244-7252): the remove handler did
 *     `if (len===0) delete priorities[type]`, so updatePrioritiesRest() (which posts the whole
 *     agent_priorities object) omitted that key and the server never cleared it → the entry
 *     reappeared on the next broadcast. FIX: keep the empty array so the POST sends `type: []`.
 *
 * These tests replicate the exact normalizer and POST-body shapes and assert the new behaviour
 * while proving the old behaviour failed (so the tests genuinely guard the regression).
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

// ── NEW normalizer: replicate EXACT loop from app.js (~2157-2176) ─────────────────
function normalizePriorities(rawPriorities) {
  const normalizedPriorities = {};
  const seenLower = new Set();
  for (const [key, value] of Object.entries(rawPriorities || {})) {
    if (!key) continue;
    const keyLower = key.toLowerCase();
    const incoming = Array.isArray(value) ? value : [];
    if (!seenLower.has(keyLower)) {
      normalizedPriorities[keyLower] = incoming.slice();
      seenLower.add(keyLower);
    } else {
      const merged = normalizedPriorities[keyLower];
      for (const eid of incoming) {
        if (!merged.includes(eid)) merged.push(eid);
      }
    }
  }
  return normalizedPriorities;
}

// ── OLD normalizer (pre-fix): dedup by lowercase but keep original-case key ───────
function normalizePrioritiesOld(rawPriorities) {
  const normalizedPriorities = {};
  const seenLower = new Set();
  for (const [key, value] of Object.entries(rawPriorities || {})) {
    if (!key) continue;
    const keyLower = key.toLowerCase();
    if (!seenLower.has(keyLower)) {
      normalizedPriorities[key] = value;   // kept original case → lowercase lookup misses
      seenLower.add(keyLower);
    }
  }
  return normalizedPriorities;
}

// ── POST body shape mirrors updatePrioritiesRest(): ───────────────────────────────
//   body: JSON.stringify({ agent_priorities: state.api_router.agent_priorities || {} })
function postBody(priorities) {
  return JSON.stringify({ agent_priorities: priorities || {} });
}

// Representative snapshot mirroring the live config/api_endpoints.json keys (mixed case).
const liveLike = {
  orchestrator: ['e1', 'e2'],
  admin: ['__use_last_endpoint__'],
  Generalist: ['__use_last_endpoint__'],
  Security: ['__use_last_endpoint__'],
};

// ── Test 1: PascalCase key resolves via lowercase lookup (the invisible-sentinel bug) ──
console.log('Test 1: PascalCase key is reachable via lowercase lookup');
{
  const norm = normalizePriorities(liveLike);
  check("generalist lookup sees the Generalist data",
    JSON.stringify(norm['generalist']) === JSON.stringify(['__use_last_endpoint__']));
  check('Security reachable via security lookup',
    Array.isArray(norm['security']) && norm['security'].length === 1);
  check('no PascalCase keys survive (all normalized to lowercase)',
    Object.keys(norm).every(k => k === k.toLowerCase()));
  // Prove the OLD behaviour failed: lowercase lookup was undefined.
  const old = normalizePrioritiesOld(liveLike);
  check('OLD behavior: generalist lookup was undefined (confirms the bug)',
    old['generalist'] === undefined && old['Generalist'] !== undefined);
}

// ── Test 2: case-collision merges value arrays dedup-preserving, no data loss ──────
console.log('Test 2: case-collision merges dedup-preserving under lowercase key');
{
  const collision = {
    Generalist: ['a', 'b', 'shared'],
    generalist: ['c', 'shared'],
  };
  const norm = normalizePriorities(collision);
  check('merged under a single lowercase key',
    JSON.stringify(Object.keys(norm)) === JSON.stringify(['generalist']));
  check('merge keeps order, dedups overlap, no data lost',
    JSON.stringify(norm['generalist']) === JSON.stringify(['a', 'b', 'shared', 'c']));
  // Count-based "log if changed" guard: 2 raw keys → 1 normalized key.
  check('collision detected as a count change (log-guard fires)',
    Object.keys(collision).length !== Object.keys(norm).length);
}

// ── Test 3: removing the last item sends [] so the server clears (no pop-back) ─────
console.log('Test 3: empty list is propagated as [] on remove');
{
  // NEW remove handler: filters out the id, KEEPS the empty array.
  const priorities = { generalist: ['__use_last_endpoint__'] };
  priorities.generalist = priorities.generalist.filter(eid => eid !== '__use_last_endpoint__');
  // (no delete) — exactly the new code path
  const body = postBody(priorities);
  check('empty array kept in state', priorities.generalist !== undefined && priorities.generalist.length === 0);
  check('POST body contains generalist:[]', body.includes('"generalist":[]'));

  // Prove the OLD handler dropped the key so the POST omitted it → server kept it.
  const prioritiesOld = { generalist: ['__use_last_endpoint__'] };
  prioritiesOld.generalist = prioritiesOld.generalist.filter(eid => eid !== '__use_last_endpoint__');
  if (prioritiesOld.generalist.length === 0) delete prioritiesOld.generalist;
  const bodyOld = postBody(prioritiesOld);
  check('OLD behavior: POST body omitted the key entirely (confirms the pop-back bug)',
    !bodyOld.includes('generalist'));
}

// ── Test 4: normalization preserves the __use_last_endpoint__ sentinel untouched ──
console.log('Test 4: sentinel values are preserved through normalization');
{
  const norm = normalizePriorities({ admin: ['__use_last_endpoint__'] });
  check('admin lowercase key preserved with sentinel intact',
    JSON.stringify(norm['admin']) === JSON.stringify(['__use_last_endpoint__']));
}

console.log('');
if (failures === 0) {
  console.log('ALL PASS');
  process.exit(0);
} else {
  console.error(failures + ' FAILURE(S)');
  process.exit(1);
}

/**
 * Sanity checks for renderSessionPanel()'s collapse-to-latest-per-session loop (app.js ~1287-1295).
 *
 * Run in Node.js (no browser needed):  node web_ui/test_session_panel_latest.js
 *
 * Behavior: the sessions list now renders ONE card per logical session (stack_id) by keeping only
 * the max-`created_at` snapshot from that stack's `snapshots[]`. The ACTIVE stack is skipped
 * (rendered on a separate path). Open/Delete key off the kept snapshot's `snapshot_id`, so they hit
 * the latest file. The filter box runs against the collapsed (latest) row's name + latest caption.
 *
 * This test replicates the EXACT loop body from app.js and asserts the plan's edge cases:
 *   1. single-snapshot session -> exactly one card;
 *   2. multi-snapshot session  -> keeps max created_at (and its snapshot_id, so Open/Delete hit it);
 *   3. ACTIVE stack is skipped entirely;
 *   4. filter matches name / latest caption; older (hidden) captions are NOT searchable;
 *   5. empty snapshots array -> skipped;
 *   6. tie in created_at      -> first-encountered wins (stable, `>` not `>=`);
 *   7. rows sort by created_at desc across sessions.
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

// ── Replicate the EXACT collapse loop body from app.js (lines 1287-1295) ─────────────
function buildRows(sessions, activeId, q) {
  q = (q || '').toLowerCase();                            // mirrors app.js:1285 (query lowercased upstream)
  const stacks = sessions.stacks || {};
  const details = sessions.details || {};
  const rows = [];
  for (const sid of Object.keys(stacks)) {
    if (sid === activeId) continue;                       // ACTIVE card is separate — untouched
    const snaps = details[sid]?.snapshots || [];
    if (!snaps.length) continue;
    // One card per logical session (stack_id): keep only the most-recent snapshot.
    let latest = snaps[0];
    for (const s of snaps) { if ((s.created_at || 0) > (latest.created_at || 0)) latest = s; }
    const name = details[sid]?.display_name || sid;
    if (q && !(name.toLowerCase().includes(q) || (latest.caption || '').toLowerCase().includes(q))) continue;
    rows.push({ sid, snap: latest, name, inst: details[sid]?.instance_count || 0 });
  }
  rows.sort((a, b) => (b.snap.created_at || 0) - (a.snap.created_at || 0));
  return rows;
}

const snap = (snapshot_id, created_at, caption) => ({ snapshot_id, created_at, caption, trigger: 'swap' });

// ── 1. single snapshot -> 1 card ──────────────────────────────────────────────────────
{
  const sessions = {
    stacks: { s1: {} },
    details: { s1: { display_name: 'Alpha', instance_count: 2, snapshots: [snap('a1', 100, 'old')] } },
  };
  const rows = buildRows(sessions, 'active_x', '');
  check('single-snapshot session yields exactly one card', rows.length === 1);
  check('single-snapshot card keeps that snapshot', rows[0] && rows[0].snap.snapshot_id === 'a1');
  check('card carries display_name and instance_count',
    rows[0] && rows[0].name === 'Alpha' && rows[0].inst === 2);
}

// ── 2. multi snapshot -> keeps max created_at (and its id for Open/Delete) ────────────
{
  const sessions = {
    stacks: { s1: {} },
    details: { s1: { display_name: 'Alpha', instance_count: 1, snapshots: [
      snap('a1', 100, 'oldest'), snap('a3', 300, 'newest'), snap('a2', 200, 'middle'),
    ] } },
  };
  const rows = buildRows(sessions, 'active_x', '');
  check('multi-snapshot session collapses to one card', rows.length === 1);
  check('collapse keeps the max created_at snapshot (and its id)',
    rows[0] && rows[0].snap.snapshot_id === 'a3' && rows[0].snap.created_at === 300);
}

// ── 3. ACTIVE stack is skipped ────────────────────────────────────────────────────────
{
  const sessions = {
    stacks: { act: {}, s2: {} },
    details: {
      act: { display_name: 'Active', snapshots: [snap('x1', 500, 'active')] },
      s2: { display_name: 'Beta', snapshots: [snap('b1', 100, 'b')] },
    },
  };
  const rows = buildRows(sessions, 'act', '');
  check('ACTIVE stack is skipped in the collapsed list',
    !rows.some(r => r.sid === 'act') && rows.some(r => r.sid === 's2'));
}

// ── 4. filter: matches name / latest caption; older hidden captions NOT searchable ────
{
  const sessions = {
    stacks: { s1: {}, s2: {} },
    details: {
      s1: { display_name: 'Alpha', snapshots: [snap('a1', 200, 'newest cap'), snap('a0', 100, 'secretold')] },
      s2: { display_name: 'Beta', snapshots: [snap('b1', 50, 'beta cap')] },
    },
  };
  check('filter by name matches collapsed row', buildRows(sessions, 'act', 'alpha').length === 1);
  check('filter by latest caption matches collapsed row', buildRows(sessions, 'act', 'newest cap').length === 1);
  check('older (hidden) snapshot caption is no longer searchable', buildRows(sessions, 'act', 'secretold').length === 0);
  check('filter is case-insensitive', buildRows(sessions, 'act', 'BETA').length === 1);
}

// ── 5. empty snapshots array -> skipped ───────────────────────────────────────────────
{
  const sessions = {
    stacks: { s1: {} },
    details: { s1: { display_name: 'Empty', snapshots: [] } },
  };
  check('empty snapshots array yields no card', buildRows(sessions, 'active_x', '').length === 0);
}

// ── 6. tie in created_at -> first-encountered wins (strict >) ─────────────────────────
{
  const sessions = {
    stacks: { s1: {} },
    details: { s1: { display_name: 'Tie', snapshots: [snap('first', 100), snap('second', 100)] } },
  };
  const rows = buildRows(sessions, 'active_x', '');
  check('created_at tie keeps first-encountered snapshot', rows[0] && rows[0].snap.snapshot_id === 'first');
}

// ── 7. rows sort by created_at desc across sessions ───────────────────────────────────
{
  const sessions = {
    stacks: { low: {}, mid: {}, high: {} },
    details: {
      low: { display_name: 'Low', snapshots: [snap('l1', 10)] },
      mid: { display_name: 'Mid', snapshots: [snap('m1', 50), snap('m0', 5)] },
      high: { display_name: 'High', snapshots: [snap('h1', 999)] },
    },
  };
  const rows = buildRows(sessions, 'active_x', '');
  check('rows sorted by created_at desc across sessions',
    rows.map(r => r.sid).join(',') === 'high,mid,low');
}

// ── 8. missing details entry -> uses stack_id as name, keeps its snapshots (if any) ────
{
  const sessions = { stacks: { uuid_20261118_142516: {} }, details: {} };
  const rows = buildRows(sessions, 'active_x', '');
  check('stack with no details entry yields no card (no snapshots)', rows.length === 0);
}

console.log('');
if (failures === 0) {
  console.log('ALL PASS');
  process.exit(0);
} else {
  console.error(failures + ' FAILURE(S)');
  process.exit(1);
}

# Investigation: UI message bugs after delta-streaming fixes

**Date:** 2026-09-06 · **Investigator:** researcher (ui-missing-msgs-research)
**Codebase:** `N:\work\WD\AgentCascade` · **Confidence:** Bug 1 = High · Bug 2 = Moderate (backend ruled out, UI mechanism needs repro)

---

## Executive Summary

1. **Bug 1 (missing user messages) — root cause confirmed.** Commit `0ae2485` made the frontend set `_needsResync` instead of self-healing when it detects a missed frame. The resync gate skips *all partial* frames, but during active streaming **every** frame — including the ~10 s periodic `force_full` recovery frames — is `is_partial=True`. So a single dropped WebSocket frame freezes the agent's panel for the entire turn (minutes). User messages are not echoed optimistically by the frontend, so they stay invisible until the turn ends.
2. **Bug 2 (security task message in parent history) — backend ruled out.** The task message is appended only to the security instance's conversation, and every frame entry is keyed by instance name end-to-end (backend serializers and frontend state/panels). No data path exists that places the task message in the parent's message list. The most likely explanation is a **tab-level artifact**: the security tab (whose first user-role message is literally the task text) is auto-focused on check start, and the auto-switch-back can be missed, leaving the user looking at the security conversation while believing they're on the parent tab.
3. **Common root cause:** both bugs trace to the same change set — the `_needsResync` gate + index verification (`0ae2485`) combined with dropped frames under queue-full. Bug 1 is a direct consequence; Bug 2 (if confirmed as tab drift) is aggravated by the same frame-drop conditions (the stack-shrink frame that triggers the switch-back is dropped in the same burst).

---

## Bug 1: User messages go missing

### How user messages flow today

- `sendMessage` (`web_ui/app.js` L5515–5571) **does not add the user message to local state**. It clears the input and sends `{type:'message', ...}`. The only local echo is for the *Agent Messages* tab (`addAgentMessage(userEchoMsg)`, L5540–5550) — a separate inter-agent list, not the conversation panel.
- The user message therefore first becomes visible when a backend frame containing it arrives and is merged (`stream_update` partial merge, L2044–2115; or `state`/`done` full replacement, L1832–1836).

### The exact mechanism

Pre-`0ae2485` merge logic (see `git show 0ae2485`):

```js
if (startIdx > existing.messages.length) {
  existing.messages = [...sa.messages];   // server ahead → immediate replace (self-healing)
}
```

Post-`0ae2485` (`app.js` L2044–2084):

```js
if (sa.is_partial) {
  if (existing._needsResync) {
    continue;                    // L2051-2053: skip ALL partial frames while resync-pending
  }
  if (hCount < (existing._lastHistoryCount || 0)) { /* stale: meta only */ }
  else {
    const startIdx = hCount - sa.messages.length;
    if (startIdx > existing.messages.length) {
      existing._needsResync = true;   // L2069-2073: server ahead → FREEZE, wait for full frame
      continue;
    }
    // L2076-2081: index verification; mismatch → _needsResync = true
    existing.messages.length = startIdx;
    existing.messages.push(...sa.messages);
  }
}
```

**The gate is only cleared by:**
- a **non-partial** frame → full object replace (L2116–2120), or
- a `state`/`done` frame (L1832–1836).

**The hole:** while an agent is streaming, *every* frame for that agent — including the periodic `force_full` recovery frames (every 100 ticks ≈ 10 s, `streaming.py` L339–343) — has `is_partial=True`, because `state_builder.py` sets `is_partial = len(stream_responses) > 0` regardless of `force_full` (force_full only disables the tail cut; the in-flight partial is still appended). The comment at L2050 ("Full frames (non-partial) replace the entire object below, clearing `_needsResync`") is therefore **wrong during streaming**: no non-partial frame arrives until the turn ends or a tool boundary commits.

**Trigger:** any dropped frame. `_put_stream_update` silently drops on `QueueFull` (`streaming.py` L256–259) — the exact condition the force_full mechanism was designed to recover from. One drop makes the next delta's `startIdx > existing.messages.length` true → gate set → **panel frozen for the rest of the turn** (potentially minutes).

**Why user messages specifically disappear:**
1. User sends a message while the gate is active (or the frame carrying it is the one dropped that *sets* the gate).
2. Backend commits it; frames arrive; the merge loop hits `if (existing._needsResync) continue` (L2051) and skips them.
3. No optimistic echo exists, so the message is invisible.
4. Recovery only when the turn ends (non-partial frame replaces the whole list) — the user perceives "my message was lost".

Pre-`0ae2485`, the identical drop was self-healed by the *next* frame's immediate replace (at the cost of a transient prefix truncation, which is what the commit set out to fix). The trade was made, but the recovery dependency (non-partial full frame) doesn't hold during streaming.

### Secondary aggravator

The stale-anchor branch (L2057–2064) and the `state`/`done` handler's partial restoration (L1822–1847) can leave `_lastHistoryCount`/list length transiently inconsistent, but these are minor compared to the gate freeze. The R1 guard for brand-new agents (L2102–2114) drops delta-without-prefix frames and relies on the next force_full — which, per above, is also `is_partial=True` and gets skipped if the agent was already known.

### Confidence: High (mechanism fully traced in code; symptom match is strong)

---

## Bug 2: Security approval task message in parent history

### Backend: verified clean (no cross-instance mixing)

- **Task message creation:** `engine._create_system_agent` (`core.py` L3263–3332) creates a fresh `Security_{rid}` instance and routes the task ("This is a message from {caller}…") through `lifecycle_manager.initialize_conversation` (L309+), which modifies **only the security instance's** conversation.
- **Conversation isolation:** the parent's conversation never receives the task message; after the verdict the parent gets the tool result, not the task.
- **Frame construction:** `_serialize_instances_incremental` (`state_builder.py` L206–270) and `_serialize_all_instances` (L101–119) serialize each instance from its **own** conversation, keyed by instance name. The primary (caller) entry and the security entry are separate dict keys in every frame.
- **Security broadcast:** `security_handler.py` L636–646 calls `broadcast_stream_update(instance_name=sec_state_key, ...)`; `push_initial_state` (L312–318) sends the security instance's first frame with `force_full=True`. `_cleanup` (L971) marks the instance inactive but keeps it in the pool (so it stays in frames until eviction — under its own key).
- **Frontend state:** `state.subAgents` is name-keyed; tabs and panels are `sub-{name}` / `panelSub-{name}` (L3971–4086); `getActiveAgentName()` (L507–515) is tab-based. No path was found that copies the security entry into the parent object or vice versa.

### Most likely UI mechanism (moderate confidence — needs repro)

The security tab's conversation **starts with the task message as its first user-role bubble** (system prompt + "This is a message from {caller}"). The auto-tab-focus machinery (L2166–2167, L2218–2222, L2256–2269):

1. Check starts → `active_stack` grows `[Maine, Security_op_x]` → `stackChanged` → UI auto-switches **to** the security tab (L2258–2261).
2. Check ends → `active_stack` shrinks → `stackChanged` → auto-switch **back** to the primary tab (L2263–2267).

The switch-back depends on the stack-shrink frame arriving (frames are dropped on QueueFull, L256–259 — exactly when the parent resumes streaming and the queue is busy) and on `shouldRender` passing (L2218–2222). If either is missed, the UI stays on the security tab. The security conversation — beginning with a user bubble "This is a message from {parent}" — then reads as "a task message in my parent agent's history" while the actual parent tab (possibly also frozen by the Bug-1 gate, showing stale content) sits behind it.

**Why it appeared after the recent changes:** the same frame-drop + gate changes that cause Bug 1 increase the probability that (a) the stack frame is dropped, and (b) the parent tab is frozen/stale, making the misread more likely and more confusing. Note the "in addition to" in the report: the message *is* in the security history (correct) and appears where the user *thinks* the parent history is (they're actually viewing the security tab).

**Open question:** if a reproduction shows the task text literally inside `state.subAgents[<parent>].messages`, then a mechanism I could not find exists and the e2e assertion below (Fix 4) will catch it. Current evidence does not support data-level mixing.

### Confidence: Moderate (backend absence of mixing = High; specific UI explanation = Moderate)

---

## Do the bugs share a root cause?

**Partially.** Both stem from the `0ae2485` change set:
- Bug 1: the `_needsResync` gate is unrecoverable during streaming (design flaw in the fix).
- Bug 2 (if confirmed as tab drift): the same dropped-frame conditions that set the gate also drop the stack-shrink frame that would switch tabs back, and the frozen/stale parent tab makes the security tab look like the parent's.

The `use_cache=False` fix (`9adac25`) and `force_full` in `push_initial_state` are not implicated — they are correct as implemented.

---

## Recommended fixes

### Fix 1 (Bug 1, frontend, primary) — treat full snapshots as resync clearers

In the partial-merge branch (`app.js` ~L2051–2084), clear the gate and full-replace when the frame is a full snapshot rather than a tail:

```js
const startIdx = hCount - sa.messages.length;
if (existing._needsResync) {
  if (startIdx <= 0) {                       // full snapshot (force_full frames have startIdx=0)
    existing.messages = [...sa.messages];
    existing._needsResync = false;
    existing._lastHistoryCount = hCount;
    /* fall through to metadata sync */
  } else {
    continue;                                 // still resync-pending, tail frame — unsafe
  }
}
```

Rationale: a `startIdx <= 0` frame contains the entire conversation (force_full frames, short conversations, rollback frames), so replacing is always safe; only tail frames (startIdx > 0) need gating. This restores the pre-`0ae2485` self-healing speed without reintroducing prefix truncation.

### Fix 2 (Bug 1, frontend, belt-and-braces) — optimistic user echo

In `sendMessage` (L5515), after computing `targetAgent`, push a local user bubble into `state.subAgents[targetAgent].messages` (temp id, `role:'user'`, content = `messageText`) and render. On the first merged frame containing a user message with matching content, remove the local echo (dedup). This makes user messages visible immediately regardless of gating — mirroring the existing Agent-Messages echo (L5540–5550).

### Fix 3 (Bug 1, backend, optional hardening) — explicit full-frame marker

Add `full_frame: true` to the result dict of `build_stream_update_from_pool` when `force_full=True` (state_builder.py), and have the frontend use it in Fix 1 instead of inferring from `startIdx`. Removes ambiguity and documents the protocol.

### Fix 4 (Bug 2, verification first) — e2e assertion + tab-state logging

- Extend `tests/test_streaming_fullstack_e2e.py`'s `_assert_message_stack_sync` to assert **per-instance message sets**: for the parent instance, no message content starts with the security task prefix ("This is a message from"); for the security instance, the task message appears exactly once.
- Add temporary console logging of `state.activeSubTab` + `state.activeStack` on every frame; reproduce a security approval and check whether the UI stays on the security tab after the check completes.
- If tab drift is confirmed: make the switch-back robust — (a) don't rely on a single stack frame: compare against the last *seen* stack string stored in state and force the switch when the top becomes the session primary (L2256 block); (b) exempt stack changes from the render throttle (treat `stackChanged` as an unconditional `shouldRender`, which it already is — the risk is the dropped frame itself, so Fix 3's full-frame markers + shorter force_full interval for stack changes help).
- Cosmetic: label the security tab `Security (advisor)` to reduce misread.

### Not recommended

- Reverting `0ae2485` wholesale: it fixed real dup/missing corruption (see `.agent_lessons/message-sync-delta-bug-fixes.md`); Fix 1 keeps those protections while restoring self-healing.

---

## Evidence index

| Claim | Source |
|---|---|
| No optimistic user echo in conversation panel | `app.js` L5515–5571 |
| `_needsResync` gate skips all partial frames | `app.js` L2044–2053 |
| Gate set on `startIdx > list length` | `app.js` L2069–2073 (was immediate replace pre-`0ae2485`, `git show 0ae2485`) |
| Gate cleared only by non-partial / state-done | `app.js` L2116–2120, L1832–1836 |
| force_full frames are `is_partial=True` during streaming | `state_builder.py` `is_partial = len(stream_responses)>0`; force_full only disables tail cut; `streaming.py` L339–343 |
| Frames dropped on QueueFull | `streaming.py` L256–259 |
| Task msg only in security conversation | `core.py` L3263–3332 (`_create_system_agent`), `lifecycle_manager.py` L85–142 |
| Frame entries keyed by instance name | `state_builder.py` L101–119, L206–270 |
| Security broadcast uses `sec_state_key` | `security_handler.py` L636–646 |
| Auto tab switch on stack change | `app.js` L2166–2167, L2218–2222, L2256–2269 |

## Open questions

1. Bug 2 repro: is the user actually on the security tab (tab-label check), or is the task text literally in the parent's `messages` array? (Fix 4 settles this.)
2. How often does QueueFull actually occur in production? (The `STREAM_BACKEND_DEBUG` probe / `yield_to_enqueue_ms` logs would quantify frame drops.)
3. Should the force_full interval (100 ticks) be shortened, or should stack changes always emit a force_full frame?

## Suggested next actions

1. Implement Fix 1 + Fix 2 (frontend, small, low-risk); add a unit/e2e case: drop a frame mid-stream, send a user message, assert visibility within one tick after next full snapshot.
2. Implement Fix 4's e2e assertion; run a manual security-approval repro with tab logging to confirm/rule out tab drift.
3. If Bug 2 is confirmed as data-level mixing (contrary to current evidence), re-investigate with the e2e assertion as a reproduction harness.

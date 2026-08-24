# `__wait` wake-up contract — v2 (user-directed refinement)

**Date:** 2026-08-24 · **Supersedes:** the "consume only matching shell message, skip non-matching" behavior in `async_shell_wait_heartbeat_fix_plan.md`.
**Driver:** User feedback — `__wait` should wake on ANY queued message; it only intercepts *its own* tool's shell message when that is at the front of the queue.

## Contract (authoritative)

When an agent issues `shell_cmd(command="__wait", tool_id=N)`:

1. **Block** until there is **any** message in this agent's message queue (not only shell messages). Wake on user messages, system/termination feedback, security prompts, other tools' heartbeats, async results — anything.
2. On wake, inspect the **front of the queue** (`message_queues[instance][0]`):
   - **If it is this tool_id's heartbeat/completion** (starts with `⟨shell_cmd` AND matches exact tool_id via the non-digit-boundary regex) → **consume it** and return it **verbatim** as the tool result. (Tracker already advanced `last_heartbeat_sent_pos`, so no duplication.)
   - **Otherwise** → return a **default wake-up string** (see below) and leave the queue **untouched**.
3. The normal drain path (`engine/core.py:2320` `drain_queue`) then pulls **all** remaining messages **in sequence** (user, heartbeat, …) one after another and injects them as USER messages — exactly as it does today. No reordering, no special-casing.

### Consequences
- A user message that arrived **before** the heartbeat preempts: `__wait` returns the default wake-up, and the drain delivers the user msg then the heartbeat in order.
- If only this tool's shell message is queued, it's consumed+returned verbatim (the original RC1/RC2/RC3 fix is preserved).
- A different tool's shell message at the front → default wake-up; that other tool's `__wait` will later consume it when it reaches the front.

## Default wake-up string
Keep the existing prefix style so tests/UI are consistent:
```
⟨shell_cmd wait⟩ Tool ID: {tool_id} - Woken by queued message (not this shell). Check your message queue.
```
(Exact wording is flexible; keep `⟨shell_cmd wait⟩` + `Tool ID: {tool_id}` so existing substring assertions still hold. Do NOT reuse the "No new output (timeout after Xs…)" string for this case — that one is reserved for genuine timeout with an empty queue.)

## Implementation notes

### Change to `wait_for_message` (message_queue.py)
The current predicate form *skips* non-matching messages and keeps waiting for a match. That is no longer what we want. We need: **wake on ANY message; tell the caller whether the front message matched.**

Preferred: add a mode that returns the front message WITHOUT consuming it when it doesn't match, i.e. a "peek-and-decide" primitive. Two clean options — pick the simplest that keeps lock discipline:

**Option 1 (recommended) — `wait_for_message(..., consume_predicate=...)`:**
- Block until queue non-empty OR timeout OR terminated.
- When a message is available, look at `msgs[0]`:
  - if `consume_predicate is None` → pop(0) and return it (unchanged default behavior).
  - else if `consume_predicate(msgs[0])` is True → pop(0) and return it.
  - else → **return the front message WITHOUT popping it** (peek), so the caller can decide. The queue is left intact for the normal drain.
- Caller (`__wait`) then: if the returned text matches its own tool's shell predicate → it was already consumed, return verbatim; else → it was only peeked (still queued), return the default wake-up string.

To let the caller distinguish "consumed" vs "peeked", either:
- (a) have `__wait` re-check: after `wait_for_message` returns a non-None value, test `_is_our_shell_msg(value)`; if True it was consumed (return verbatim), if False it was peeked (return default string). This works because the front message is exactly what was tested. **Simplest — no new return type.**
- (b) return a small tuple `(message, consumed: bool)`. More explicit but changes the signature/contract more.

**Go with (a).** It reuses the existing single-string return and keeps the diff minimal.

Lock discipline: all of this stays inside `with self._message_condition:` (holds `_queue_lock`). The peek must NOT mutate the list. Termination check unchanged (return None). Timeout on empty queue → return None (caller returns the existing "No new output (timeout after Xs…)" string — that path is unchanged).

### Change to `__wait` branch (shell_cmd.py)
Replace the current predicate-skip call with:
```python
if _has_real_wait_for_message(pool):
    msg = pool.wait_for_message(agent_name, timeout, consume_predicate=_is_our_shell_msg)
    if msg is None:
        # Genuine timeout / terminated — empty queue. Existing string (unchanged).
        task_elapsed = _elapsed_for_task(task)
        return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
                f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s).")
    if _is_our_shell_msg(msg):
        # Front message was THIS tool's shell msg → already consumed by the primitive.
        return str(msg)
    # Front message is something else (user/system/other-tool) → it was only peeked,
    # still queued. Return default wake-up; normal drain delivers it in sequence.
    return f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Woken by queued message (not this shell). Check your message queue."
```
Keep the fallback to `_polling_wait(...)` when the guard is False (MagicMock pools in existing tests) — unchanged.

NOTE on double-check: calling `_is_our_shell_msg(msg)` again after `wait_for_message` is safe and cheap; it's the same predicate used as `consume_predicate`, so "consumed" ⇔ "predicate True on the front msg".

## Test changes (tests/test_async_shell_cmd.py)
- **Rewrite** `test_wait_does_not_swallow_non_shell_messages`: now the user msg at the front → `__wait` returns the DEFAULT wake-up string (NOT the shell msg), and BOTH the user msg AND the shell msg remain queued in order for the drain. Assert: result == default string; queue still contains user_msg then shell_msg, in that order.
- **Add** `test_wait_user_and_heartbeat_drain_in_sequence`: enqueue user msg then this tool's heartbeat; `__wait` returns default wake-up; assert both remain queued in original order (proves "pop one after another as usual" is preserved).
- **Keep/adjust** `test_wait_consumes_already_queued_heartbeat`: when ONLY the shell msg is at the front, it's consumed+returned verbatim (unchanged behavior).
- **Keep** `test_wait_predicate_does_not_match_longer_tool_id` (collision fix) — but note: with front-of-queue semantics, if a `Tool ID: 12` msg is at the FRONT and we `__wait(1)`, it's NOT our msg → default wake-up, 12 stays queued. Update the test to reflect front-of-queue ordering (enqueue 12 first, then 1; `__wait(1)` should now return default string because 12 is at front, leaving both queued — OR enqueue 1 first so it's consumed). Decide and assert precisely; the key invariant is the exact-id boundary still holds.
- **Keep** `test_wait_timeout_on_empty_new_path` (empty queue → timeout string) unchanged.
- The `_FakePoolWithWait` helper must expose the new peek behavior (it already inherits `wait_for_message`; just ensure the fake's `message_queues` is a real dict so front-inspection works).

## Invariants that MUST hold after the change
1. Empty queue + timeout → existing "No new output (timeout after Xs…)" string (unchanged).
2. This tool's shell msg at front → consumed + returned verbatim, no duplication (RC3 preserved).
3. Any non-matching msg at front → default wake-up string, queue left intact, normal drain delivers all in sequence.
4. Exact tool_id boundary (non-digit lookahead) still prevents 1-vs-12 collisions.
5. Lock discipline: everything under `_message_condition`/`_queue_lock`; peek never mutates; no `task._lock` held across the wait.
6. MagicMock pools still fall to `_polling_wait` (existing timeout/cap/lock tests stay green).

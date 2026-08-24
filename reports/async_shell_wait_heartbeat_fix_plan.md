# Fix Plan: `__wait` must wait for a heartbeat in the message queue (todo.md:135)

**Status:** Plan (for review) · **Author:** Maine · **Date:** 2026-08-24
**Investigation source:** `reports/async_shell_wait_heartbeat_investigation.md` (CONFIRMED, independently reviewed PASS)
**Memory:** `.agent_lessons/async-shell-wait-queue-blind-polling.md`

---

## 1. Problem (confirmed root causes)

`__wait` (`agent_cascade/tools/custom/shell_cmd.py:399-480`) is documented as "wait until next heartbeat" but instead **busy-polls raw `task.stdout_lines/stderr_lines` every 0.5 s and returns immediately on ANY new line**. It never consults the message queue.

- **RC1** — wrong wakeup predicate: return-on-any-new-line (shell_cmd.py:474) instead of wake-on-next-heartbeat/queue-event.
- **RC2** — queue blindness: already-enqueued heartbeats are invisible; `MessageQueueMixin.wait_for_message()` (message_queue.py:135-175) is dead code with a docstring that falsely claims `__wait` uses it.
- **RC3** — output duplication: `__wait` reads buffers without advancing `task.last_heartbeat_sent_pos`, so the later heartbeat/completion message re-sends the same lines (proven in production log).
- **RC4** — cleanup race: follow-up `__wait` returns misleading "No running shell found" just before the completion USER message arrives.

## 2. Goal

Make `__wait` a **true queue-driven sleep**: block until the next *shell* message (heartbeat or completion) for this specific `tool_id` is enqueued, return it verbatim, and stop duplicating output. Non-shell messages in the shared queue must NOT be swallowed.

## 3. Changes

### Change A — `agent_cascade/pool/message_queue.py`: add predicate support to `wait_for_message`

Extend the existing method (do NOT create a parallel one):

```python
def wait_for_message(self, instance_name: str, timeout: float = 30.0,
                     predicate=None) -> Optional[str]:
    """Block until a message matching `predicate` is available for this instance, or timeout.

    When `predicate` is None (default), behavior is unchanged: pop and return the first
    queued message. When `predicate` is provided, only messages for which
    `predicate(msg) is True` are consumed; non-matching messages remain queued in order.

    Used by the shell_cmd `__wait` tool to pause execution until the next async shell
    message (heartbeat or completion) for a specific tool_id arrives, without consuming
    unrelated supervisor/user messages that share the same queue.

    Returns None if dismissed/terminated or if the timeout elapses with no matching message.
    """
```

**Loop logic (reuse existing skeleton at :152-175):**
- Keep `with self._message_condition:` and the termination check (`is_instance_terminated` → return None) exactly as-is.
- When `predicate is None`: keep current behavior — pop(0) first message, return it.
- When `predicate is not None`:
  - Scan the queue list for the **first index** where `predicate(msg)` is True.
  - If found: `return msgs.pop(index)` (consume only that one; leave others in order).
  - If not found but queue non-empty: do NOT consume; fall through to wait.
  - Deadline/timeout handling unchanged (1.0 s slices, cleanup of empty list on timeout, return None).

**Lock discipline (per review):** `self._message_condition` is a `threading.Condition(self._queue_lock)` (constructed in core.py), so entering `with self._message_condition:` acquires the underlying `_queue_lock`. The predicate scan + `msgs.pop(index)` must be performed **inside the `with self._message_condition:` block**, exactly matching how the existing code at :152-175 reads and pops from `self.message_queues` under that same lock. Do NOT introduce a second lock, and do NOT read `self.message_queues` outside that block.

### Change B — `agent_cascade/tools/custom/shell_cmd.py`: rewrite the `__wait` branch (:399-480)

Keep unchanged:
- The `_get_task` None check (but soften message per RC4 below).
- The completed/return_code early-return block (:409-418).
- The timeout math (:420-427): `hb>0 → min(hb_interval, WAIT_CMD_MAX_TIMEOUT)` else `WAIT_CMD_DEFAULT_TIMEOUT`.

Replace the busy-poll loop (:436-480) with a queue-driven wait:

```python
pool = self.agent_pool
# Predicate: only THIS tool's shell messages (heartbeat or completion).
# Both formats start with "⟨shell_cmd" and contain "Tool ID: {tool_id}".
def _is_our_shell_msg(m):
    try:
        m = str(m)
    except Exception:
        return False
    return m.startswith('⟨shell_cmd') and f'Tool ID: {tool_id}' in m

if _has_real_wait_for_message(pool):
    msg = pool.wait_for_message(agent_name, timeout, predicate=_is_our_shell_msg)
    if msg is not None:
        return str(msg)          # tracker already advanced last_heartbeat_sent_pos → no duplication
    # Timeout (or terminated): preserve existing string format.
    task_elapsed = _elapsed_for_task(task)
    return (
        f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
        f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s)."
    )

# Fallback: pool lacks a real wait_for_message (e.g. MagicMock in unit tests).
# Delegate to an extracted helper so the polling logic is NOT duplicated inline.
return _polling_wait(task, tool_id, agent_name, timeout, self.agent_pool)
```

**Extract the polling loop into `_polling_wait(...)` (per review #3):** move the existing busy-poll body (:436-480) verbatim into a module-level or method helper `_polling_wait(task, tool_id, agent_name, timeout, pool) -> str` so it is not duplicated inline. The new `__wait` branch calls it only on the fallback path. This keeps the diff clean and avoids two copies of the polling logic drifting apart.

**`_has_real_wait_for_message(pool)` helper** (define at module scope or inline): must reject mock objects. Use the `isinstance` form (preferred — future-proof, per review):
```python
from agent_cascade.pool.message_queue import MessageQueueMixin

def _has_real_wait_for_message(pool):
    """True only when pool is a real MessageQueueMixin with a working wait_for_message."""
    return isinstance(pool, MessageQueueMixin) and callable(getattr(pool, 'wait_for_message', None))
```
Rationale: in tests `self.agent_pool` is a bare `MagicMock`, so `hasattr(pool, 'wait_for_message')` is True and calling it returns a truthy MagicMock — that would break the timeout/cap tests. The `isinstance` check forces those tests down the polling fallback path. (Fallback if importing `MessageQueueMixin` into shell_cmd.py causes a circular import: use `type(pool).__name__ not in ('MagicMock','AsyncMock','Mock') and callable(...)` — but prefer isinstance; verify no circular import first.)

**RC4 softening:** when `_get_task` returns None, change the message to:
`f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No running shell found (may have just completed — watch for the completion message)."`
Keep the substring "No running shell found" so `test_wait_no_running_shell` still passes.

**Do NOT call `wait_for_message` while holding `task._lock`.** The current code releases all task locks before the poll loop; keep it that way (the queue wait acquires `_message_condition`, a different lock).

### Change C — docstring cleanup
- Update `wait_for_message` docstring (done in Change A) to name the real caller.
- No other doc drift expected.

## 4. Test plan (`tests/test_async_shell_cmd.py`)

**Test helper (per review #4):** the new queue-driven path needs a pool with a *real* `wait_for_message` + `is_instance_terminated` + `_message_condition`. Add a lightweight fake in the test file:
```python
class _FakePoolWithWait(MessageQueueMixin):
    def __init__(self):
        self.message_queues = {}
        self._queue_lock = threading.Lock()
        self._message_condition = threading.Condition(self._queue_lock)
    def is_instance_terminated(self, instance_name):
        return False
    def _mark_activity(self, instance_name):   # called by enqueue_message (message_queue.py:67)
        pass
```
`MessageQueueMixin` has **no `__init__`** (verified), so this subclass fully covers what `wait_for_message` + `enqueue_message` touch: `_message_condition`, `message_queues`, `is_instance_terminated`, and `_mark_activity`. Wire this fake as `shell_cmd_tool.agent_pool` (alongside `_async_shell_tracker`) in the rewritten/new queue-driven tests.

**Rewrite (encodes buggy semantics):**
- `test_wait_returns_new_output` (:209-228): currently asserts `'new output line' in result`. Replace with a **queue-driven test** using `_FakePoolWithWait`: enqueue a heartbeat message formatted like the tracker's (`⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | ...`) and assert `__wait` returns it verbatim (contains "⟨shell_cmd heartbeat⟩" and "Tool ID: 1"), NOT raw stdout lines.

**Add:**
- **Consumes already-queued heartbeat:** enqueue a matching shell message *before* calling `__wait`; assert it is returned immediately (no full timeout wait) — this is the core bug fix (RC2).
- **Does not swallow non-shell messages:** enqueue a supervisor/user message (e.g. "hello from user") and a matching shell message; assert `__wait` returns only the shell one and the user message remains in the queue (`pool.get_queue_messages(agent_name)` still contains it) — predicate correctness.
- **No duplication (RC3):** after `__wait` consumes a heartbeat, simulate the tracker's position advancement and assert the subsequent completion/heartbeat does not re-send lines already returned. (Can be a focused unit on `_send_heartbeat` + `wait_for_message` interaction, or an integration-style test with a real tracker + pool.)
- **Predicate leaves other tool_id queued:** enqueue shell messages for tool_id 1 and tool_id 2; `__wait(tool_id=1)` returns only the tool_id-1 one.

**Keep (must still pass via fallback path):**
- `test_wait_no_running_shell` (:191) — soften message but keep "No running shell found".
- `test_wait_already_completed` (:197).
- `test_wait_returns_completion_status` (:230) — uses MagicMock pool → falls to polling loop; keep as-is (validates fallback).
- `test_wait_returns_timeout_when_no_output` (:252), `test_wait_respects_timeout_cap_at_180s` (:263), `test_wait_uses_heartbeat_interval_below_cap` (:276), `test_wait_heartbeat_at_exact_cap` (:290), `test_wait_no_deadlock_on_sequential_access` (:305), `test_wait_proper_lock_handling` (:313) — all use MagicMock pool → fallback polling path; must remain green.

**Note on fake time:** the new queue-driven path uses a condition variable + real `time.time()` inside `wait_for_message`. For the "consumes already-queued" test, no fake time is needed (message is pre-queued → returns immediately). For timeout-on-empty tests under the new path, either use a short real timeout or patch `time` — but prefer keeping timeout/cap coverage on the fallback path to avoid flakiness.

## 5. Risks / edge cases
- **Shared queue:** supervisor `send_message` and user messages share `message_queues[instance_name]`. Predicate is mandatory (Change B). Verified both heartbeat (`⟨shell_cmd heartbeat⟩ ... Tool ID: {id}`) and completion (`⟨shell_cmd completed⟩ Tool ID: {id}`) formats contain the marker.
- **Concurrent shells, one owner:** predicate includes tool_id → `__wait(X)` never consumes `Y`'s message. (Decision: per report §5, do NOT allow cross-tool consumption.)
- **Completion-only tasks (`heartbeat_interval=-1`):** completion message wakes the wait; timeout falls back to `WAIT_CMD_DEFAULT_TIMEOUT=30`. Works.
- **Terminated/dismissed instance:** `wait_for_message` returns None on termination (message_queue.py:157-158) → `__wait` returns the timeout string promptly; no hang. Add a quick test if feasible.
- **Lock discipline:** never hold `task._lock` across the queue wait. Verified current code releases it first.
- **MagicMock pools in tests:** handled by `_is_real_method` guard → fallback path. This is the single most likely source of test breakage; the guard is deliberate.
- **Running app must be restarted** to pick up changes (no auto-reload) — per todo.md:129 note.

## 6. Acceptance criteria
1. `__wait` blocks until a *matching* shell message is enqueued and returns it verbatim; does not return on arbitrary new stdout lines.
2. An already-queued heartbeat is consumed immediately (the original complaint).
3. Non-shell / other-tool_id messages are left in the queue untouched.
4. No output duplication between `__wait`'s reply and the subsequent heartbeat/completion.
5. All pre-existing `__wait` tests pass (via fallback where they use MagicMock), plus new tests green.
6. Full async-shell suite passes; no regressions elsewhere.

## 7. Out of scope
- Changing timeout values (180s cap etc.) — correct, leave alone.
- UI rendering of "(not on any new line…)" — the log evidence attributes it to RC1/RC3; fixing those resolves it. No frontend change planned.

# Review: async_shell_wait_heartbeat_fix_plan.md

**Date:** 2026-08-24  
**Reviewer:** rev_wait_plan (Senior QA & Review Specialist)  
**Plan author:** Maine  
**Investigation source:** `reports/async_shell_wait_heartbeat_investigation.md` (CONFIRMED, PASS)

---

## Summary Verdict

The implementation plan is **substantively correct** and addresses the root causes identified in the investigation report. However, there are **three corrections** needed before implementation: one major clarity issue on lock discipline, one minor improvement to the mock guard, and a note on test rewrite feasibility. No critical blockers exist.

**Overall verdict:** **NEEDS WORK** – apply the corrections below, then proceed.

---

## Detailed Findings

### 1. Lock Relationship (Change A)

**Status:** 🟠 Major – Needs precise wording  
**Location:** Plan line 54; `agent_cascade/pool/message_queue.py` lines 59, 64, 152-175

**Finding:** The plan states:

> "the predicate scan must happen inside `self._message_condition` (i.e., while holding `_queue_lock` — see note below on lock relationship)."

This phrasing is ambiguous because `self._message_condition` is a `threading.Condition`, not a lock. However, the condition is constructed with `_queue_lock` as its underlying lock (`_message_condition = threading.Condition(self._queue_lock)` in `core.py`). Therefore, entering `with self._message_condition:` does acquire `_queue_lock`. The existing `wait_for_message` at line 152 correctly holds this lock while reading and popping from `self.message_queues`.

**Correction:** Replace the ambiguous phrasing with:

> "Perform the predicate scan inside the `with self._message_condition:` block (which acquires the underlying `_queue_lock`). This matches the lock discipline used by existing `wait_for_message` at line 152."

No functional change is required; only wording precision to avoid future confusion.

---

### 2. MagicMock Guard (Change B)

**Status:** 🟡 Minor – Improvement recommended  
**Location:** Plan lines 94-98; `tests/test_async_shell_cmd.py` lines 50-56

**Finding:** The guard `_is_real_method(x)` is defined as:

```python
def _is_real_method(x):
    return callable(x) and not type(x).__name__ in ('MagicMock', 'AsyncMock')
```

The current tests use `MagicMock`. However, to be robust against any mock type (e.g., plain `unittest.mock.Mock`), the guard should also exclude `'Mock'`. Additionally, a more idiomatic approach would be to check `isinstance(pool, MessageQueueMixin)`, but the explicit type-name check is acceptable if we broaden it.

**Correction:** Expand the exclusion list:

```python
def _is_real_method(x):
    return callable(x) and type(x).__name__ not in ('MagicMock', 'AsyncMock', 'Mock')
```

Alternatively, consider:

```python
from agent_cascade.pool.message_queue import MessageQueueMixin
def _is_real_method(x):
    return callable(x) and isinstance(x, MessageQueueMixin)
```

The `isinstance` check is more future-proof and clearer. If the plan prefers the type-name approach for its explicitness, at least include `'Mock'`.

---

### 3. Predicate Correctness (Change B)

**Status:** ✅ OK – No changes needed  
**Location:** Plan line 74; `agent_cascade/async_shell_pkg/tracker.py` lines 954, 983-987, 1055-1075

**Finding:** The predicate `m.startswith('⟨shell_cmd') and f'Tool ID: {tool_id}' in m` correctly matches both heartbeat and completion messages. Both formats start with the literal `'⟨shell_cmd'` and contain `"Tool ID: {tool_id}"` with a space after the colon. Verified by reading tracker code.

No action required.

---

### 4. Test Rewrite Feasibility (Change C)

**Status:** ⚠️ OK with note – Feasible but requires a small helper  
**Location:** Plan lines 112-120; `tests/test_async_shell_cmd.py`; `agent_cascade/pool/message_queue.py` lines 135-176

**Finding:** The plan proposes rewriting `test_wait_returns_new_output` to exercise the new queue-driven path. This is feasible, but `MessageQueueMixin.wait_for_message` depends on:
- `self.is_instance_terminated(instance_name)`
- `self._message_condition` (a `Condition` object)
- `self.message_queues`

A minimal fake or subclass must provide these dependencies. The plan acknowledges that timeout/cap tests can remain on the fallback path, which is wise.

**Recommendation:** Create a lightweight test helper class:

```python
class _FakePoolWithWait(MessageQueueMixin):
    def __init__(self):
        self._execution = _FakeExecution()
        self.message_queues = {}
        self._queue_lock = threading.Lock()
        self._message_condition = threading.Condition(self._queue_lock)

    @property
    def _state_lock(self):
        return self._execution._state_lock

    def is_instance_terminated(self, instance_name):
        return False

class _FakeExecution:
    _state_lock = threading.Lock()
```

Or, even simpler, implement a standalone `wait_for_message` function that directly performs the predicate scan and condition wait without needing the mixin's structure. The key is to avoid flakiness from real timeouts; use short waits or patch `time` if needed.

No change to the plan is required; just be aware of the small implementation effort.

---

### 5. Other Risks / Edge Cases

**Status:** Mostly OK, with one implementation note

#### 5.1 Lock Discipline
The plan correctly states: "Do NOT call `wait_for_message` while holding `task._lock`." The existing code releases task locks before entering the wait/poll loop. No change needed.

#### 5.2 Timeout Handling
The plan says "Deadline/timeout handling unchanged (1.0 s slices, cleanup of empty list on timeout, return None)." This is fine; no modification to `wait_for_message`'s timeout logic is required beyond adding the predicate loop.

#### 5.3 Non-Shell Message Filtering
The predicate ensures only shell messages are consumed. Verified that both heartbeat and completion formats contain the tool_id marker. No change needed.

#### 5.4 Code Duplication / Maintainability
The plan says: "Replace the busy-poll loop (:440-480) with a queue-driven wait" but then provides a fallback that includes the old poll loop. This will result in two code paths: one using `wait_for_message`, one using polling. **To avoid duplication and future maintenance issues, extract the polling logic into a separate helper function (e.g., `_polling_wait(...)`) and call it from the fallback.** The plan does not explicitly mention this; it's an implementation detail that should be addressed during coding.

#### 5.5 Performance of Predicate Scan
Scanning the queue list for a matching message is O(n). For typical usage (one agent, few messages), this is acceptable. No change needed.

#### 5.6 Backward Compatibility of `wait_for_message` Signature
The current signature at `message_queue.py:135` is `def wait_for_message(self, instance_name: str, timeout: float = 30.0) -> Optional[str]:`. Adding `predicate=None` is backward-compatible because it's a default argument. The investigation report confirms zero existing call sites, so no risk of breaking other code.

---

## Required Changes Before Implementation

1. **Clarify lock wording in Change A** (plan line 54) to explicitly state that the predicate scan occurs inside `with self._message_condition:` which acquires the underlying `_queue_lock`.
2. **Expand mock guard** to include `'Mock'` or switch to `isinstance(pool, MessageQueueMixin)`.
3. **Extract polling logic** into a separate helper function to avoid duplication between the new path and fallback.
4. **Prepare a lightweight fake pool** (or mixin subclass) for the rewritten test that provides `wait_for_message`, `is_instance_terminated`, and necessary locks.

---

## Final Verdict

**Plan needs these N changes before implementation.**  
Apply the corrections above, then the plan is ready for coding.

---

*Review completed with rigorous code inspection of all referenced files.*

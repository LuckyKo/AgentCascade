# Parallel Merge Protocol — Detailed Pseudocode

**Purpose:** Defines exactly how concurrent sub-agents serialize their writes into the unified `sub_agent_state` store under `_state_lock`, preventing race conditions when multiple agents complete simultaneously.

**Context:** This is the implementation specification for Section 5.2 of the tab unification plan (`tab_unification_plan_v4.md`). It covers the three critical scenarios:
- (a) How `enqueue_message()` threads are serialized into `sub_agent_state` under `_state_lock`
- (b) What happens when two parallel agents try to update overlapping state
- (c) How result merging is gated behind `_state_lock`

**Locking Discipline:**
- `_state_lock` protects: `sub_agent_state`, `active_stack`, `last_tool_args`, `message_queues`, `async_message_queue`
- `_activity_lock` protects: `_last_activity` timestamps (acquired internally by `_mark_activity`)
- `_halt_lock` protects: per-instance halt status (acquired internally by `is_halted()`, declared at agent_pool.py:98)

⚠️ **Dead Code Note:** `_conversation_lock` is declared at agent_pool.py:115 but never acquired anywhere in the codebase. This document does NOT propose using it. If a future implementation requires protecting `instance_conversations` writes, add `_conversation_lock` as priority 2 (after `_state_lock`).

**Lock Acquisition Order (to prevent deadlocks):**
1. `_state_lock` → 2. `_halt_lock` → 3. `_activity_lock` (matches existing code at agent_pool.py:737)

Never hold an inner lock while acquiring an outer one. The current code already follows this pattern.

---

## (a) Serializing `enqueue_message()` Threads Under `_state_lock`

When parallel agents complete, they all call `enqueue_message()` to push their completion notification into the orchestrator's queue. Without synchronization, interleaved appends can corrupt the list or lose messages. The protocol ensures that each enqueue is atomic with respect to state mutation.

### Current Code Reference

```python
# agent_pool.py line 262 — current (unlocked) implementation:
def enqueue_message(self, target: str, text: str):
    """Push a message into a specific agent's queue."""
    if target not in self.message_queues:
        self.message_queues[target] = []
    self.message_queues[target].append(text)

    # Mark activity: someone is sending messages to this agent
    if hasattr(self, '_mark_activity'):
        self._mark_activity(target)
```

### Pseudocode — Lock-Guarded `enqueue_message`

```python
def enqueue_message(self, target: str, text: str):
    """Push a message into a specific agent's queue (thread-safe).

    Steps:
      1. Acquire _state_lock to protect the message_queues dict from
         concurrent modification by drain_queue() or other enqueue calls.
      2. Ensure the target queue exists; create if missing.
      3. Append the message atomically within the lock scope.
      4. Mark activity for the target agent. Note: _mark_activity uses its
         own _activity_lock internally (agent_pool.py:696-703), so holding
         _state_lock here does NOT nest locks — it merely prevents another
         thread from modifying message_queues between our append and drain.
    """
    with self._state_lock:
        # Step 1 — ensure queue exists
        if target not in self.message_queues:
            self.message_queues[target] = []

        # Step 2 — append message (atomic under lock)
        self.message_queues[target].append(text)

        # Step 3 — mark activity (_activity_lock acquired internally, no nesting issue)
        if hasattr(self, '_mark_activity'):
            self._mark_activity(target)


# ── drain_queue also needs to be lock-guarded ──────────────────────────
# Critical: drain_queue runs on the orchestrator's main thread (agent_orchestrator.py:2005)
# while enqueue_message runs on ThreadPoolExecutor worker threads. The _state_lock bridges these threads.

def drain_queue(self, target: str) -> List[str]:
    """Pop and return all pending messages for a specific agent (thread-safe).

    Cross-thread scenario: called from the orchestrator's main loop on one thread,
    while parallel agents call enqueue_message() on worker threads. The lock ensures
    the deduplication logic (seen = set(msgs)) doesn't lose or duplicate messages if
    another thread appends to async_message_queue between our copy and clear.

    Steps:
      1. Acquire _state_lock to prevent concurrent enqueue during drain.
      2. Copy the target queue into a local list and clear it.
      3. Merge in any legacy global queue messages with deduplication.
      4. Return the combined list.
    """
    with self._state_lock:
        msgs = []

        # Drain targeted queue
        if target in self.message_queues and self.message_queues[target]:
            msgs = self.message_queues[target][:]   # copy snapshot
            self.message_queues[target].clear()      # consume

        # Drain legacy global queue (backward compat)
        if self.async_message_queue:
            seen = set(msgs)
            for m in self.async_message_queue:
                if m not in seen:                     # deduplicate
                    msgs.append(m)
                    seen.add(m)
            self.async_message_queue.clear()          # consume

        return msgs
```

### Scenario Walkthrough — Three Agents Complete Simultaneously

Consider three parallel agents (`WorkerA`, `WorkerB`, `WorkerC`) all finishing at the same time and each calling `enqueue_message("Maine", "...")`:

```
Thread-A: with _state_lock → [acquire] → enqueue "WorkerA finished" → [release]
Thread-B: with _state_lock → [BLOCKED until A releases] → enqueue "WorkerB finished" → [release]
Thread-C: with _state_lock → [BLOCKED until B releases] → enqueue "WorkerC finished" → [release]
```

Result in `message_queues["Maine"]`:
```python
["WorkerA finished", "WorkerB finished", "WorkerC finished"]   # deterministic FIFO
```

Without the lock, Thread-B and Thread-C could interleave their list operations with Thread-A's append, potentially losing messages or corrupting the list structure.

---

## (b) Resolving Overlapping State Updates from Parallel Agents

When two parallel agents attempt to update overlapping entries in `sub_agent_state`, the `_state_lock` ensures that updates are serialized. The key insight: **each agent writes its own entry, so there is no data conflict** — but we still need the lock because (1) the dict itself can be mutated concurrently, and (2) the orchestrator may read `sub_agent_state` while agents are writing.

### Pseudocode — Merging Results into `sub_agent_state`

```python
def _merge_result_into_state(self, instance_name: str, conv: List[Message],
                             is_active: bool, error: Optional[str] = None):
    """Merge a completed sub-agent's results into sub_agent_state (thread-safe).

    This is called from agent_orchestrator.py after a parallel agent finishes.
    It updates the state entry for this specific instance and clears its
    active_stack presence.

    Steps:
      1. Acquire _state_lock to protect sub_agent_state and active_stack.
      2. Build the new state dict for this instance.
      3. Write it into sub_agent_state[instance_name] atomically.
      4. Remove the instance from active_stack if present.
    """
    with self._state_lock:
        # Step 1 — build the state snapshot for this agent
        state = {
            'active': is_active,
            'agent_name': f"{instance_name} ({self.instance_classes.get(instance_name, 'unknown')})",
            'messages': copy.deepcopy(conv),  # deep copy to isolate from live instance_conversations
        }

        if error:
            state['error'] = error

        # Step 2 — write into the unified store (atomic under lock)
        self.sub_agent_state[instance_name] = state

        # Step 3 — remove from active_stack if still there
        # Use reverse-first-occurrence removal (matches agent_orchestrator.py:2223-2227)
        # because agents can appear multiple times in the stack during nested calls
        for i in range(len(self.active_stack) - 1, -1, -1):
            if self.active_stack[i] == instance_name:
                self.active_stack.pop(i)
                break
```

### Scenario Walkthrough — Two Agents Update Overlapping State

Consider `WorkerA` and `WorkerB` both completing. They do NOT write the same key (each writes its own `sub_agent_state["WorkerA"]` / `sub_agent_state["WorkerB"]`), but they share the same dict object:

```
Thread-A: with _state_lock → [acquire]
          → self.sub_agent_state["WorkerA"] = {...}
          → reverse-pop("WorkerA") from active_stack  # most recent occurrence
          → [release]

Thread-B: with _state_lock → [BLOCKED until A releases]
          → self.sub_agent_state["WorkerB"] = {...}
          → reverse-pop("WorkerB") from active_stack
          → [release]
```

The orchestrator's main loop reading `sub_agent_state` will see either the fully-consistent state before any writes, or after both writes have completed — never a partially-updated view.

### Edge Case — Agent Writes Its Own State While Another Agent Reads It

If `WorkerA` completes and updates its own entry while the orchestrator's main loop is iterating over `sub_agent_state.values()` (e.g., to broadcast UI state), the lock prevents reading during write:

```python
# PROPOSED NEW function in agent_orchestrator.py — does not exist yet.
# Current code reads sub_agent_state directly (without locking) at:
#   api_server.py lines 510, 1074, 1082, 1100
# This function would replace those unprotected reads.

def _collect_ui_state(self) -> Dict[str, Any]:
    """Snapshot of all sub-agent states for UI broadcast (thread-safe).

    Called from the main loop before sending state to the WebUI via WebSocket.
    Acquires _state_lock, deep-copies the snapshot, releases lock, then broadcasts.
    This prevents the orchestrator from reading a partially-updated sub_agent_state
    while a worker thread is writing into it.
    """
    with self.agent_pool._state_lock:
        return {name: copy.deepcopy(state)
                for name, state in self.agent_pool.sub_agent_state.items()}
```

The `copy.deepcopy` is critical: it ensures the returned dict is independent of the live store, so the orchestrator can broadcast without holding the lock. **Note:** since `_merge_result_into_state()` already deep-copies messages at write time, this second `copy.deepcopy` in the reader is a safety net for any nested objects not covered by the writer's copy (e.g., mutable error strings). It could potentially be reduced to a shallow copy if profiling shows it's too expensive.

---

## (c) Gating Result Merging Behind `_state_lock` — Full End-to-End Protocol

This section shows the complete flow from parallel agent completion through result merging, as implemented in `ParallelAgentManager.task_wrapper()` (agent_orchestrator.py lines 311–348).

**Architecture note:** `task_wrapper` calls `orchestrator._stream_sub_agent_call(...)` which is a generator. The three writes to `sub_agent_state` (lines 1963, 2174, 2219) happen **inside that generator**, not in `task_wrapper` itself. Both the synchronous path (from `_run`) and the parallel path (from `task_wrapper`) share this same generator — so any change to how it writes state affects both paths.

The proposed fix: consolidate the three scattered writes into a single new method `_merge_result_into_state()` on AgentPool, called from inside `_stream_sub_agent_call()`.

### Pseudocode — Complete Parallel Completion Protocol

```python
# ── Inside ParallelAgentManager.submit_task() as a closure ────────────────
# (actual code: agent_orchestrator.py lines 311-348, defined inside submit_task)

def task_wrapper():   # closure capturing: orchestrator, instance_name, endpoint_release, self
    """Execute a parallel sub-agent and merge its results safely.

    Full lifecycle:
      1. Run the agent's generator to completion (or until stopped/halted).
         The generator (_stream_sub_agent_call) writes to sub_agent_state
         at lines 1963, 2174, 2219 — these will be replaced by calls to
         _merge_result_into_state() which acquires _state_lock.
      2. Capture the result from StopIteration.value.
      3. Enqueue a completion message into the orchestrator's queue.
      4. Clean up active_tasks tracking.

    Thread-safety is enforced at each mutation point via _state_lock.
    Note: runs on a ThreadPoolExecutor worker thread (not the main orchestrator thread).
    """
    result = None
    error = None

    try:
        # Phase 1 — execute the agent (no locks needed; isolated by deepcopy)
        gen = orchestrator._stream_sub_agent_call(...)
        try:
            while True:
                if self.agent_pool.stopped or self.agent_pool.is_halted(instance_name):
                    break                          # early exit on stop/halt
                next(gen)                          # advance generator
        except StopIteration as e:
            result = e.value                       # capture final result

    except Exception as exc:
        error = str(exc)
        logger.error(f"Parallel sub-agent {instance_name} failed: {exc}")

    finally:
        # Phase 2 — release endpoint slot (no lock needed; scheduler is thread-safe)
        if endpoint_release is not None:
            endpoint_release()

        # Phase 3 — mark activity (needs lock via enqueue_message path)
        self.agent_pool._mark_activity(instance_name)

        # Phase 4 — merge results into sub_agent_state under _state_lock
        if error:
            completion_msg = f"[Parallel Sub-Agent '{instance_name}' Failed]:\n{error}"
            is_active = False
        else:
            completion_msg = f"[Parallel Sub-Agent '{instance_name}' Finished]:\n{result}"
            is_active = False

        # Enqueue the completion message (thread-safe via _state_lock inside enqueue_message)
        self.agent_pool.enqueue_message(orchestrator.session_name, completion_msg)

        # NOTE: The sub_agent_state update happens INSIDE _stream_sub_agent_call()
        # at lines 1963, 2174, 2219 — not here in task_wrapper.
        # After implementing _merge_result_into_state(), those three write sites
        # will be replaced with calls to it (which acquires _state_lock internally).

        # Phase 5 — clean up active_tasks (no lock needed; this dict is only
        #          accessed from the orchestrator thread after agent completion)
        self.active_tasks.pop(instance_name, None)
```

### Lock Ordering Summary

To prevent deadlocks, all code paths must acquire locks in this order:

| Priority | Lock              | Protects                                   |
|----------|-------------------|--------------------------------------------|
| 1        | `_state_lock`     | `sub_agent_state`, `active_stack`, `message_queues`, `async_message_queue`, `last_tool_args` |
| 2        | `_halt_lock`      | Per-instance halt status (internal to `is_halted()`, declared at agent_pool.py:98) |
| 3        | `_activity_lock`  | `_last_activity` timestamps (internal to `_mark_activity()`) |

⚠️ **Dead code:** `_conversation_lock` is declared at agent_pool.py:115 but never acquired. Do not use it until `instance_conversations` writes are actually protected by it.

**Rule:** Never hold an inner lock while acquiring an outer one. Always acquire higher-priority locks first. The current code already follows this pattern (see agent_pool.py line 737: `_state_lock` is acquired alone, then `is_halted()` acquires `_halt_lock` internally — no nesting).

### Race Condition Prevention Checklist

| Scenario                                          | Prevention Mechanism                      |
|---------------------------------------------------|-------------------------------------------|
| Two agents enqueue to same queue simultaneously   | `_state_lock` in `enqueue_message()`      |
| Agent writes `sub_agent_state` while orchestrator reads it | `_state_lock` + `copy.deepcopy` in reader |
| Agent removes from `active_stack` while idle checker reads it | `_state_lock` (already exists, line 737) |
| `drain_queue` runs while `enqueue_message` appends | `_state_lock` in both methods             |
| `last_tool_args` read during `__USE_PREV_ARG__` resolution | `_state_lock` — **NEEDS TO BE ADDED** at agent_orchestrator.py:1457-1460 (reads) and 1522-1528 (writes, `__USE_PREV_ARG__` resolution). Grep for `"last_tool_args"` in agent_orchestrator.py to find all 9 occurrences (see implementation checklist item 4) |

---

## Implementation Checklist for Coders

When implementing this protocol, follow these steps:

1. **Add `_state_lock` to `enqueue_message()` and `drain_queue()`** — wrap the body of both methods in `with self._state_lock:`
2. **Create `_merge_result_into_state()` as a new method on AgentPool** — this centralizes the state write logic currently scattered across agent_orchestrator.py (lines 1963, 2174, 2219). **Replace all three existing `self.agent_pool.sub_agent_state[instance_name] = state` writes in `_stream_sub_agent_call()` with calls to this new method.** Do not leave any direct writes to `sub_agent_state` outside of `_merge_result_into_state()`. Note: `_stream_sub_agent_call()` is shared between sync and parallel execution paths, so this change affects both.
3. **Guard all `sub_agent_state` reads in the main loop** with `_state_lock` + `copy.deepcopy` to snapshot before releasing the lock (see proposed `_collect_ui_state()` in Section b)
4. **Add `_state_lock` around `last_tool_args` reads/writes** — currently unprotected at agent_orchestrator.py:1457-1460 (read) and 1522-1528 (writes, `__USE_PREV_ARG__` resolution). Grep for `"last_tool_args"` in agent_orchestrator.py to find all 9 occurrences. Wrap both read and write sections in `with self.agent_pool._state_lock:`
5. **Guard all `active_stack.append()` and `active_stack.clear()` calls with `_state_lock`** — currently unprotected at agent_orchestrator.py:1947, api_server.py:2236 (append), agent_cascade/compression/agent_invoker.py:193 (append), api_server.py:886 (clear on session reset), api_server.py:2028 (clear on retry), agent_orchestrator.py:909 (clear in `_run`), and agent_pool.py:681 (clear during `reset_all`). Grep for `active_stack\.(append|clear)` across all .py files to find all 7 occurrences. Each must be wrapped in `with self.agent_pool._state_lock:`. The clear() calls are especially important because they run on the API server's request-handling thread, which can race with append()s on ThreadPoolExecutor worker threads.
6. **Verify no nested lock acquisitions** — grep for all `with.*_lock` patterns across agent_pool.py and agent_orchestrator.py to confirm `_state_lock` is never held while acquiring another lock. Specifically check: (a) `enqueue_message` does not hold `_activity_lock` when entering `_state_lock`, (b) `_is_agent_idle` acquires `_state_lock` alone then calls `is_halted()` which acquires `_halt_lock` internally — this is fine because `_state_lock` is released before `_halt_lock` is acquired.
7. **Update the `_state_lock` comment** at agent_pool.py:114 to reflect extended protection scope: change from `# Protects sub_agent_state, active_stack` to `# Protects sub_agent_state, active_stack, last_tool_args, message_queues, async_message_queue`.
8. **Add integration test** — spawn 3+ parallel agents that complete simultaneously and verify all completion messages arrive in `message_queues["Maine"]` without loss

### Out-of-Scope Notes

- A separate direct `sub_agent_state` write exists in `agent_cascade/compression/agent_invoker.py:187-191` for the compression agent's direct run path. Grep for `self\.agent_pool\.sub_agent_state\[\w+\]\s*=` across all .py files to find all such writes. This is NOT covered by `_merge_result_into_state()` and should be addressed separately (add `_state_lock` around it).
- The `_conversation_lock` at agent_pool.py:115 remains unused. If future work requires protecting `instance_conversations` writes, assign it priority 2 in the lock ordering table (after `_state_lock`).
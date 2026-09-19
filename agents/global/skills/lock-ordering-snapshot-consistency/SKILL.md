---
name: lock-ordering-snapshot-consistency
description: Pattern for identifying race conditions and data inconsistency in concurrent index or cache modifications where locks are not consistently held during reads and writes.
source: auto-generated
version: "1.0.0"
triggers:
  - "concurrent index modification"
  - "lock ordering issue"
  - "snapshot consistency"
  - "race condition in data structures"
generated_by: reviewer
generated_from_task: "Independent review of memory-hint commits for race conditions and consistency issues."
---

## Goal

Identify and prevent race conditions and data inconsistency bugs in systems that maintain mutable indexes, caches, or collections accessed by multiple threads where locks are not consistently applied across all read/write operations.

## Procedure

### Step 1 — Map the lock topology
- List all locks used in the component (e.g., `_index_lock`, `_compression_lock`, `RLock`, `Lock`).
- For each public method that accesses shared state, note which lock it acquires and for how long.
- Identify any methods that read/write shared state **without** holding any lock.

### Step 2 — Trace read-write sequences across method boundaries
Look for patterns where:
1. Method A acquires Lock X, reads data, releases Lock X, then calls Method B which uses the same data.
2. Method C modifies the same data under Lock Y, but Method A's read happened without any lock.
3. The order of lock acquisition is inconsistent (e.g., some code acquires `_index_lock` before `_compression_lock`, others do the opposite).

Example from `manager.py`:
```python
def rescan_vaults(self):
    # Modifies self._vault_indexes WITHOUT holding _index_lock
    self._vault_indexes[root] = idx  # line 179 - RACE CONDITION

def _process_job(self, job):
    with self._index_lock:          # Acquires lock
        matches = self._matcher.match(query)
    display = [self._display_path(p) for p in to_hint]  # Uses data AFTER releasing lock - INCONSISTENCY
```

### Step 3 — Check for snapshot vs live access
- Does any method read shared state and then later use it without re-acquiring the lock?
- If a sequence involves multiple steps (match → filter → display), is the entire sequence protected by a single lock, or does data potentially change between steps?

### Step 4 — Verify mutation safety for concurrent readers
- For methods that read but don't modify, are they protected by a read lock (e.g., `RLock`) or do they rely on immutable data structures?
- Can the data be modified while a reader is active? If yes, what happens to the reader?

### Step 5 — Look for "best-effort" gaps
- Are there methods that swallow exceptions (`try/except`) around lock acquisition or use of shared state? This can hide race conditions.
- Is there any code that checks `if X` then uses `X` without holding a lock in between?

## Tips

- **Always assume locks are not held unless explicitly shown.** A method that doesn't acquire a lock is reading potentially stale or concurrently-modified data.
- **Snapshot pattern:** When you need to read and then later use data, either hold the lock for the entire duration, or create a copy under the lock and work with the copy.
- **Lock ordering:** Define a strict order (e.g., `_index_lock` always acquired before `_compression_lock`) to prevent deadlocks. Inconsistent ordering is a common source of concurrency bugs.
- **Test with stress:** Race conditions may not appear in normal tests. Use tools like `pytest-xdist` or manual stress testing to expose them.

## Common Pitfalls

1. **"It's only used by a daemon thread"** - Still needs protection if main thread can also modify shared state.
2. **"The lock is there, just not held during this small operation"** - Small operations can still be preempted; consistency matters.
3. **"I'm just reading, I don't need the lock"** - Reading while another thread writes without synchronization causes data races.
4. **"Lock order doesn't matter here"** - Even two locks in different methods can deadlock if acquired in opposite orders across the call stack.

## When to Escalate

- If the component uses **multiple locks** with no clear ordering policy.
- If there are **no unit tests** that exercise concurrent access.
- If the code relies on **language-level guarantees** (e.g., "Python's GIL protects this") without understanding the actual memory model.

---
name: python-object-identity-shadowing-debug
description: Debug Python variable shadowing bugs where reassigning a mutable parameter (list/dict) to a new object severs identity with the caller, causing stale-state loops or silent data loss far from the bug site.
source: auto-generated
version: "1.0.0"
triggers:
  - "infinite loop after state mutation"
  - "stale data after function returns"
  - "list parameter reassignment"
  - "object identity severed"
  - "clear vs reassign list"
  - "mutable parameter shadowing"
generated_by: orchestrator
generated_from_task: "Fallback compression infinite loop — llm_messages = [] reassigned parameter, severing identity with run() loop's list"
---

## Goal
Rapidly identify and fix Python variable shadowing bugs where `param = []` (or `= {}`) inside a function rebinds the local name to a new object, silently discarding the caller's reference. The bug manifests as stale state in the caller — often an infinite loop or silent data loss — far from the actual code location.

## Procedure

### Step 1 — Recognize the symptom pattern
The bug presents as: a function mutates a mutable parameter (list/dict) to update state, the mutation succeeds internally, but the caller sees NO change after the function returns. Common manifestations:
- Infinite retry/loop where the same stale payload is resent every iteration
- Token counts or message lists "jumping back" to pre-mutation values on the next turn
- Server-side seeing identical input despite client-side "successful" compression/update

Key log signature: a value that was reduced (e.g., 91 messages → 28) reappears at its original size (91) on the very next iteration, with identical downstream effects (same token count, same error).

### Step 2 — Trace object identity through the call chain
For each function in the call path that receives the mutable object:
1. Check if the parameter is **reassigned** (`param = []`) vs **mutated in-place** (`param.clear()` / `param.append()`).
   - Reassignment creates a new local binding; the caller's reference is untouched.
   - In-place mutation affects the shared object.
2. Check if any helper called with the parameter does `clear()` + `extend()` (in-place) or returns a new list (rebinding).
3. Check where the object is stored in a cache/registry (e.g., `instance._cached_llm_messages = llm_messages`) — after reassignment, this points to the NEW local, not the caller's list.

### Step 3 — Verify with the documented invariant
Look for comments or docstrings that state the identity expectation. Example: "llm_messages is normally the SAME object as _cached_llm_messages" (core.py:245). The bug VIOLATES this invariant; the fix RESTORES it. If no invariant is documented, the absence of one is itself a code smell — add a comment.

### Step 4 — Fix: reassign → in-place mutation
```python
# BUG: rebinds local name, caller's list untouched
llm_messages = []
helper(llm_messages)  # fills NEW list; caller still has old list

# FIX: mutate in-place, caller sees the change
llm_messages.clear()
helper(llm_messages)  # fills SHARED list; caller sees compressed state
```
For dicts: `d = {}` → `d.clear()`. For sets: `s = set()` → `s.clear()`.

### Step 5 — Add an identity guard (defensive)
After the mutation path completes, add a cheap identity check that logs a warning if the invariant broke:
```python
if instance._cached_llm_messages is not llm_messages:
    logger.warning(f"identity diverged for {name} "
                   f"(id={id(llm_messages)} vs id={id(instance._cached_llm_messages)})")
```
This turns a silent infinite loop into a visible warning on the next regression.

### Step 6 — Write a revert-proof regression test
The test must assert **object identity**, not just content:
```python
original = [Message(f'm{i}') for i in range(20)]
instance._cached_llm_messages = original
result = list(engine.call(instance, original, ...))  # triggers the bug path

# Assertion 1: in-place mutation (content check)
assert len(original) < 20

# Assertion 2: identity invariant (the actual guard)
assert original is instance._cached_llm_messages
```
Verify revert-proof: temporarily revert the fix, confirm the test FAILS. Restore the fix, confirm it PASSES.

## Tips
- **Grep for the anti-pattern**: `grep -n "param_name = \[\]" file.py` where `param_name` is a function parameter receiving a list. Same for `= {}` and `= set()`.
- **The bug is invisible in logs** until you compare the value BEFORE and AFTER the function returns — the internal mutation succeeds, so all internal logs look correct. The only tell is the caller's value being unchanged.
- **Multi-round loops amplify it**: if the handler runs N rounds (e.g., 5 compression rounds), each round rebuilds the local list but never touches the caller's. The loop appears to "work" internally but produces zero external effect.
- **Don't confuse with legitimate reassignment**: if a function is DESIGNED to return a new list (pure function), reassignment is fine. The bug only exists when the caller expects in-place mutation AND the function silently rebinds instead.
- **Thread safety note**: in-place `clear()` + `extend()` is NOT atomic — if another thread reads the list between clear and extend, it sees an empty/partial list. In single-threaded-per-instance designs (like AgentCascade's state guard), this is safe. In concurrent designs, use a lock or swap-the-reference pattern instead.

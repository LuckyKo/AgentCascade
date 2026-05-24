# Scheduling Audit Lessons (2026-05-21)

## Closure Capture in Generator Wrappers

**Problem**: When wrapping a generator with a `finally` block that releases a semaphore, the `sem` variable is captured by closure. If the endpoint semaphore is resized between generator creation and consumption, the wrapper could release the wrong semaphore.

**Fix applied**: Default-argument capture pattern:
```python
def sem_generator_wrapper(gen, _sem=sem):
    try:
        yield from gen
    finally:
        _sem.release()
```
This freezes `sem` at definition time. Zero runtime cost.

## Python yield from + finally Behavior (PEP 380)

When an exception propagates from the underlying generator through `yield from`, the enclosing `finally` block **always executes**. This is guaranteed by Python's semantics:
```python
def wrapper(gen):
    try:
        yield from gen  # if gen raises here...
    finally:
        sem.release()   # ...this runs before the exception propagates
```

This means semaphore release on generator failure is **always safe** — no leak possible.

## Dead Code Pattern to Watch For

`generator_wrapper` function (now removed) was a copy-paste artifact — defined but never called. Always check if defined generator wrappers are actually used when reviewing retry/fallback code.
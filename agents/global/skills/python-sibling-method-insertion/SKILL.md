---
name: python-sibling-method-insertion
description: Safe way to add/extract a sibling method inside a large Python class without silently swallowing the enclosing method's body (an indentation bug that is syntactically valid but breaks at runtime).
source: auto-generated
version: "1.0.0"
triggers:
  - "add method to existing class"
  - "extract helper method"
  - "python indentation bug"
  - "refactor long method"
  - "insert sibling method"
generated_by: coder
generated_from_task: "Backfill Message.ts from log timestamp in session_io.py load_session_from_log; extracted a private helper and hit a nested-method indentation bug."
---

## Goal

Add or extract a new method inside an existing Python class — especially a large one (dozens–hundreds of lines) — without accidentally making the rest of the enclosing method into *nested* code.

## The trap

Python has no braces. If you insert a `def` at the WRONG indent level while you are still inside another method's body, everything after it that is indented deeper becomes part of the new (or old) function. This is **syntactically valid** — `ast.parse`, `syntax_check`, and even import all pass — but it fails at runtime with confusing errors like:

```
NameError: name 'self' is not defined      # step-7 code now lives inside a @staticmethod
NameError: name 'agent_class' is not defined  # enclosing method's locals no longer in scope
```

The failure often only appears when a *specific* branch runs, so it can pass a quick smoke test and blow up later.

## Procedure

### Step 1 — Prefer the END of the class
When extracting a helper from the middle of a long method, the safest placement is **after the enclosing method's final `return`** (i.e. at the end of the class). This never touches the enclosing body:

```python
    def load_session_from_log(self, ...):
        ...
        return f"Loaded {n} messages ..."   # enclosing method ends here

    @staticmethod
    def _backfill_ts_from_dict(msg, msg_dict) -> None:
        ...                                  # clean sibling — zero risk to body above
```

Call it from the original site with `self._helper(...)` (or `ClassName._helper(...)` for a staticmethod).

### Step 2 — If you MUST insert mid-body, re-join carefully
If the helper must sit between two blocks of the enclosing method:
1. Keep the ENTIRE enclosing body at its original indent (8 spaces for a class-method body).
2. Add the new `def` at 4-space (class) indent so it is a sibling.
3. After inserting, **re-read the whole region** and confirm every line of step-7/step-8/etc. is back at 8-space indent inside the original method — not under the new `def`.

### Step 3 — Verify with a REAL test run, not just ast.parse
`ast.parse` / `syntax_check` CANNOT catch this (it's valid syntax). Run the actual code path:
- The unit test that exercises the extracted helper.
- Ideally one test that drives the enclosing method end-to-end (or at least imports + instantiates and calls it), so a swallowed-body NameError surfaces.

### Step 4 — Extract small + pure for testability
Keep the extracted helper **pure** (no `self`, no I/O) where possible so it's a `@staticmethod` you can unit-test directly without fixtures/mocks:
```python
SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': iso})
```

## Tips

- After any mid-method insertion, do a focused `read_file` of the boundary (the last lines of the previous method + the new def + first lines of the next block) and check indentation by eye before running.
- A sudden "new" `NameError` for a variable that clearly exists in the enclosing method is the signature symptom — suspect a swallowed body, not a typo.
- Use targeted `edit_file` (exact or delete_and_insert with explicit line ranges) rather than rewriting the whole file; large rewrites are where indentation drift happens.
- If you used `delete_and_insert` and the diff shows an unrelated line removed (e.g. a comment), restore it — keep the diff minimal for review.

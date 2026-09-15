---
name: python-sibling-method-insertion
description: Safe way to add/extract a sibling method inside a large Python class without silently swallowing the enclosing method's body — an indentation bug that is syntactically valid but breaks at runtime.
source: auto-generated
version: "1.0.0"
triggers:
  - "add method to existing class"
  - "extract helper method"
  - "python indentation bug"
  - "refactor long method"
  - "insert sibling method"
---

## Goal

Add or extract a new method inside an existing Python class — especially a large one — without accidentally making the rest of the enclosing method into *nested* code.

## The trap

Python has no braces. If you insert a `def` at the WRONG indent level while still inside another method's body, everything after it that is indented deeper becomes part of the new (or old) function. This is **syntactically valid** — `ast.parse`, `syntax_check`, and even import all pass — but fails at runtime with confusing errors like:
```
NameError: name 'self' is not defined       # step-7 code now lives inside a @staticmethod
NameError: name 'agent_class' is not defined  # enclosing method's locals no longer in scope
```
The failure often only appears when a *specific* branch runs, so it can pass a quick smoke test and blow up later.

## Procedure

1. **Prefer the END of the class.** When extracting a helper from the middle of a long method, the safest placement is after the enclosing method's final `return` (end of the class) — it never touches the enclosing body:
   ```python
       def load_session_from_log(self, ...):
           ...
           return f"Loaded {n} messages ..."   # enclosing method ends here

       @staticmethod
       def _backfill_ts_from_dict(msg, msg_dict) -> None:
           ...                                  # clean sibling — zero risk to body above
   ```
   Call it from the original site with `self._helper(...)` (or `ClassName._helper(...)` for a staticmethod).
2. **If you MUST insert mid-body, re-join carefully.** Keep the ENTIRE enclosing body at its original indent (8 spaces for a class-method body); add the new `def` at 4-space (class) indent so it's a sibling; after inserting, **re-read the whole region** and confirm every subsequent line is back at 8-space indent inside the original method — not under the new `def`.
3. **Verify with a REAL test run, not just ast.parse.** `ast.parse`/`syntax_check` CANNOT catch this (it's valid syntax). Run the actual code path: the unit test that exercises the extracted helper, and ideally one test that drives the enclosing method end-to-end (or at least imports + instantiates + calls it) so a swallowed-body NameError surfaces.
4. **Extract small + pure for testability.** Keep the extracted helper **pure** (no `self`, no I/O) where possible so it's a `@staticmethod` you can unit-test directly without fixtures/mocks: `SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': iso})`.

## Tips

After any mid-method insertion, do a focused `read_file` of the boundary (last lines of the previous method + new def + first lines of the next block) and check indentation by eye before running. A sudden "new" `NameError` for a variable that clearly exists in the enclosing method is the signature symptom — suspect a swallowed body, not a typo. Use targeted `edit_file` (exact or delete_and_insert with explicit line ranges) rather than rewriting the whole file; large rewrites are where indentation drift happens. If you used `delete_and_insert` and the diff shows an unrelated line removed (e.g. a comment), restore it — keep the diff minimal for review.

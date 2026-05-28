# Phase 7 Migration — Fixer Notes

## Critical Discovery: validate_message_pool Broke Class Structure

The most insidious issue was that `validate_message_pool` was defined at column 0 (module-level indentation) **inside** the ExecutionEngine class body. This caused Python to treat all methods after it (`_create_and_run_agent`, `_execute_agent_sync`, etc.) as **nested functions inside `validate_message_pool`**, not as methods of ExecutionEngine.

### How to detect this
Use AST analysis:
```python
import ast
tree = ast.parse(open('execution_engine.py').read())
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == 'ExecutionEngine':
        methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
```
If key methods like `_create_and_run_agent` are NOT in the list, something is structurally wrong.

### Root cause
A module-level function was inserted between class methods without being moved outside the class body. Python treats any dedent to column 0 as exiting the current block, and subsequent indented code becomes part of that new block (the function), not the original block (the class).

## Fix Summary

1. **Fix #1**: Moved `_handle_compress_context` inside ExecutionEngine class
2. **Fix #2**: Moved `validate_message_pool` to module level AFTER the class ends
3. **Fix #3**: Added `import os` — was using `os.getcwd()` without importing os
4. **Fix #4**: Added logger sync after /compress success (matching forced compression pattern)
5. **Fix #5**: Class mismatch now returns error to caller instead of silently overriding
6. **Fix #6**: `_format_message()` now returns a copy (`dict(message)`) instead of mutating in-place
7. **Fix #7**: Added fraction validation (`max(0.1, min(0.9, fraction))`) in `_handle_compress_context` tool path
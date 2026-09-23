---
name: agentcascade-tool-unit-test-fake-pool
description: Unit-test AgentCascade BaseTool subclasses (file_ops etc.) by injecting a fake agent_pool with a recording operation_manager — no LLM, no filesystem.
source: auto-generated
version: "1.0.0"
triggers:
  - "test write_file"
  - "test edit_file"
  - "unit test tool call"
  - "agent_pool fake"
  - "operation_manager mock"
generated_by: coder
generated_from_task: "Regression test for edit_file/write_file fence-stripping bug in file_ops.py"
---

## Goal
Test an AgentCascade `BaseTool` subclass's `call()` logic in isolation — assert exactly what args it forwards to the filesystem/operation manager — without an LLM, network, or real file I/O.

## Procedure

### Step 1 — Build a recording operation manager
The tool calls `self.agent_pool.operation_manager.<method>(...)`. Make a stub that records the exact kwargs:
```python
class _RecordingOM:
    def __init__(self):
        self.write = None   # (path, content)
        self.edit  = None   # (path, old_content, new_content, match_mode)
    def write_file(self, path, content, agent_name=None, justification=''):
        self.write = (path, content); return 'written'
    def edit_file(self, path, agent_name=None, old_content=None, new_content=None,
                  match_mode='exact', range_param=None, justification=''):
        self.edit = (path, old_content, new_content, match_mode); return 'edited'

class _FakePool:
    def __init__(self, om): self.operation_manager = om
```
Match the real `OperationManager` method signatures (keyword args) so the tool's call site binds correctly.

### Step 2 — Construct the REAL tool via kwargs (do NOT set attributes after the fact)
`BaseTool.__init__(cfg)` sets cfg; each subclass `__init__` reads `agent_pool`/`agent_name` from **kwargs**:
```python
def _tool(tool_cls, om):
    return tool_cls(agent_pool=_FakePool(om), agent_name='tester')
```
This is the pattern used in `tests/test_delete_file.py::_make_tool`. It exercises the real `call()` end-to-end (JSON parse → schema validate → forward).

### Step 3 — Drive both param shapes and assert forwarded values
Tools accept `params` as a **str** (JSON) or a **dict** (native function calling). Test BOTH to cover `_verify_json_format_args`:
```python
t.call(json.dumps({'path': 'x.md', 'content': payload}))   # str path
t.call({'path': 'x.md', 'content': payload})                # dict path
assert om.write[1] == payload, f"corrupted: {om.write[1]!r}"
```

### Step 4 — Run standalone (project uses pytest + xdist)
`python -m pytest tests/test_your.py -v -p no:xdist -o addopts=""` from the project root. The conftest auto-detects a local LLM and skips live tests; plain unit tests run regardless.

## Tips
- `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` at top so the import of `agent_cascade...` works when pytest is invoked from anywhere.
- Assert on the **forwarded** value (what the OM received), not the tool's return string — that's what actually hits disk.
- When pinning legacy/fallback behavior, run it once first and assert the ACTUAL output; don't trust a hand-written expectation (e.g. a regex like `(.*?)\n?``` ` also strips the trailing newline — expected `"print(1)"`, not `"print(1)\n"`).
- If the tool has a non-JSON fallback path (e.g. WriteFile's `path\n```code```` regex), add a test that it still behaves as intended so the fix doesn't silently change it.
- Reference tests to copy structure from: `tests/test_delete_file.py` (fake pool + real tool), `tests/test_heuristic_comment_fix.py` (pure logic simulation).

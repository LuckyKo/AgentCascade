#!/usr/bin/env python3
"""Regression: edit_file / write_file must preserve legitimate markdown code fences in
new_content / content. extract_code() previously stripped the outermost fence whenever the
value started with ```, silently dropping the fences the agent intended to write.

The fix removed the two extract_code() calls (and their now-unused local imports) from
WriteFile.call() and EditFile.call(). Content is now written exactly as delivered (the raw
parsed value). The legacy "path\\n```code```" non-JSON fallback in WriteFile.call() is a
separate path that intentionally strips the wrapper — it uses its own regex and must keep
working.

Unit approach: instantiate the real tool classes and inject a fake agent_pool whose
operation_manager records the exact args forwarded to the filesystem. No LLM, no network,
no real file I/O.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _RecordingOM:
    """Captures the exact args forwarded to the operation manager."""

    def __init__(self):
        self.write = None   # (path, content)
        self.edit = None    # (path, old_content, new_content, match_mode)

    def write_file(self, path, content, agent_name=None, justification=''):
        self.write = (path, content)
        return 'written'

    def edit_file(self, path, agent_name=None, old_content=None, new_content=None,
                  match_mode='exact', range_param=None, justification=''):
        self.edit = (path, old_content, new_content, match_mode)
        return 'edited'


class _FakePool:
    def __init__(self, om):
        self.operation_manager = om


def _tool(tool_cls, om):
    """Construct a real tool wired to a recording operation manager."""
    return tool_cls(agent_pool=_FakePool(om), agent_name='tester')


def test_write_file_preserves_fenced_content():
    """JSON-string path: a fenced block in `content` must survive intact."""
    from agent_cascade.tools.custom.file_ops import WriteFile
    om = _RecordingOM()
    t = _tool(WriteFile, om)
    payload = "```python\nprint('hi')\n```"
    t.call(json.dumps({'path': 'x.md', 'content': payload, 'justification': 't'}))
    assert om.write is not None
    assert om.write[1] == payload, f"fences dropped: {om.write[1]!r}"


def test_write_file_preserves_fenced_content_dict_args():
    """Dict-params path (native function calling): fenced content must survive intact."""
    from agent_cascade.tools.custom.file_ops import WriteFile
    om = _RecordingOM()
    t = _tool(WriteFile, om)
    payload = '# Title\n\n```\nscore = 1\n```\n'
    t.call({'path': 'x.md', 'content': payload})      # dict path (native function calling)
    assert om.write is not None
    assert om.write[1] == payload, f"fences dropped: {om.write[1]!r}"


def test_edit_file_preserves_fenced_new_content():
    """JSON-string path: a fenced block in `new_content` must survive intact."""
    from agent_cascade.tools.custom.file_ops import EditFile
    om = _RecordingOM()
    t = _tool(EditFile, om)
    new = '```\nscore = compute(a)\n```'
    t.call(json.dumps({'path': 'doc.md', 'old_content': 'PLACEHOLDER',
                       'new_content': new, 'match_mode': 'exact'}))
    assert om.edit is not None
    assert om.edit[2] == new, f"fences dropped: {om.edit[2]!r}"


def test_edit_file_preserves_fenced_new_content_dict_args():
    """Dict-params path: a fenced block in `new_content` must survive intact."""
    from agent_cascade.tools.custom.file_ops import EditFile
    om = _RecordingOM()
    t = _tool(EditFile, om)
    new = '```python\nx = 1\n```\n'
    t.call({'path': 'doc.md', 'old_content': 'OLD', 'new_content': new})
    assert om.edit is not None
    assert om.edit[2] == new, f"fences dropped: {om.edit[2]!r}"


def test_write_file_nonjson_fallback_still_strips_wrapper():
    """The legacy non-JSON 'path\\n```code```' fallback is preserved (intended behavior).

    This path uses its own regex (not extract_code) and intentionally strips the wrapper,
    forwarding only the inner content. It must keep working after the fix.
    """
    from agent_cascade.tools.custom.file_ops import WriteFile
    om = _RecordingOM()
    t = _tool(WriteFile, om)
    t.call('x.md\n```python\nprint(1)\n```')   # str, not starting with '{'
    assert om.write is not None
    # The fallback regex `(.*?)\n?```` strips the wrapper AND the trailing newline,
    # so only the bare inner content is forwarded. This is pre-existing behavior —
    # the fix does not touch this path; we just pin it as a regression guard.
    assert om.write[1] == 'print(1)', f"fallback strip changed: {om.write[1]!r}"

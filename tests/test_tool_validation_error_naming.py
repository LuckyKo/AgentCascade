"""BUG_0038 regression tests — schema-validation errors must be returned as
friendly ERROR strings that name the dispatched tool, never leaked as raw
jsonschema ValidationError text.

Root cause (see .bug_tracker/BUG_0038_validation_error_misreports_tool_schema.md):
BaseTool._verify_json_format_args raises a friendly ValueError naming the tool
and the missing/unknown parameters, but seven call sites in
tools/custom/file_ops.py invoked it bare, so the ValueError escaped as an
exception and was only rescued upstream by Agent._call_tool — which keeps the
crash shape ("An error occurred when calling tool ...") instead of a normal
tool result. In the originating run the raw ValidationError text
("Failed validating 'required' in schema: ...") reached the model.

Fix under test (option C): each of the seven sites wraps the call in
try/except ValueError and returns f"ERROR: {e}", matching ReadFile.call's
existing pattern.

Assertion layer: the string returned by BaseTool.call — never logger output or
Agent._call_tool (see plan Part 3: crash-message dedup makes log assertions
flaky).
"""
import pytest

from agent_cascade.tools.custom.file_ops import (
    DeleteFile,
    EditFile,
    Grep,
    ListDir,
    ReIndent,
    ViewImage,
    WriteFile,
)

# The seven wrapped sites. ViewImage is the class behind the screen-capture
# call site (file_ops.py:608).
WRAPPED_SITES = [
    ('view_image', lambda: ViewImage().call({})),
    ('write_file', lambda: WriteFile().call({'content': 'x'})),
    ('edit_file', lambda: EditFile().call({'new_content': 'x'})),
    ('list_dir', lambda: ListDir().call({'path': 42})),
    ('grep', lambda: Grep().call({'path': '.'})),
    ('delete_file', lambda: DeleteFile().call({'include': ['*.md']})),
    ('re_indent', lambda: ReIndent().call({'indent': 4, 'indent_type': 'space'})),
]


class TestBug0038Repro:
    """The exact BUG_0038 reproduction: edit_file called with propose_skill-shaped args."""

    def test_edit_file_missing_path_names_edit_file_and_lists_required(self):
        out = EditFile().call({'new_content': 'x'})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'edit_file' in out
        assert 'path' in out

    def test_edit_file_rejects_propose_skill_shaped_args_by_name(self):
        """The model sent propose_skill arguments to edit_file (originating run L185)."""
        out = EditFile().call({'name': 'x', 'rating': 9, 'skill_content': 'c'})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        # Must name the dispatched tool — never the intended one.
        assert 'edit_file' in out
        assert 'propose_skill' not in out
        # The message points at what edit_file actually requires...
        assert 'path' in out
        # ...not at a parameter of the other tool.
        assert 'skill_content' not in out


class TestEachWrappedSiteNamesItsTool:
    """One schema-tripping call per wrapped site; each must return an ERROR string."""

    def test_view_image_missing_path_names_tool(self):
        out = ViewImage().call({})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'view_image' in out
        assert 'path' in out

    def test_write_file_missing_path_names_tool(self):
        out = WriteFile().call({'content': 'x'})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'write_file' in out
        assert 'path' in out

    def test_re_indent_missing_lines_names_tool(self):
        out = ReIndent().call({'indent': 4, 'indent_type': 'space'})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 're_indent' in out
        assert 'lines' in out

    def test_list_dir_bad_path_type_names_tool(self):
        out = ListDir().call({'path': 42})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'list_dir' in out

    def test_grep_missing_pattern_names_tool(self):
        out = Grep().call({'path': '.'})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'grep' in out
        assert 'pattern' in out

    def test_delete_file_bad_include_type_names_tool(self):
        out = DeleteFile().call({'include': ['*.md']})
        assert isinstance(out, str)
        assert out.startswith('ERROR'), out
        assert 'delete_file' in out


class TestNoJsonschemaLeak:
    """Pins the actual observed defect: raw ValidationError text must never reach the model."""

    @pytest.mark.parametrize(
        ('tool_name', 'invoke'),
        WRAPPED_SITES,
        ids=[name for name, _ in WRAPPED_SITES],
    )
    def test_validation_error_never_leaks_jsonschema_preamble(self, tool_name, invoke):
        out = invoke()
        assert isinstance(out, str), f'{tool_name}: expected a returned string, got {type(out)}'
        assert 'Failed validating' not in out, f'{tool_name} leaked raw ValidationError text: {out}'
        assert 'jsonschema' not in out, f'{tool_name} leaked jsonschema internals: {out}'

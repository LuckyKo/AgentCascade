"""BUG_0229 — the auto-deny reason fragments must render grammatically.

``_auto_deny_reason`` returns fragments that are spliced into a fixed template used at
BOTH consumer sites (main-loop denial path ``tool_execution.py:648`` and the
orphan-placeholder path ``tool_execution.py:390``)::

    Tool '{tool}' was auto-denied because it is {reason} for this agent.

The template assumes every fragment is adjectival. The old third branch returned the
verb phrase ``'does not exist'`` → "it is does not exist" (ungrammatical). The fix makes
every fragment adjectival so ALL branches render grammatically with the EXISTING
template.

This test pins:
1. the exact fragment set (so a revert of any branch is caught, not just the ones a
   substring check happens to catch);
2. that both production templates are byte-identical (extracted from the source via AST,
   not restated in the test) and that each fragment renders grammatically in them;
3. the ``None`` guard (a present, non-disabled tool is allowed).
"""

import ast
import re
from pathlib import Path

from agent_cascade.engine.tool_execution import ToolExecMixin

_SRC = Path(__file__).resolve().parents[1] / 'agent_cascade' / 'engine' / 'tool_execution.py'

_SELF = object()  # _auto_deny_reason is a pure method (does not touch self)


def _extract_deny_templates():
    """Return the f-string templates that render the auto-deny sentence, each rebuilt as
    a ``str.format``-able string (``{tool_name}`` / ``{deny_reason}`` placeholders kept).

    There must be exactly two (main-loop ``_auto_deny_tool_call`` and the orphan
    placeholder path) and they must be byte-identical — that is the invariant this test
    guards, so it is read from the source rather than restated.
    """
    tree = ast.parse(_SRC.read_text(encoding='utf-8'))
    templates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                name = inner.id if isinstance(inner, ast.Name) else str(ast.dump(inner))
                parts.append('{' + name + '}')
        text = ''.join(parts)
        if 'was auto-denied because it is' in text and 'for this agent' in text:
            templates.append(text)
    return templates


def _reason(disabled_tools, function_map, tool_name):
    return ToolExecMixin._auto_deny_reason(
        _SELF, 'tpl', set(disabled_tools), dict(function_map), tool_name)


# (disabled_tools, function_map, tool_name, expected fragment) — one row per branch.
_EXPECTED = [
    (['x'], {}, 'x', 'disabled and absent'),   # disabled AND absent
    (['x'], {'x': 'fn'}, 'x', 'disabled'),      # disabled but present
    ([], {'y': 'fn'}, 'x', 'not available'),   # not disabled, absent
]


def _render(template, tool_name, reason):
    """Render a fragment into a production template (``{tool_name}`` / ``{deny_reason}``)."""
    return template.format(tool_name=tool_name, deny_reason=reason)


def test_fragments_are_the_expected_adjectival_set():
    """Each branch returns exactly the adjectival fragment it should (pins every branch,
    including 'disabled and absent', which a bare 'is does' substring check would miss)."""
    for disabled, fmap, name, want in _EXPECTED:
        assert _reason(disabled, fmap, name) == want


def test_both_templates_are_identical():
    """The main-loop and orphan-placeholder paths must use byte-identical denial
    templates (the invariant that keeps their rendered sentences in lockstep)."""
    templates = _extract_deny_templates()
    assert len(templates) == 2, f'expected exactly 2 denial templates, found {len(templates)}'
    assert templates[0] == templates[1], 'the two denial templates have drifted apart'


def test_all_branches_render_grammatically():
    """Each fragment renders grammatically in the production template: no 'is does'
    splice and no duplicated trailing 'for this agent' clause."""
    unique = set(_extract_deny_templates())
    assert len(unique) == 1, f'expected one unique denial template, found {len(unique)}'
    (template,) = unique
    for disabled, fmap, name, _ in _EXPECTED:
        sentence = _render(template, name, _reason(disabled, fmap, name))
        assert 'is does' not in sentence, f'ungrammatical splice: {sentence!r}'
        assert len(re.findall(r'for this agent', sentence)) == 1, \
            f'duplicated trailing clause: {sentence!r}'
        assert sentence.endswith('This tool cannot be used.'), sentence


def test_rendered_sentence_is_identical_across_paths():
    """Rendering the same fragment into the main-loop template and the orphan-placeholder
    template yields identical sentences (both sites stay in lockstep)."""
    main_template, orphan_template = _extract_deny_templates()
    for disabled, fmap, name, _ in _EXPECTED:
        frag = _reason(disabled, fmap, name)
        assert _render(main_template, name, frag) == _render(orphan_template, name, frag)


def test_present_non_disabled_tool_is_allowed():
    """GUARD: the ``None`` guard at the top of _auto_deny_reason — a tool that is present
    in function_map and not disabled is allowed (returns None); a missing template also
    allows it."""
    assert ToolExecMixin._auto_deny_reason(_SELF, None, {'x'}, {'x': 'fn'}, 'x') is None
    assert ToolExecMixin._auto_deny_reason(_SELF, 'tpl', set(), {'x': 'fn'}, 'x') is None

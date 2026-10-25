"""Regression tests for BUG_0100 — host-path mapping must respect path-segment boundaries.

The field failure: with the main repo registered as ``N:\\work\\WD\\AgentCascade``, a snippet
that read ``N:\\work\\WD\\AgentCascade-phase7\\...`` was rewritten to
``/extra_rw_0/-phase7/...`` because the resolver used a bare ``str.startswith()``. The sibling
directory was never a child of the mapping, so the rewrite produced a path that cannot exist.

Fix: a registered prefix matches only on a segment boundary — the whole path, or the prefix
followed by ``/``. Registration is also normalized (forward slashes, no trailing slash) so the
same folder cannot appear as two mappings.

These tests are self-contained: the resolver's module-level mapping table is saved/restored
per test, and no Docker kernel or LLM is involved.
"""

import pytest

import agent_cascade.utils.code_path_resolver as resolver
from agent_cascade.utils.code_path_resolver import (
    _resolve_single_path,
    get_active_mappings,
    resolve_code_paths,
    set_active_mappings,
)

MAIN = r'N:\work\WD\AgentCascade'
WORKTREE = r'N:\work\WD\AgentCascade-phase7\agent_cascade\engine\tool_execution.py'


@pytest.fixture(autouse=True)
def _isolate_mappings():
    """Keep the resolver's module-level state out of other tests (and vice versa)."""
    saved = dict(resolver._ACTIVE_PATH_MAPPINGS)
    saved_warned = resolver._warnings_issued
    resolver._ACTIVE_PATH_MAPPINGS = {}
    resolver._warnings_issued = True  # silence the "no mappings" one-shot warning
    try:
        yield
    finally:
        resolver._ACTIVE_PATH_MAPPINGS = saved
        resolver._warnings_issued = saved_warned


# ── The reported failure ──────────────────────────────────────────────────────

def test_sibling_directory_is_not_partially_rewritten():
    """Only the main repo is mounted; the -phase7 sibling must be left untouched.

    Pre-fix this returned '/extra_rw_0/-phase7/agent_cascade/...' — the reported stderr.
    """
    set_active_mappings({'work_dir': {'host': MAIN, 'container': '/workspace'}})

    assert _resolve_single_path(WORKTREE) == WORKTREE


def test_sibling_directory_is_not_rewritten_end_to_end():
    """Same guarantee through resolve_code_paths(), including a zero replacement count.

    A zero count matters: the caller shows the 'N path(s) auto-resolved' notice, so a bogus
    count would advertise a rewrite that never happened.
    """
    set_active_mappings({'work_dir': {'host': MAIN, 'container': '/workspace'}})
    code = f"p = r'{WORKTREE}'"

    resolved, count = resolve_code_paths(code)

    assert resolved == code
    assert count == 0


@pytest.mark.parametrize('sibling', [
    r'N:\work\WD\AgentCascade-old\file.py',
    r'N:\work\WD\AgentCascade2\file.py',
    r'N:\work\WD\AgentCascadefoo\file.py',
    r'N:\work\WD\AgentCascade_phase7\file.py',
    # Same name under a different parent must not match either.
    r'N:\other\AgentCascade\file.py',
])
def test_lookalike_paths_are_left_unchanged(sibling):
    set_active_mappings({'work_dir': {'host': MAIN, 'container': '/workspace'}})

    assert _resolve_single_path(sibling) == sibling


# ── Overlapping registrations: most-specific wins, never partial ──────────────

@pytest.mark.parametrize('reverse', [False, True], ids=['main-first', 'worktree-first'])
def test_both_mounts_use_the_most_specific_mapping(reverse):
    """When both folders are registered, the worktree maps to its own container."""
    entries = [
        ('work_dir', {'host': MAIN, 'container': '/workspace'}),
        ('extra_rw_0', {'host': WORKTREE.rsplit('\\', 3)[0], 'container': '/extra_rw_0'}),
    ]
    if reverse:
        entries.reverse()
    set_active_mappings(dict(entries))

    assert _resolve_single_path(WORKTREE) == '/extra_rw_0/agent_cascade/engine/tool_execution.py'


def test_nested_mappings_prefer_the_deepest_prefix():
    set_active_mappings({
        'work_dir': {'host': MAIN, 'container': '/workspace'},
        'extra_rw_0': {'host': r'N:\work\WD', 'container': '/extra_rw_0'},
    })

    assert _resolve_single_path(MAIN + r'\agent_cascade\engine\tool_execution.py') == \
        '/workspace/agent_cascade/engine/tool_execution.py'
    assert _resolve_single_path(r'N:\work\WD\Other\file.txt') == '/extra_rw_0/Other/file.txt'


def test_longer_mapping_does_not_match_its_shorter_parent_by_accident():
    """The reverse direction of the bug: the -phase7 mapping must not claim main-repo files."""
    set_active_mappings({
        'work_dir': {'host': MAIN, 'container': '/workspace'},
        'extra_rw_0': {'host': MAIN + '-phase7', 'container': '/extra_rw_0'},
    })

    assert _resolve_single_path(MAIN + r'\agent_cascade\engine\tool_execution.py') == \
        '/workspace/agent_cascade/engine/tool_execution.py'


# ── Registration normalization ────────────────────────────────────────────────

@pytest.mark.parametrize('host', [MAIN, MAIN + '\\', MAIN + '/', MAIN.replace('\\', '/')])
def test_trailing_slash_and_separator_style_do_not_change_matching(host):
    """'N:/x', 'N:\\x', 'N:/x/', 'N:\\x\\' are the same mount."""
    set_active_mappings({'work_dir': {'host': host, 'container': '/workspace'}})

    assert get_active_mappings() == {'N:/work/WD/AgentCascade': '/workspace'}
    assert _resolve_single_path(MAIN + r'\data.csv') == '/workspace/data.csv'


def test_equivalent_registrations_collapse_to_one_mapping():
    set_active_mappings({
        'extra_rw_0': {'host': MAIN, 'container': '/extra_rw_0'},
        'extra_rw_1': {'host': MAIN + '\\', 'container': '/extra_rw_1'},
    })

    assert get_active_mappings() == {'N:/work/WD/AgentCascade': '/extra_rw_1'}


def test_drive_root_mapping_still_matches_children():
    """Guard for the normalization change: 'N:/' collapses to 'N:' and must still match."""
    set_active_mappings({'work_dir': {'host': 'N:/', 'container': '/host_n'}})

    assert _resolve_single_path(r'N:\work\file.txt') == '/host_n/work/file.txt'


# ── Boundary at the end of the path ──────────────────────────────────────────

@pytest.mark.parametrize('path', [MAIN, MAIN + '\\', MAIN.replace('\\', '/')])
def test_exact_prefix_maps_to_the_container_root(path):
    set_active_mappings({'work_dir': {'host': MAIN, 'container': '/workspace'}})

    assert _resolve_single_path(path) == '/workspace'


def test_resolution_is_idempotent():
    set_active_mappings({'work_dir': {'host': MAIN, 'container': '/workspace'}})
    code = f"p = r'{MAIN}\\data.csv'"

    once, first_count = resolve_code_paths(code)
    twice, second_count = resolve_code_paths(once)

    assert once == "p = r'/workspace/data.csv'"
    assert first_count == 1
    assert twice == once
    assert second_count == 0

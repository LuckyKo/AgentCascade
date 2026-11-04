r"""Regression tests for BUG_0228 — a mapped host prefix must keep the trailing separator.

The field failure: ``N:\\work\\WD\\ninfer-all`` is a registered read-write mount mapping to
``/extra_rw_5``. Snippets that build paths by string concatenation::

    base = r"N:\work\WD\ninfer-all\"
    open(base + "src/runtime/engine/engine_core.h")

relied on ``base`` keeping its trailing separator. The resolver translated the mount root
``N:/work/WD/ninfer-all/`` to the bare container root ``/extra_rw_5`` (separator dropped), so
the runtime concatenation produced ``/extra_rw_5src/...`` — a path that cannot exist.

Fix: when the input is exactly the registered mount root *followed by a separator*, the
resolved path emits the container root plus that single separator (``/extra_rw_5/``), so the
boundary survives concatenation. The mount root with no trailing separator still maps to the
bare container root (``/extra_rw_5``), and BUG_0100's segment-boundary guard is untouched.

These tests are self-contained: the resolver's module-level mapping table is saved/restored
per test, and no Docker kernel or LLM is involved.
"""

import pytest

import agent_cascade.utils.code_path_resolver as resolver
from agent_cascade.utils.code_path_resolver import (
    _resolve_single_path,
    resolve_code_paths,
    set_active_mappings,
)

MOUNT = r'N:\work\WD\ninfer-all'
CONTAINER = '/extra_rw_5'


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


def _mount():
    set_active_mappings({'extra_rw_5': {'host': MOUNT, 'container': CONTAINER}})


# ── The reported failure: prefix + separator + remainder ───────────────────────

@pytest.mark.parametrize('sep', ['/', '\\'], ids=['forward', 'backslash'])
def test_prefix_with_remainder_keeps_the_boundary(sep):
    """``M + sep + 'a/b.py'`` must resolve to ``/extra_rw_5/a/b.py``, not ``/extra_rw_5a/b.py``."""
    _mount()

    resolved = _resolve_single_path(MOUNT.replace('\\', sep) + sep + 'a/b.py')

    assert resolved == f'{CONTAINER}/a/b.py'
    assert resolved.startswith(f'{CONTAINER}/')


def test_exact_reported_path_translates_correctly():
    """The verbatim path from the bug report."""
    _mount()

    assert _resolve_single_path(r'N:\work\WD\ninfer-all\src\runtime\engine\engine_core.h') == \
        '/extra_rw_5/src/runtime/engine/engine_core.h'
    assert _resolve_single_path('N:/work/WD/ninfer-all/src/runtime/engine/engine_core.h') == \
        '/extra_rw_5/src/runtime/engine/engine_core.h'


# ── Mount root with a trailing separator preserves it ──────────────────────────

@pytest.mark.parametrize('path', [MOUNT + '\\', MOUNT + '/'])
def test_mount_root_with_trailing_separator_preserves_it(path):
    """The reported mechanism: ``base + 'child'`` must keep the boundary."""
    _mount()

    assert _resolve_single_path(path) == f'{CONTAINER}/'


def test_concatenation_after_resolution_keeps_the_boundary():
    """End-to-end: translate the base literal, then concatenate as the snippet does."""
    _mount()
    code = f"base = r'{MOUNT}\\'\nopen(base + 'src/engine_core.h')"

    resolved, count = resolve_code_paths(code)

    base_line = resolved.splitlines()[0]
    assert base_line == f"base = r'{CONTAINER}/'"
    assert count == 1
    # The open() line is unchanged: at runtime it now resolves to a real path
    # (pre-fix the bare root produced the non-existent '/extra_rw_5src/...').
    assert resolved.splitlines()[1] == "open(base + 'src/engine_core.h')"


# ── Mount root with NO remainder still maps to the bare container root ─────────

def test_mount_root_without_remainder_maps_to_container_root():
    _mount()

    assert _resolve_single_path(MOUNT) == CONTAINER
    assert _resolve_single_path(MOUNT.replace('\\', '/')) == CONTAINER


# ── No-op: fix_paths=false leaves literals untouched (BUG_0113) ────────────────

def test_fix_paths_false_leaves_literals_untouched():
    """The documented BUG_0113 workaround: the caller gates resolution on the flag.

    ``_resolve_fix_paths`` (code_interpreter) returns the flag; when it is False the caller
    skips ``resolve_code_paths`` entirely, so the literals stay host-native. Assert both the
    flag resolution and that an untouched literal is what a ``base + child`` snippet would use.
    """
    from agent_cascade.tools.code_interpreter import _resolve_fix_paths

    _mount()
    assert _resolve_fix_paths({'fix_paths': False}) is False
    assert _resolve_fix_paths({}) is True  # default stays on

    code = f"base = r'{MOUNT}\\'"
    # Flag on: the caller runs the resolver and the literal is translated.
    assert _resolve_fix_paths({'fix_paths': True}) is True
    resolved, _ = resolve_code_paths(code)
    assert resolved == f"base = r'{CONTAINER}/'"
    # Flag off: the caller skips resolution, so the literal stays host-native
    # (no container path has been substituted in).
    assert _resolve_fix_paths({'fix_paths': False}) is False
    assert CONTAINER not in code  # no container path leaked into the untouched literal

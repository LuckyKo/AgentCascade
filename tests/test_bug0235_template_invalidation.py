"""BUG_0235 regression tests — mtime-based invalidation of the agent soul template cache.

The soul template cache (AgentPool.templates) used to be filled once at startup with no
invalidation path, so a soul edited on disk was never served to agents spawned later in the
same process. These tests drive a REAL AgentPool (mocked peripheral deps only, mirroring
tests/test_agent_pool.py fixture style) against a temporary agents dir, and assert the
mtime + content-hash double-check reloads a changed soul in-process while an mtime-only
touch does NOT fire a spurious notify_config_changed().
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_SOUL = """---
name: BugTest
tagline: Minimal test soul
---

You are a minimal test agent.
"""

_MARK = 'BUG0235-UNIQUE-MARKER'


@pytest.fixture
def template_pool(tmp_path):
    """A real AgentPool over a temp agents dir containing one minimal *_soul.md.

    OperationManager / TelemetryCollector / APIRouter are mocked exactly as in
    tests/test_agent_pool.py, but the router returns a plain-llm-cfg dict so the soul
    is actually parsed into an Assistant (no LLM connection is made at construction).
    """
    agents_dir = tmp_path / 'agents'
    agents_dir.mkdir()
    soul = agents_dir / 'bugtest_soul.md'
    soul.write_text(_SOUL, encoding='utf-8')

    llm_cfg = {
        'model': 'test_model',
        'model_server': 'http://localhost:1234/v1',
        'api_key': 'EMPTY',
    }

    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector'), \
         patch('agent_cascade.api_router.APIRouter') as mock_router:
        op_mgr = MagicMock()
        op_mgr.base_dir = MagicMock()
        op_mgr.base_dir.__str__ = lambda self: str(tmp_path / 'workspace')
        op_mgr.extra_work_folders_ro = []
        op_mgr.extra_work_folders_rw = []
        mock_op_mgr.return_value = op_mgr

        router = MagicMock()
        router.get_effective_concurrency.return_value = 1
        # Real dict so create_agent_from_soul() gets a usable (non-mock) LLM config.
        router.get_llm_config.return_value = llm_cfg
        router.default_llm_cfg = llm_cfg
        router._config_dir = tmp_path  # real path so pool-settings persistence is contained
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg=llm_cfg,
            agents_dir=str(agents_dir),
            workspace_dir=str(tmp_path / 'workspace'),
        )

    yield pool, soul


def _append_soul(soul: Path, text: str) -> None:
    """Append content to the soul file (caller updates mtime separately)."""
    with open(soul, 'a', encoding='utf-8') as f:
        f.write(text)


def _touch_forward(soul: Path) -> float:
    """Bump the mtime strictly forward, deterministically (coarse-fs safe)."""
    t = time.time() + 100
    os.utime(soul, (t, t))
    return os.path.getmtime(soul)


def _spy_notify(pool):
    """Wrap pool.notify_config_changed with a call counter; return the list."""
    calls = []
    original = pool.notify_config_changed
    pool.notify_config_changed = lambda: (calls.append(1), original())
    return calls


class TestSoulEditInvalidation:
    """On-disk soul edits must reach agents loaded later in the same process."""

    def test_soul_edit_is_served_in_process_and_notifies(self, template_pool):
        """A soul edit (marker line) is served in-process; hash changed + notify fired."""
        pool, soul = template_pool
        calls = _spy_notify(pool)

        # Baseline: cached template, no marker, hash + config version recorded.
        baseline = pool.get_template('bugtest')
        assert baseline is not None
        assert _MARK not in baseline.system_message
        baseline_hash = pool._compute_template_hash('bugtest')
        baseline_cv = pool._config_version
        assert len(calls) == 0, 'no reload should fire on the pristine first lookup'

        # Edit on disk + deterministic mtime bump.
        _append_soul(soul, f"\n{_MARK} unique line\n")
        _touch_forward(soul)

        # Same pool, same process — the marker must now be served.
        reloaded = pool.get_template('bugtest')
        assert _MARK in reloaded.system_message, 'edited soul was not served in-process'
        assert pool._compute_template_hash('bugtest') != baseline_hash, 'hash must change'
        assert len(calls) == 1, 'notify_config_changed must fire exactly once on a content change'
        assert pool._config_version == baseline_cv + 1, 'config version must increment'

    def test_mtime_only_touch_updates_mtime_without_notify(self, template_pool):
        """A bare mtime touch (identical bytes) refreshes bookkeeping but never notifies."""
        pool, soul = template_pool
        calls = _spy_notify(pool)

        # Establish a known edited state first, so the negative case is unambiguous.
        _append_soul(soul, f"\n{_MARK}\n")
        _touch_forward(soul)
        edited = pool.get_template('bugtest')
        assert _MARK in edited.system_message
        assert len(calls) == 1
        hash_after_edit = pool._compute_template_hash('bugtest')
        cv_after_edit = pool._config_version

        # Touch with NO content change.
        new_mtime = _touch_forward(soul)
        after = pool.get_template('bugtest')

        assert after.system_message == edited.system_message, 'content must be unchanged'
        assert pool._compute_template_hash('bugtest') == hash_after_edit, 'hash must be unchanged'
        assert len(calls) == 1, 'mtime-only touch must NOT fire notify_config_changed()'
        assert pool._config_version == cv_after_edit, 'config version must NOT increment'
        # Bookkeeping mtime must be refreshed to suppress repeated reloads.
        assert pool._template_mtime['bugtest'] == pytest.approx(new_mtime)

    def test_deleted_soul_keeps_cached_template(self, template_pool):
        """A deleted soul file keeps serving the cached template and must not raise."""
        pool, soul = template_pool
        cached = pool.get_template('bugtest')
        os.remove(soul)

        result = pool.get_template('bugtest')
        assert result is cached, 'deleted soul must keep serving the cached template'
        assert result is not None

    def test_template_without_source_is_returned_untouched(self, template_pool):
        """Synthetic/manual entries (no recorded soul) are returned without the stat check.

        Covers the plan edge case: a template registered directly into pool.templates
        has no bookkeeping source and must not be (and cannot be) reloaded.
        """
        pool, _soul = template_pool
        sentinel = object()
        pool.templates['synthetic'] = sentinel
        assert pool.get_template('synthetic') is sentinel
        assert 'synthetic' not in pool._template_source


class TestRefreshAndCaseHandling:
    """refresh_agents must keep working and repopulate bookkeeping; case must resolve."""

    def test_refresh_agents_updates_bookkeeping_then_serves_edit(self, template_pool):
        """refresh_agents() repopulates the mtime bookkeeping so later edits still work."""
        pool, soul = template_pool
        # refresh_agents() re-reads everything (via _discover_agents) and must record mtime.
        pool.refresh_agents()
        assert pool._template_source.get('bugtest') == str(soul)
        assert pool._template_mtime.get('bugtest') == pytest.approx(os.path.getmtime(soul))

        # A subsequent on-disk edit is still served after a refresh (bookkeeping not stale).
        _append_soul(soul, f"\n{_MARK}\n")
        _touch_forward(soul)
        assert _MARK in pool.get_template('bugtest').system_message

    def test_case_insensitive_lookup_reloads_the_same_bookkeeping_entry(self, template_pool):
        """Looking up via a different case must still hit (and reload) the registered key."""
        pool, soul = template_pool
        _append_soul(soul, f"\n{_MARK}\n")
        _touch_forward(soul)
        # 'Bugtest' resolves through the fallback chain to the registered 'bugtest'.
        assert _MARK in pool.get_template('Bugtest').system_message


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '--tb=short']))

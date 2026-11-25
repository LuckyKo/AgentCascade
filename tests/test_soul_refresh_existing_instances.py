"""Regression tests: "refresh souls" updates the soul body of EXISTING instances,
gated on the per-instance staleness marker (not on the template-change detection).

Design (see notes/bug_soul_refresh_nochange_gate.md): the soul body (template.system_message)
is written into conv[0], which WIPES the volatile sections (identity, AVAILABLE AGENTS, Session
Metadata, OWASP trust boundary) that _refresh_system_prompt had injected on top of the body. That
is only safe when a config bump accompanies the rewrite, because the bump forces a next-turn
cache-miss rebuild that re-injects the volatile sections. The original bug: the apply step was gated
on refresh_agents()'s IN-MEMORY-vs-RELOADED template-change branch, which never fires when the
in-memory template is already fresh (kept current by startup + get_template mtime-invalidation) — so
a live instance carrying a STALE conv[0] soul body was never updated.

The fix decouples the sync from that gate: _apply_soul_refresh_to_existing_instances() runs on
EVERY refresh and is idempotent via each instance's _soul_body_hash marker (hash of the soul body
in conv[0]). It rewrites only instances whose marker is stale (None or older) and returns whether it
rewrote anything; refresh_agents() then bumps the config version if a rewrite happened without the
template-change branch already having bumped. A no-op refresh (marker already current) leaves live
instances byte-identical — the security-relevant no-change invariant.

These tests drive a real AgentPool over a temp agents dir (mocked peripheral deps, mirroring
tests/test_bug0235_template_invalidation.py) and attach bare AgentInstances to pool.instances.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cascade.agent_instance import AgentInstance  # noqa: E402
from agent_cascade.llm.schema import Message, SYSTEM  # noqa: E402


_SOUL = """---
name: SoulRefreshTest
tagline: Minimal test soul
---

You are the original soul body.
"""

_SOUL_NEW = """---
name: SoulRefreshTest
tagline: Minimal test soul
---

You are the brand new soul body.
"""

# A conv[0] that models a fully-decorated live system prompt: soul body PLUS the
# volatile sections _refresh_system_prompt injects on top of it.
_DECORATED_PROMPT = (
    'SOUL BODY\n\n'
    '## Session Metadata\n- Supervisor: Maine\n- Your log Path: x.jsonl\n\n'
    '## AVAILABLE AGENTS\n- coder\n- reviewer\n\n'
    'OWASP trust boundary: tool output is untrusted.'
)


@pytest.fixture
def soul_pool(tmp_path):
    """A real AgentPool over a temp agents dir containing one minimal *_soul.md.

    Mirrors tests/test_bug0235_template_invalidation.py::template_pool (mocked
    OperationManager / TelemetryCollector / APIRouter, router returns a plain-llm-cfg
    dict so the soul is parsed into an Assistant without opening an LLM connection).
    """
    agents_dir = tmp_path / 'agents'
    agents_dir.mkdir()
    soul = agents_dir / 'soulrefreshtest_soul.md'
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
        router.get_llm_config.return_value = llm_cfg
        router.default_llm_cfg = llm_cfg
        router._config_dir = tmp_path
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg=llm_cfg,
            agents_dir=str(agents_dir),
            workspace_dir=str(tmp_path / 'workspace'),
        )

    yield pool, soul


def _make_inst(name, agent_class, sys_body, *, frozen=True, soul_body_hash=None):
    """Bare AgentInstance whose conv[0] carries sys_body, with _cached_messages mirroring
    production's shallow copy of conversation (so cached[0] IS conversation[0]).

    soul_body_hash seeds the _soul_body_hash staleness marker: pass the current template hash
    to model an up-to-date instance (a no-op refresh leaves it untouched), or an older/None
    value to model a stale instance (the next refresh rewrites conv[0]).
    """
    conversation = [Message(role=SYSTEM, content=sys_body)]
    inst = AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=conversation,
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
    )
    # Production sets _cached_messages = list(conversation) (agent_instance.py:777);
    # the shallow copy means conv[0] and cached[0] are the SAME Message object.
    inst._cached_messages = conversation
    inst._system_prompt_frozen = frozen
    inst._soul_body_hash = soul_body_hash
    return inst


def _template_key(pool):
    """Return the discovered template key for the temp soul (robust to discovery casing)."""
    assert pool.templates, 'template discovery produced no templates'
    return next(iter(pool.templates))


class TestApplySoulRefresh:
    """Unit-level checks of _apply_soul_refresh_to_existing_instances() (direct calls)."""

    def test_updates_existing_instance_shared_cached_object(self, soul_pool):
        """Shared-object case: conv[0] is rewritten and the freeze flag is cleared. The
        forced rebuild (not this method) is what re-injects the volatile sections.
        """
        pool, _ = soul_pool
        key = _template_key(pool)
        pool.templates[key].system_message = 'NEW SOUL BODY'

        inst = _make_inst('Live1', key, 'OLD SOUL BODY')
        # Mirror the shallow-copy identity invariant of the common production path.
        assert inst._cached_messages[0] is inst.conversation[0]
        pool.instances['Live1'] = inst

        pool._apply_soul_refresh_to_existing_instances()

        # conv[0] carries the new raw body; cached[0] is the SAME object so it matches.
        assert inst.conversation[0].content == 'NEW SOUL BODY'
        assert inst._cached_messages[0].content == 'NEW SOUL BODY'
        assert inst._system_prompt_frozen is False

    def test_skips_removed_agent(self, soul_pool):
        """An instance whose agent_class is no longer in templates is left untouched."""
        pool, _ = soul_pool
        _template_key(pool)  # sanity: at least one template exists on disk

        inst = _make_inst('Ghost1', 'ghostclass_removed', 'OLD SOUL BODY')
        pool.instances['Ghost1'] = inst

        pool._apply_soul_refresh_to_existing_instances()

        assert inst.conversation[0].content == 'OLD SOUL BODY'
        assert inst._system_prompt_frozen is True

    def test_handles_empty_conversation_and_dict_form(self, soul_pool):
        """Empty-conversation instance is skipped; a dict-shaped conv[0] is handled."""
        pool, _ = soul_pool
        key = _template_key(pool)
        pool.templates[key].system_message = 'NEW SOUL BODY'

        # Empty conversation → skipped without raising or touching frozen.
        empty = _make_inst('Empty1', key, 'ignored')
        empty.conversation = []
        empty._cached_messages = []
        pool.instances['Empty1'] = empty

        # dict-shaped conv[0] → set via ['content'].
        dict_inst = _make_inst('Dict1', key, 'ignored')
        dict_inst.conversation = [{'role': SYSTEM, 'content': 'OLD'}]
        dict_inst._cached_messages = dict_inst.conversation
        pool.instances['Dict1'] = dict_inst

        pool._apply_soul_refresh_to_existing_instances()

        assert dict_inst.conversation[0]['content'] == 'NEW SOUL BODY'
        assert dict_inst._system_prompt_frozen is False
        # Empty instance untouched (skipped): still empty, still frozen.
        assert len(empty.conversation) == 0
        assert empty._system_prompt_frozen is True


class TestRefreshAgentsEndToEnd:
    """refresh_agents() wiring: the soul-sync runs on EVERY call but is idempotent via the
    per-instance _soul_body_hash marker; the config version is bumped iff a rewrite happened."""

    def test_no_change_refresh_leaves_instances_untouched(self, soul_pool):
        """In-memory template already fresh (change branch is a no-op) AND the instance's
        marker already matches the current hash → the always-run sync skips it, leaving a
        fully-decorated prompt (volatile sections intact, trust boundary present), the frozen
        flag, and the config version all untouched. This is the security-relevant no-change
        invariant: a no-op refresh must not strip a decorated prompt without a rebuild.
        """
        pool, _ = soul_pool
        key = _template_key(pool)
        current_hash = pool._compute_template_hash(key)

        # Up-to-date instance: its marker equals the current template hash, so the always-run
        # soul-sync skips it (no volatile strip, no config bump).
        inst = _make_inst('LiveNoChange', key, _DECORATED_PROMPT, soul_body_hash=current_hash)
        assert inst._system_prompt_frozen is True
        pool.instances['LiveNoChange'] = inst
        cv_before = pool._config_version

        pool.refresh_agents()  # no on-disk change → change branch no-op; marker current → sync skips

        # The decorated prompt must NOT be stripped to the raw body...
        assert inst.conversation[0].content == _DECORATED_PROMPT
        # ...the trust boundary (the OWASP guard) must survive verbatim...
        assert 'trust boundary' in inst.conversation[0].content
        # ...the instance must remain frozen (we never touched it)...
        assert inst._system_prompt_frozen is True
        # ...and the config version must NOT bump (true no-op).
        assert pool._config_version == cv_before

    def test_change_refresh_updates_instances(self, soul_pool):
        """On a real on-disk edit the change branch fires: notify_config_changed() bumps
        the version AND the apply step rewrites conv[0] to the new soul body and unfreezes
        it, so the next turn's forced rebuild re-injects the volatile sections.
        """
        pool, soul = soul_pool
        key = _template_key(pool)

        inst = _make_inst('LiveChange', key, 'STALE SOUL BODY')
        assert inst._system_prompt_frozen is True
        pool.instances['LiveChange'] = inst
        cv_before = pool._config_version

        # Edit the soul on disk → content hash changes → change branch fires.
        soul.write_text(_SOUL_NEW, encoding='utf-8')
        pool.refresh_agents()

        new_body = pool.templates[key].system_message
        assert new_body  # reloaded from disk
        assert new_body != 'STALE SOUL BODY'
        assert inst.conversation[0].content == new_body
        assert inst._system_prompt_frozen is False
        # The change-branch rewrite is paired with a config bump (the invariant), so the next
        # turn's forced rebuild re-injects the volatile sections on top of the new soul body.
        assert pool._config_version > cv_before

    def test_stale_instance_synced_when_template_already_fresh(self, soul_pool):
        """EXACT bug repro (bug_soul_refresh_nochange_gate.md): the in-memory template is
        already up-to-date (so the in-memory-vs-reloaded template-change branch is a NO-OP),
        but an existing instance carries a STALE conv[0] soul body (its _soul_body_hash is
        older than the current template hash). The decoupled, always-run soul-sync must still
        rewrite that instance and bump the config version. A SECOND refresh (now up-to-date)
        must be a true no-op — no config bump, conv[0] byte-identical.
        """
        pool, _ = soul_pool
        key = _template_key(pool)
        current_hash = pool._compute_template_hash(key)

        # In-memory template is already fresh: no on-disk change, so refresh_agents() takes the
        # no-change branch. The instance's marker is OLDER than the current hash → stale conv[0].
        assert current_hash is not None
        inst = _make_inst('Stale1', key, _DECORATED_PROMPT, soul_body_hash='STALE_OLD_HASH')
        assert inst._soul_body_hash != current_hash
        assert inst._system_prompt_frozen is True
        pool.instances['Stale1'] = inst
        cv_before = pool._config_version

        pool.refresh_agents()  # template-change branch is a no-op; marker stale → sync must fire

        # The SERVED conv[0] must now carry the CURRENT template soul body (not the decorated
        # stale body), proving the rewrite reached the message that gets rebuilt — not just a
        # template variable. Volatile sections are re-injected on the next-turn forced rebuild.
        assert inst.conversation[0].content == pool.templates[key].system_message
        # The instance is unfrozen so _refresh_system_prompt re-injects the volatile sections.
        assert inst._system_prompt_frozen is False
        # The marker is now stamped to the current hash, so the staleness is recorded correctly.
        assert inst._soul_body_hash == pool._compute_template_hash(key)
        # The rewrite is paired with a config bump (the invariant) so the next turn rebuilds.
        assert pool._config_version == cv_before + 1

        # SECOND refresh: marker now current and no disk change → true no-op.
        conv0_before = inst.conversation[0].content
        frozen_before = inst._system_prompt_frozen
        cv_before2 = pool._config_version
        pool.refresh_agents()

        assert pool._config_version == cv_before2  # no bump on a no-op refresh
        assert inst.conversation[0].content == conv0_before  # byte-identical
        assert inst._system_prompt_frozen is frozen_before  # untouched (sync skipped it)

"""E2E swap -> functional proof for BUG_0364 / BUG_0372.

Why this test exists
--------------------
No test in the repo drives a REAL engine turn-setup against a REAL pool after a
REAL stack swap. The swap tests (``test_stack_swap_primitive.py``,
``test_stack_api_e2e.py``, ``test_stack_sessions_backend.py``) use the
``PoolDouble`` from ``tests/_snapshot_fleet_double`` and never run an engine
turn; the runtime identity test (``test_session_swap_identity_runtime.py``) uses
a ``SessionIOMixin`` mock pool and calls ``_refresh_system_prompt`` directly, so
it never exercises the pool swap primitives. This leaves the end-to-end contract
"garden a frozen, swapped-in instance whose log retains the SOUL name -> its live
prompt self-heals to the instance name and is otherwise functional" untested.

This test closes that gap with the real objects: ``AgentPool(DUMMY_LLM_CFG)`` +
the real swap primitives (``park_active_stack`` / ``activate_stack`` ->
``restore_fleet_snapshot``) + the real ``ExecutionEngine._setup_turn``.

Non-vacuity (BUG_0364 regression gate)
--------------------------------------
The swapped-in instance is restored by ``restore_fleet_snapshot`` whose
``_instantiate_instance`` leaves ``conversation`` empty and phase 5a's
``_reattach_instance_log`` fills it from the ON-DISK log. That log still says
"\"You are Orchestrator.\"" because ``_refresh_system_prompt`` only heals the
IN-MEMORY prompt (BUG_0364); the log file is never patched. The restore also
re-applies ``_system_prompt_frozen=True`` (snapshot.py:988). So the swapped-in
instance genuinely needs the identity self-heal and is frozen.

Before the BUG_0372/BUG_0364 decouple, ``_refresh_system_prompt`` gated the WHOLE
rewrite block (identity included) behind ``not sys_prompt_frozen`` (the old
``if m0_role == SYSTEM and not sys_prompt_frozen:``), so a frozen swapped-in
instance never healed -> assertion (a) fails. After the decouple, the identity
rewrite runs unconditionally (idempotent / KV-safe) and only the volatile
sections are gated -> assertion (a) passes. Reintroducing the old gate makes (a)
FAIL; see the plan's non-vacuity note.
"""

import json

import pytest

from agent_cascade.agent_pool import AgentPool
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import SYSTEM, Message
from agent_cascade.snapshot import (
    activate_stack,
    load_snapshot_from_disk,
    park_active_stack,
    _ensure_registry,
)

# Same dead-server LLM config used by the real-pool pattern in
# tests/test_session_load_dup_fix.py — NO real LLM endpoint is ever contacted.
DUMMY_LLM_CFG = {
    'model': 'qwen/qwen3-4b',
    'model_server': 'http://127.0.0.1:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
}

SOUL_NAME = 'Orchestrator'
INSTANCE = 'Main'
STACK = 'A'
SOUL_IDENTITY = f'You are {SOUL_NAME}.'
SOUL_SYSTEM_LINE = f'{SOUL_IDENTITY} Technical lead.'


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_soul_log(tmp_path, instance_name=INSTANCE):
    """Write a JSONL session log whose system line carries the SOUL name.

    Mirrors tests/test_session_swap_identity_runtime.py::_make_log_file so the
    load reuses this file in place (same_identity), which is exactly the state a
    swap-restored instance must heal from.
    """
    log_name = f'orchestrator_{instance_name}_e2e.jsonl'
    log_file = tmp_path / log_name
    lines = [
        {'metadata': {
            'agent_class': 'orchestrator',
            'instance_name': instance_name,
            'current_log_path': str(log_file),
        }},
        {'role': 'system', 'content': SOUL_SYSTEM_LINE},
        {'role': 'user', 'content': 'Do the thing.'},
        {'role': 'assistant', 'content': 'Done.'},
    ]
    with open(log_file, 'w', encoding='utf-8') as f:
        for m in lines:
            f.write(json.dumps(m) + '\n')
    return log_file


@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch, tmp_path):
    """Swap is gated OFF by default; turn it on and isolate persistence to tmp."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
    cfg = tmp_path / 'config'
    cfg.mkdir(exist_ok=True)
    monkeypatch.setenv('AGENT_CASCADE_TEST_CONFIG_DIR', str(cfg))


@pytest.fixture
def pool(tmp_path):
    """A REAL AgentPool pointed at tmp_path logs; stopped on teardown."""
    p = AgentPool(DUMMY_LLM_CFG)
    yield p
    try:
        p.stop()
    except Exception:
        pass


# ── Tier A: real pool + real swap + real _setup_turn (deterministic) ─────────


class TestSwapFunctionalE2E:

    def test_swapped_in_frozen_instance_self_heals_and_is_functional(self, pool, tmp_path):
        """(a)-(e) hold for a swapped-in frozen instance whose log keeps the soul name."""
        log_file = _write_soul_log(tmp_path)

        # 1. Seed the real pool with a session-loaded instance 'Main'. The load
        #    reuses this log in place (same identity) and sets _system_prompt_frozen
        #    = True (BUG_0372, session_io.py:784) AFTER the in-memory metadata
        #    self-heal — so conv[0] still reads the soul name and the on-disk log
        #    retains it.
        status = pool.load_session_from_log(
            str(log_file), target_instance=INSTANCE, clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f'session load failed: {status}'

        loaded = pool.instances[INSTANCE]
        assert loaded._system_prompt_frozen is True, 'precondition: loaded instance is frozen'
        assert SOUL_IDENTITY in loaded.conversation[0].content, (
            f'precondition: loaded prompt should keep the soul name, got '
            f'{loaded.conversation[0].content[:80]!r}')
        len_before = len(loaded.conversation)

        # 2. Mark this stack active so the park primitive has a real active stack.
        reg = _ensure_registry(pool)
        reg.register(STACK)
        reg.set_active(STACK)

        # 3. Park the active stack into an on-disk snapshot and empty the pool.
        #    grace_seconds=0.0 + no-op sleeper/clock -> fully deterministic (the
        #    loaded instance is idle, no in-flight turn to drain).
        env = park_active_stack(
            pool, stack_id=STACK, grace_seconds=0.0,
            sleeper=lambda *_a, **_k: None, clock=lambda: 0.0)
        assert env is not None and env.stack_id == STACK, 'park must return the source envelope'
        assert pool.instances.get(INSTANCE) is None, 'park must empty the pool'

        # 3b. (d-partial) The parked stack is persisted and re-activatable from disk.
        snap_path = reg.resolve_path(STACK, env.snapshot_id)
        assert snap_path is not None
        disk_env = load_snapshot_from_disk(snap_path)
        assert disk_env.stack_id == STACK, 'parked snapshot must reload with the same stack id'

        # 4. Re-activate (restore) the parked stack. restore_fleet_snapshot's
        #    _instantiate_instance leaves conversation empty and phase 5a's
        #    _reattach_instance_log fills it from the on-disk LOG (soul name), while
        #    _system_prompt_frozen=True is re-applied (snapshot.py:988).
        activate_stack(pool, env, gate_checked=True)

        live = pool.instances[INSTANCE]
        assert live is not None, 'restored instance must be re-installed'

        # Forced regression precondition: the swapped-in prompt genuinely needs
        # healing (still the soul name) AND is frozen. If a future swap path ever
        # carried an already-healed identity, this assert fails loudly so the test
        # never silently stops exercising the regression.
        assert SOUL_IDENTITY in live.conversation[0].content, (
            f'regression precondition: swapped-in prompt must still carry the soul '
            f'name, got {live.conversation[0].content[:80]!r}')
        assert not live.conversation[0].content.startswith(f'You are {INSTANCE}.'), (
            'precondition: swapped-in prompt must NOT already be healed before setup_turn')
        assert live._system_prompt_frozen is True, 'precondition: swapped-in instance is frozen'

        # 5. Run the REAL turn-setup that carries the identity regression. Force a
        #    cache miss so the M1 block actually runs (a restored instance has no
        #    warm cache; -1 makes the rebuild deterministic).
        engine = ExecutionEngine(pool)
        live._last_config_version = -1
        engine._setup_turn(live)

        # 6. FUNCTIONALITY assertions.
        # (a) identity self-heals to the instance name.
        assert live.conversation[0].content.startswith(f'You are {INSTANCE}.'), (
            f'live system prompt should start with "You are {INSTANCE}.", got '
            f'{live.conversation[0].content[:80]!r}')
        # (b) no stale soul identity token anywhere in the live prompt.
        assert SOUL_IDENTITY not in live.conversation[0].content, (
            'stale soul identity leaked into the healed live prompt')
        # (c) conversation state preserved through the swap.
        assert len(live.conversation) >= len_before, (
            f'conversation length shrunk through swap: {len_before} -> {len(live.conversation)}')
        # (d) the source stack is back as the active stack (re-activated).
        assert reg.active_stack_id == STACK, 'active stack must point back at the swapped-in stack'
        # (e) the KV-preservation invariant still holds post-swap (the prompt is
        #     re-frozen at the end of _setup_turn by _sync_turn_cache).
        assert live._system_prompt_frozen is True, (
            'KV-preservation invariant: swapped-in instance must remain frozen')

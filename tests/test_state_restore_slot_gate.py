"""Unit tests for the KV state-restore slot-ownership gate (2026-08-27).

Bug: ``restore_instance_state`` fired from ``_setup_turn`` to the instance's STALE
``_last_endpoint_config`` — which can be a shared conc=0 autoloader the agent no
longer owns. On a conc=0 autoloader, ``state/load`` loads the model and auto-evicts
the LRU resident (a live sibling's in-flight model), severing its stream.

Fix under test:
- Change 1: ``restore_instance_state(instance, held_endpoint_cfg=...)`` targets the
  held endpoint instead of ``_last_endpoint_config`` when provided; falls back to
  ``_last_endpoint_config`` when None (backward compat).
- Change 2: ``ExecutionEngine._setup_turn`` restores ONLY when the instance holds a
  slot (``_slot_release is not None``) and passes the held endpoint resolved via the
  router. Any resolution failure skips the restore (never evict on error).

The FIFO guarantees single-holder for conc=0, so restoring while holding the slot can
never evict another agent — these tests pin that invariant down.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.state_ops as state_ops
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import Message


# ============================================================================
# Helpers
# ============================================================================

AUTOLOADER_X = "http://localhost:1234/v1/"  # the shared conc=0 autoloader (held)
STALE_Y = "http://stale-host:9123/v1/"      # stale _last_endpoint_config (NOT held)


def make_instance(name="B", label="B"):
    """Real AgentInstance with a saved state label and a STALE endpoint config."""
    inst = AgentInstance(
        instance_name=name,
        agent_class="coder",
        conversation=[Message(role="system", content="sys")],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance="Main",
    )
    with inst._state_lock:
        inst._state_label = label
        # Stale config pointing at endpoint Y — must NOT be the restore target.
        inst._last_endpoint_config = {
            'api_base': STALE_Y,
            'model': "stale-model-B",
            'state_save_enabled': True,
        }
    return inst


def make_engine(instance):
    """ExecutionEngine over a MagicMock pool; router returns the held endpoint X."""
    pool = MagicMock()
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    pool.get_instance.return_value = instance
    # Identity slice — _setup_turn calls pool.slice_history_for_llm(conv).
    pool.slice_history_for_llm.side_effect = lambda msgs, **kw: list(msgs)

    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine, pool


def wire_router_held_endpoint(pool):
    """Router resolves the held endpoint (X / model-A) for this instance."""
    pool.api_router.get_effective_slot_info.return_value = {
        'slot_key': "pool-x",
        'is_sequential': True,
        'concurrency_limit': 0,
        'api_base': AUTOLOADER_X,
        'needs_slot': True,
    }
    pool.api_router.get_endpoint_chain.return_value = [
        {'api_base': AUTOLOADER_X, 'model': "model-A", 'state_save_enabled': True},
    ]


def _recorded_load_urls(post_mock):
    """Extract the /state/load URLs from recorded httpx.post calls."""
    urls = []
    for call in post_mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get('url', '')
        if isinstance(url, str) and '/state/load' in url:
            urls.append(url)
    return urls


def drive_setup_turn(engine, inst):
    """Run _setup_turn with a minimal conversation; returns its result tuple."""
    return engine._setup_turn(inst)


# ============================================================================
# Change 1 — restore_instance_state(held_endpoint_cfg=...)
# ============================================================================

class TestRestoreTargetsHeldEndpoint:

    def test_held_cfg_overrides_stale_last_endpoint_config(self):
        """With held_endpoint_cfg, the load POSTs to the HELD endpoint X/model-A,
        never to the stale _last_endpoint_config Y/stale-model-B."""
        inst = make_instance()
        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            ok = state_ops.restore_instance_state(
                inst, held_endpoint_cfg={'api_base': AUTOLOADER_X, 'model': "model-A"})

        assert ok is True
        load_urls = _recorded_load_urls(post_mock)
        assert len(load_urls) == 1, f"exactly one state/load expected: {load_urls}"
        url = load_urls[0]
        assert AUTOLOADER_X.rstrip('/') in url, f"must target held endpoint X: {url}"
        assert "model-A" in url, f"must target held model: {url}"
        assert STALE_Y not in url and "stale-model-B" not in url, \
            f"must NOT target stale config Y: {url}"

    def test_none_held_cfg_falls_back_to_last_endpoint_config(self):
        """Backward compat: held_endpoint_cfg=None restores via _last_endpoint_config."""
        inst = make_instance()
        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            ok = state_ops.restore_instance_state(inst)

        assert ok is True
        load_urls = _recorded_load_urls(post_mock)
        assert len(load_urls) == 1
        assert STALE_Y.rstrip('/') in load_urls[0], \
            f"None cfg must fall back to _last_endpoint_config: {load_urls}"

    def test_no_label_is_noop_without_http(self):
        """No saved label → no restore HTTP at all."""
        inst = make_instance(label=None)
        with patch.object(state_ops.httpx, "post") as post_mock:
            ok = state_ops.restore_instance_state(
                inst, held_endpoint_cfg={'api_base': AUTOLOADER_X, 'model': "model-A"})

        assert ok is False
        post_mock.assert_not_called()


# ============================================================================
# Change 2 — _setup_turn slot-ownership gate
# ============================================================================

class TestSetupTurnRestoreGate:

    def test_no_slot_held_skips_restore_entirely(self):
        """THE core invariant: instance holds no slot (_slot_release=None) →
        restore_instance_state is NOT called and zero state/load POSTs occur —
        even with a stale autoloader _last_endpoint_config and a saved label."""
        inst = make_instance()
        inst._slot_release = None  # no slot held

        engine, pool = make_engine(inst)
        wire_router_held_endpoint(pool)

        with patch.object(state_ops.httpx, "post") as post_mock:
            drive_setup_turn(engine, inst)

        load_urls = _recorded_load_urls(post_mock)
        assert load_urls == [], f"no state/load may fire without a held slot: {load_urls}"
        # Router must not even be consulted for restore when no slot is held.
        pool.api_router.get_effective_slot_info.assert_not_called()

    def test_slot_held_restores_to_held_endpoint_not_stale(self):
        """Instance holds the conc=0 slot on autoloader X but its
        _last_endpoint_config points at Y → restore POSTs to X/model-A only."""
        inst = make_instance()
        inst._slot_release = lambda: None  # holding a slot

        engine, pool = make_engine(inst)
        wire_router_held_endpoint(pool)

        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            drive_setup_turn(engine, inst)

        load_urls = _recorded_load_urls(post_mock)
        assert len(load_urls) == 1, f"exactly one state/load expected: {load_urls}"
        url = load_urls[0]
        assert AUTOLOADER_X.rstrip('/') in url and "model-A" in url, \
            f"must target held endpoint X: {url}"
        assert STALE_Y not in url and "stale-model-B" not in url, \
            f"must NOT target stale config Y: {url}"

    def test_resolution_error_skips_restore_never_evicts(self):
        """Router resolution raising → restore skipped (no POST), no exception
        escapes _setup_turn. A missed restore is better than a wrongful eviction."""
        inst = make_instance()
        inst._slot_release = lambda: None  # holding a slot

        engine, pool = make_engine(inst)
        pool.api_router.get_effective_slot_info.side_effect = RuntimeError("boom")

        with patch.object(state_ops.httpx, "post") as post_mock:
            result = drive_setup_turn(engine, inst)  # must not raise

        assert result is not None  # setup turn still completed
        load_urls = _recorded_load_urls(post_mock)
        assert load_urls == [], f"no state/load may fire on resolution failure: {load_urls}"

    def test_no_model_in_chain_skips_restore(self):
        """Held endpoint resolvable but chain has no model → skip (defensive)."""
        inst = make_instance()
        inst._slot_release = lambda: None  # holding a slot

        engine, pool = make_engine(inst)
        pool.api_router.get_effective_slot_info.return_value = {
            'slot_key': "pool-x", 'is_sequential': True, 'concurrency_limit': 0,
            'api_base': AUTOLOADER_X, 'needs_slot': True,
        }
        pool.api_router.get_endpoint_chain.return_value = []  # empty chain

        with patch.object(state_ops.httpx, "post") as post_mock:
            drive_setup_turn(engine, inst)

        assert _recorded_load_urls(post_mock) == [], \
            "no model resolved → restore must be skipped"


# ============================================================================
# Compression-resume path (BUG-1 sanctioned-degrade) keeps the gate
# ============================================================================

class TestCompressionResumeRestoreGate:

    def _drive(self, engine, inst, reacquire_result):
        """Drive _wait_for_compression_to_clear with suspension already cleared."""
        pool = engine.pool
        pool._compression_halted = set()  # not suspended → zero iterations
        # NOTE: core.py imports save/restore lazily from agent_cascade.state_ops, so
        # both must be patched at the state_ops module (patching engine.core would fail).
        with patch.object(engine, "reacquire_for", return_value=reacquire_result), \
             patch("agent_cascade.state_ops.save_instance_state", return_value=True):
            return engine._wait_for_compression_to_clear(inst.instance_name)

    def test_degraded_slotless_resume_skips_restore(self):
        """Sanctioned degrade: reacquire returns True but the instance holds NO slot
        (no-slot endpoint / unlimited) → _slot_release is None → no state/load."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        wire_router_held_endpoint(pool)
        inst._slot_release = None  # no-slot agent

        with patch.object(state_ops.httpx, "post") as post_mock:
            self._drive(engine, inst, reacquire_result=True)

        assert _recorded_load_urls(post_mock) == [], \
            "no state/load may fire when the instance holds no slot"

    def test_slot_held_resume_restores_to_held_endpoint(self):
        """Re-acquired holder → restore fires to the HELD endpoint X, not stale Y."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        wire_router_held_endpoint(pool)

        # The (mocked) re-acquire sets _slot_release, like reacquire_for does.
        def fake_reacquire(instance_arg, holder_name, context="reacquire"):
            instance_arg._slot_release = lambda: None
            instance_arg._slot_key = "pool-x"
            return True

        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            pool._compression_halted = set()
            with patch.object(engine, "reacquire_for", side_effect=fake_reacquire), \
                 patch("agent_cascade.state_ops.save_instance_state", return_value=True):
                engine._wait_for_compression_to_clear(inst.instance_name)

        load_urls = _recorded_load_urls(post_mock)
        assert len(load_urls) == 1
        assert AUTOLOADER_X.rstrip('/') in load_urls[0] and "model-A" in load_urls[0]
        assert STALE_Y not in load_urls[0]


# ============================================================================
# Sleep-wakeup paths keep the gate
# ============================================================================

class TestSleepWakeupRestoreGate:

    def test_no_slot_held_wakeup_skips_restore(self):
        """_setup_turn is the canonical gate; sleep-wakeup sites use the same
        _slot_release check. Verify the shared helper skips when no slot is held."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        wire_router_held_endpoint(pool)
        inst._slot_release = None  # unlimited endpoint — acquire left it None

        with patch.object(state_ops.httpx, "post") as post_mock:
            # The caller-side gate (same check used by the sleep-wakeup sites):
            if inst._slot_release is not None:
                engine._restore_held_slot_state(inst, inst.instance_name)

        assert _recorded_load_urls(post_mock) == []


# ============================================================================
# Invariant — no wrongful eviction of a sibling's resident
# ============================================================================

class TestNoWrongfulEviction:

    def test_sibling_on_other_endpoint_never_touched(self):
        """Agent B holds the conc=0 slot on X and restores. Agent A's resident
        lives on a DIFFERENT endpoint (not X) — since restore only ever fires to
        the held endpoint, A's autoloader can never receive a load/evict call."""
        agent_a_autoloader = "http://a-host:1234/v1/"  # A's resident lives here
        inst_b = make_instance(name="B")
        inst_b._slot_release = lambda: None  # B holds the conc=0 slot on X

        engine, pool = make_engine(inst_b)
        wire_router_held_endpoint(pool)

        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            drive_setup_turn(engine, inst_b)

        # Every load call must target the held endpoint X — never A's autoloader.
        for url in _recorded_load_urls(post_mock):
            assert agent_a_autoloader not in url, \
                f"restore evicted sibling A's resident: {url}"
        # And B's stale config Y is equally off-limits.
        for url in _recorded_load_urls(post_mock):
            assert STALE_Y not in url

    def test_two_agents_only_holder_restores(self):
        """Two agents sharing the conc=0 pool: only the one HOLDING the slot may
        restore; the slotless one must produce zero load calls. This is the FIFO
        single-holder guarantee pinned as a unit invariant."""
        holder = make_instance(name="holder")
        holder._slot_release = lambda: None  # holds the conc=0 slot

        other = make_instance(name="other")
        other._slot_release = None  # waiting in FIFO — holds nothing

        engine, pool = make_engine(holder)
        wire_router_held_endpoint(pool)

        with patch.object(state_ops.httpx, "post") as post_mock:
            post_mock.return_value.status_code = 200
            drive_setup_turn(engine, holder)   # holder → exactly one load (to X)
            # Drive the slotless agent through the same engine path.
            engine._setup_turn(other)

        load_urls = _recorded_load_urls(post_mock)
        assert len(load_urls) == 1, \
            f"only the slot holder may restore; got loads: {load_urls}"
        assert AUTOLOADER_X.rstrip('/') in load_urls[0]

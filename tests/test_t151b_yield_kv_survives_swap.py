"""t151b Part 2 — a yielded caller's KV survives an interleaved model swap.

The race this closes (plan §6): agent A yields its slot to run a system agent
(Security / Compressor). While A is slotless, another agent's forced compression
loads a DIFFERENT model onto the shared conc=0 autoloader and evicts A's resident
KV. Without Part 2, A re-acquires with ``_state_label is None`` and its context is
silently destroyed.

Part 2 makes the save happen BEFORE the release (eviction-safe) and the restore
happen ONTO THE HELD ENDPOINT after re-acquisition — never the stale
``_last_endpoint_config``. These tests pin that down against a mock autoloader that
FAITHFULLY reproduces eviction: loading model-B evicts the resident KV of every
other model on the shared endpoint, so a test that never evicted would prove nothing.

Run serially: ``pytest -n 0 --timeout=120`` (threading/timing-sensitive).
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.state_ops as state_ops
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import Message

# The shared conc=0 autoloader (the endpoint the caller holds a slot on).
AUTOLOADER_X = 'http://localhost:1234/v1/'   # is_autoloader_endpoint() → True (' :1234/')
STALE_Y = 'http://stale-host:9123/v1/'       # stale _last_endpoint_config (NOT held)


# ── A faithful mock of the llama-autoloader state endpoint ─────────────────────
class AutoloaderMock:
    """Simulates the autoloader's per-model KV residency + LRU eviction.

    ``resident`` maps model -> label currently resident in VRAM. Loading a model
    (state/load) EVICTS every other model's resident state — this is exactly what a
    shared conc=0 autoloader does when an interleaved agent's model swap happens. A
    mock that never evicts would make the "survives the swap" assertion vacuous.

    ``save`` snapshots the caller's KV to disk (keyed by label) and sets its resident
    state; it returns a fresh resident dict so a later eviction cannot destroy what was
    saved. ``load`` restores from that snapshot onto the requested model, evicting all
    other models in the process.
    """

    def __init__(self):
        self.resident = {}          # model -> label currently loaded in VRAM
        self.saved = {}             # label -> {'model': ..., 'kv': ...}  (durable on disk)
        self.save_calls = []        # list of (model, label) — order matters for assertions
        self.load_calls = []        # list of (model, label)

    def save(self, api_base, model, label):
        """Snapshot the caller's KV to durable storage. Returns True on success."""
        # The resident state is whatever is currently loaded for this model (the live KV).
        current_kv = self.resident.get(model, f'live-kv-{model}')
        self.saved[label] = {'model': model, 'kv': current_kv}
        self.save_calls.append((model, label))
        return True

    def load(self, api_base, model, label):
        """Restore a saved snapshot onto ``model``, EVICTING every other model.

        Eviction happens unconditionally — loading ANY model on a shared conc=0 autoloader
        unloads all others, whether or not the requested state file exists (a real autoloader
        loads the model into VRAM even if its /state/load 404s). Returns True only if the
        snapshot existed to restore.
        """
        self.load_calls.append((model, label))
        entry = self.saved.get(label)
        # Eviction: loading ``model`` unloads all other resident models (the core of the race).
        self.resident = {model: (entry['kv'] if entry else label)}
        return entry is not None


def _http_side_effect(autoloader):
    """Build an httpx.post side_effect that routes /state/save and /state/load to the mock."""

    def _post(url, json=None, timeout=None):
        if '/state/save' in url:
            model = url.split('/models/')[1].split('/')[0]
            label = (json or {}).get('label')
            ok = autoloader.save(AUTOLOADER_X, model, label)
            code = 200 if ok else 404
        elif '/state/load' in url:
            model = url.split('/models/')[1].split('/')[0]
            label = (json or {}).get('label')
            ok = autoloader.load(AUTOLOADER_X, model, label)
            code = 200 if ok else 404
        else:
            code = 200  # /state cleanup GET etc. — irrelevant to these tests
        resp = MagicMock()
        resp.status_code = code
        return resp

    return _post


# ── Instance / engine / router fixtures ────────────────────────────────────────

def make_instance(name='A', label=None, stale_cfg=True):
    """Real AgentInstance. ``stale_cfg`` points _last_endpoint_config at STALE_Y (NOT held)."""
    inst = AgentInstance(
        instance_name=name, agent_class='coder', conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    with inst._state_lock:
        inst._state_label = label
        if stale_cfg:
            inst._last_endpoint_config = {
                'api_base': STALE_Y, 'model': 'stale-model', 'state_save_enabled': True,
            }
    return inst


def make_engine(inst):
    pool = MagicMock()
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    pool.get_instance.return_value = inst
    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine, pool


def wire_held_endpoint(pool):
    """Router resolves the HELD endpoint X / model-A for this instance."""
    pool.api_router.get_effective_slot_info.return_value = {
        'slot_key': 'pool-x', 'is_sequential': True, 'concurrency_limit': 0,
        'api_base': AUTOLOADER_X, 'needs_slot': True,
    }
    pool.api_router.get_endpoint_chain.return_value = [
        {'api_base': AUTOLOADER_X, 'model': 'model-A', 'state_save_enabled': True},
    ]


def _load_urls(post_mock):
    """URLs of every /state/load POST the mock recorded (call args or kwargs)."""
    urls = []
    for call in post_mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get('url', '')
        if isinstance(url, str) and '/state/load' in url:
            urls.append(url)
    return urls


def _save_urls(post_mock):
    """URLs of every /state/save POST the mock recorded."""
    urls = []
    for call in post_mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get('url', '')
        if isinstance(url, str) and '/state/save' in url:
            urls.append(url)
    return urls


# ============================================================================
# T3 — KV survives an interleaved model swap (Security + compression paths)
# ============================================================================

class TestYieldKvSurvivesSwap:

    def test_security_yield_kv_survives_interleaved_model_swap(self):
        """A saves before releasing; B's forced-compression model swap evicts A's
        resident KV; A re-acquires and its KV is restored onto the HELD endpoint X."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label=None)
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)

        # A holds the conc=0 slot on X and has a live resident KV.
        inst._slot_release = lambda: None
        autoloader.resident['model-A'] = 'A'  # A's model is resident (live KV in VRAM)

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            # 1. Save BEFORE release (the eviction-safe moment — A still owns the endpoint).
            saved = engine.save_before_slot_yield(inst, 'A', reason='before_security_check')
            assert saved is True, 'save must set a pending label'
            with inst._state_lock:
                assert inst._state_label == 'A'

            # 2. A releases its slot (now slotless).
            inst._slot_release = None

            # 3. INTERLEAVE: B's forced compression loads model-B onto the shared endpoint,
            #    evicting A's resident KV. This is exactly what falsifies the old "stays
            #    resident in RAM" premise.
            autoloader.load(AUTOLOADER_X, 'model-B', 'B')
            assert 'model-A' not in autoloader.resident, \
                f"mock must evict A's resident KV on B's model load: {autoloader.resident}"

            # 4. A re-acquires (reacquire_for mocked to hand back the slot) and restores.
            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                ok = engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')
            loads = _load_urls(post_mock)

        assert ok is True
        # The restore targeted the HELD endpoint X / model-A — NOT the stale _last_endpoint_config.
        assert len(loads) == 1, f'exactly one state/load expected: {loads}'
        # The restore resolved the HELD endpoint (X / model-A), not the stale config.
        held_cfg = engine._resolve_held_endpoint(inst)
        assert held_cfg is not None and held_cfg.get('model') == 'model-A', \
            f"restore must resolve the held endpoint: {held_cfg}"
        assert AUTOLOADER_X.rstrip('/') in loads[0] and 'model-A' in loads[0], \
            f"restore must target the held endpoint X/model-A: {loads[0]}"
        assert STALE_Y not in loads[0] and 'stale-model' not in loads[0], \
            f"restore must NOT target stale config Y: {loads[0]}"
        # The label was consumed (cleared) by the restore.
        with inst._state_lock:
            assert inst._state_label is None, 'label must be consumed on successful restore'

    def test_compression_yield_kv_survives_interleaved_model_swap(self):
        """Same invariant through the compression caller path (tolerate_failure=True)."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label=None)
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)

        inst._slot_release = lambda: None
        autoloader.resident['model-A'] = 'A'

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            assert engine.save_before_slot_yield(inst, 'A', reason='before_compression') is True
            inst._slot_release = None
            autoloader.load(AUTOLOADER_X, 'model-B', 'B')  # interleaved swap evicts A

            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                ok = engine.reacquire_after_slot_yield(inst, 'A', 'after_compression',
                                                       tolerate_failure=True)
            loads = _load_urls(post_mock)

        assert ok is True
        assert len(loads) == 1 and AUTOLOADER_X.rstrip('/') in loads[0] and 'model-A' in loads[0]
        with inst._state_lock:
            assert inst._state_label is None


# ============================================================================
# T4 — eviction safety: restore targets the HELD endpoint; resolution failure skips
# ============================================================================

class TestEvictionSafety:

    def test_restore_targets_held_endpoint_not_stale(self):
        """When a label is pending and the slot is held, restore fires to the HELD
        endpoint X/model-A — never the stale _last_endpoint_config Y."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label='A')  # label already pending (a save happened)
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)
        inst._slot_release = lambda: None

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')
            loads = _load_urls(post_mock)

        assert len(loads) == 1
        assert AUTOLOADER_X.rstrip('/') in loads[0] and 'model-A' in loads[0]
        assert STALE_Y not in loads[0] and 'stale-model' not in loads[0]

    def test_resolution_failure_skips_restore_and_clears_label(self):
        """When _resolve_held_endpoint returns None (resolution failure), NO restore fires
        and the orphaned label is cleared so it cannot re-fire a 3.1 GB load later."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label='A')
        engine, pool = make_engine(inst)
        # Router resolution fails → _resolve_held_endpoint returns None.
        pool.api_router.get_effective_slot_info.side_effect = RuntimeError('boom')
        inst._slot_release = lambda: None

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                ok = engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')
            loads = _load_urls(post_mock)

        assert ok is True  # re-acquired fine; only the restore was skipped
        assert loads == [], f"no state/load may fire on resolution failure: {loads}"
        with inst._state_lock:
            assert inst._state_label is None, 'orphaned label must be cleared on skip'


# ============================================================================
# T5 — no redundant restore when nothing changed (label-gate discipline)
# ============================================================================

class TestNoRedundantRestore:

    def test_no_label_means_no_restore(self):
        """With _state_label None at reacquire, restore_instance_state is NOT called —
        no 3.1 GB reload on a window where nothing evicted."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label=None)  # nothing saved → no pending label
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)
        inst._slot_release = lambda: None

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')
            loads = _load_urls(post_mock)

        assert loads == [], 'no state/load may fire when no save is pending (label gate)'

    def test_label_set_means_exactly_one_restore(self):
        """With a label set, restore fires EXACTLY once (consume-once)."""
        autoloader = AutoloaderMock()
        inst = make_instance(name='A', label='A')
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)

        loads = []
        with patch.object(state_ops.httpx, 'post', side_effect=_http_side_effect(autoloader)) as post_mock:
            def fake_reacquire(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = lambda: None
                instance_arg._slot_key = 'pool-x'
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')
            loads = _load_urls(post_mock)

        assert len(loads) == 1


# ============================================================================
# T7 — no slot / unlimited endpoint: no save, no restore, no raise
# ============================================================================

class TestNoSlotNoUnlimitedEndpoint:

    def test_no_slot_unlimited_endpoint_is_noop(self):
        """save_instance_state self-gates (no autoloader endpoint config) → no save;
        reacquire with _slot_release None → no restore. Neither raises."""
        # No _last_endpoint_config → save_instance_state returns False immediately.
        inst = make_instance(name='A', label=None, stale_cfg=False)
        engine, pool = make_engine(inst)
        wire_held_endpoint(pool)

        with patch.object(state_ops.httpx, 'post') as post_mock:
            # Save path: no endpoint config → self-gates, no HTTP.
            saved = engine.save_before_slot_yield(inst, 'A', reason='before_security_check')
            assert saved is False
            post_mock.assert_not_called()

            # Reacquire path: re-acquired but holds NO slot (unlimited endpoint) → no restore.
            def fake_reacquire_no_slot(instance_arg, holder_name, context='reacquire'):
                instance_arg._slot_release = None  # unlimited endpoint — acquire left it None
                return True

            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire_no_slot):
                ok = engine.reacquire_after_slot_yield(inst, 'A', 'after_security_check')

        assert ok is True
        post_mock.assert_not_called()  # no save AND no restore HTTP at all


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))

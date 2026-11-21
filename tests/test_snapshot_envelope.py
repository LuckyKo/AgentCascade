"""BUG_0323 S2: versioned fleet-snapshot envelope + read-only writer.

Covers the slice contract:
  * flag OFF keeps capture a no-op (byte-identical legacy behavior),
  * envelope shape + schema version + migrate_envelope round-trip,
  * log reference correctness (message_count + offset match the real JSONL),
  * non-streaming instances always carry kv: null,
  * a streaming sub-agent that holds the slot gets a KV block,
  * active_stack + children + blocking-call edges are captured (D-CONTINUE),
  * monotonic clocks are projected to wall-clock in created_at / last_activity.
"""

import hashlib
import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

from agent_cascade import snapshot
from agent_cascade.agent_instance import AgentState
from agent_cascade.snapshot import (
    CURRENT_SCHEMA_VERSION,
    capture_fleet_snapshot,
    load_snapshot_from_disk,
    migrate_envelope,
    monotonic_to_wall,
    write_snapshot_to_disk,
)

# A save-capable endpoint config (state_save_enabled + api_base + model).
CAPABLE_CFG = {'api_base': 'http://127.0.0.1:1234/v1', 'model': 'm-a', 'state_save_enabled': True}


# ── Test doubles ─────────────────────────────────────────────────────────────

class _Logger:
    """Stands in for AgentInstanceLogger — only .log_path is read."""

    def __init__(self, path):
        self.log_path = path


class _PoolDouble:
    """Minimal surface capture_fleet_snapshot needs (mirrors AgentPool containers/locks)."""

    def __init__(self):
        self.instances = {}
        self.children = {}
        self.instance_loggers = {}
        self.message_queues = {}
        self.instance_summaries = {}
        self.terminated_instances = set()
        self._halted_instances = set()
        self._compression_halted = set()
        self.active_stack = []
        self._pool_lock = threading.RLock()
        self._children_lock = threading.RLock()
        self._queue_lock = threading.RLock()


def _instance(name, *, agent_class='coder', parent=None, depth=0,
              display=None, state=AgentState.RUNNING, cfg=None):
    """A lightweight instance double with exactly the fields capture reads.

    Uses SimpleNamespace (not MagicMock) so missing fields return the capture
    defaults instead of auto-creating truthy attributes.
    """
    inst = SimpleNamespace(
        instance_name=name,
        agent_class=agent_class,
        parent_instance=parent,
        _nest_depth=depth,
        display_name=display if display is not None else name,
        state=state,
        is_terminated=False,
        _terminated_reason=None,
        restricted_shell=False,
        created_at=0.0,
        last_activity=0.0,
        system_started_at=None,
        kv_birth_ts='20260101_143022',
        compression_summary=None,
        _force_compress_count=0,
        _force_compress_fail_streak=0,
        _last_force_compress_time=0.0,
        _last_force_compress_fail_at=0.0,
        _system_prompt_frozen=False,
        _system_prompt_initialized=False,
        _last_actual_token_count=0,
        _allocated_max_input_tokens=0,
        _current_turn=0,
        _turn_consumed=False,
        max_turns=None,
        token_budget=0,
        budget_unit='turns',
        _token_warned_50=False,
        _token_warned_90=False,
        _token_final_fired=False,
        _token_last_warn_past=0,
        _loop_rollback_count=0,
        _suppress_loop_detection_next_turn=False,
        _fuzzy_warn_armed=True,
        _fuzzy_warn_last_turn=-10**9,
        _fuzzy_escalation_armed=False,
        _discovered_mcp_tools=[],
        _unloaded_mcp_tools=[],
        _mcp_unload_seq=0,
        _memories_read=set(),
        _skill_hint_counts={},
        _last_memory_hint_turn=-1,
        _last_skill_hint_turn=-1,
        _generate_cfg_override=None,
        _last_endpoint_config=cfg,
        _session_digest='dig-' + name,
        _session_digest_endpoint=(cfg['api_base'], cfg['model']) if cfg else None,
        # Live-stream flag read by the canonical ``instance_llm_in_flight`` predicate
        # (no ``_stream_reader_done`` here, so this is the authority).
        _llm_call_active=False,
        _state_lock=threading.RLock(),
    )
    return inst


def _register(pool, inst, *, on_stack=True):
    pool.instances[inst.instance_name] = inst
    if on_stack:
        pool.active_stack.append((inst.instance_name, inst._nest_depth))


def _write_jsonl(path, meta_instance_name, message_count):
    """Write a real JSONL log: 1 metadata line + message_count message lines.

    Message lines match the logger's load semantics (JSON dicts without
    'metadata'/'event' keys). Returns the file's byte size at write time.
    """
    lines = [json.dumps({'metadata': {'instance_name': meta_instance_name}})]
    for i in range(message_count):
        lines.append(json.dumps({'role': 'user' if i % 2 else 'assistant', 'content': f'msg {i}'}))
    text = '\n'.join(lines) + '\n'
    path.write_text(text, encoding='utf-8')
    return path.stat().st_size


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch):
    """S2 capture is gated OFF by default; every test here enables it."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)


@pytest.fixture
def pool():
    return _PoolDouble()


def _make_fleet(pool, tmp_path, monkeypatch, *, streaming_names=()):
    """Root + streaming child + a capable-but-no-slot instance + a non-capable child.

    Returns (root, worker, parked, incapable). ``resolve_held_endpoint`` is faked to
    return a held endpoint only for ``streaming_names``, and those same instances are
    marked as mid-stream (``_llm_call_active``) — the two conditions the KV-coverage
    predicate requires.
    """
    import agent_cascade.state_ops as state_ops

    root = _instance('Maine_20261115_143022', agent_class='orchestrator', display='Maine',
                      depth=0, cfg=CAPABLE_CFG)
    worker = _instance('coder_worker1', parent='Maine_20261115_143022', depth=1, cfg=CAPABLE_CFG)
    parked = _instance('coder_parked', parent='Maine_20261115_143022', depth=1, cfg=CAPABLE_CFG)
    incapable = _instance('coder_plain', parent='Maine_20261115_143022', depth=1, cfg=None)
    for inst in (root, worker, parked, incapable):
        inst._llm_call_active = inst.instance_name in streaming_names
        _register(pool, inst)

    # Register log paths on the pool (source of truth capture reads).
    pool.instance_loggers = {
        'Maine_20261115_143022': _Logger(str(tmp_path / 'maine.jsonl')),
        'coder_worker1': _Logger(str(tmp_path / 'worker.jsonl')),
        'coder_parked': _Logger(str(tmp_path / 'parked.jsonl')),
        'coder_plain': _Logger(str(tmp_path / 'plain.jsonl')),
    }
    pool.children = {'Maine_20261115_143022': ['coder_worker1', 'coder_parked', 'coder_plain']}

    def fake_held(p, inst):
        if inst.instance_name in streaming_names:
            return {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']}
        return None

    monkeypatch.setattr(state_ops, 'resolve_held_endpoint', fake_held)
    return root, worker, parked, incapable


# ── Gate ─────────────────────────────────────────────────────────────────────

class TestGate:
    def test_capture_is_noop_when_flag_off(self, pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
        assert capture_fleet_snapshot(pool) is None

    def test_empty_pool_warns_but_returns_envelope(self, pool, caplog):
        # An empty pool is a recoverable, logged condition — not a crash.
        caplog.set_level(logging.WARNING)
        env = capture_fleet_snapshot(pool)
        assert env is not None
        assert env.to_dict()['instances'] == {}
        assert 'empty pool' in caplog.text.lower()


# ── Envelope shape + version ─────────────────────────────────────────────────

class TestEnvelopeShape:
    def test_envelope_schema_and_top_level_keys(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_worker1',))
        env = capture_fleet_snapshot(pool, trigger='user')
        raw = env.to_dict()
        assert raw['schema_version'] == CURRENT_SCHEMA_VERSION
        assert raw['trigger'] == 'user'
        assert raw['snapshot_id']
        assert isinstance(raw['created_at'], float)
        assert set(raw['instances']) == set(pool.instances)
        assert 'pool_state' in raw
        assert set(raw['instances']['coder_worker1']) >= {
            'identity', 'clocks', 'log', 'state', 'compression', 'system_prompt',
            'token_counters', 'budget', 'loop_detection', 'mcp', 'memory_hints',
            'llm_override', 'kv',
        }

    def test_migrate_envelope_roundtrip_accepts_current_version(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_worker1',))
        raw = capture_fleet_snapshot(pool).to_dict()
        assert migrate_envelope(raw) is raw  # current schema passes through unchanged

    def test_migrate_envelope_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            migrate_envelope({'schema_version': '0.0.1'})
        with pytest.raises(ValueError):
            migrate_envelope({})  # missing schema_version


# ── Log references (count + offset match the real log) ───────────────────────

class TestLogReference:
    def test_message_count_and_offset_match_actual_log(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        maine_log = tmp_path / 'maine.jsonl'
        worker_log = tmp_path / 'worker.jsonl'
        expected_maine = _write_jsonl(maine_log, 'Maine_20261115_143022', 12)
        expected_worker = _write_jsonl(worker_log, 'coder_worker1', 5)
        env = capture_fleet_snapshot(pool)
        raw = env.to_dict()

        m = raw['instances']['Maine_20261115_143022']['log']
        assert m['path'] == str(maine_log)
        assert m['message_count'] == 12
        assert m['offset'] == expected_maine == os.path.getsize(maine_log)

        w = raw['instances']['coder_worker1']['log']
        assert w['message_count'] == 5
        assert w['offset'] == expected_worker == os.path.getsize(worker_log)

    def test_missing_log_yields_zero_reference_not_crash(self, pool, tmp_path, monkeypatch):
        missing = tmp_path / 'absent.jsonl'
        _make_fleet(pool, tmp_path, monkeypatch)
        pool.instance_loggers['coder_plain'] = _Logger(str(missing))
        env = capture_fleet_snapshot(pool)
        raw = env.to_dict()
        # BUG_0345: the reference now always carries prefix_sha256 ('' for an absent log).
        assert raw['instances']['coder_plain']['log'] == {
            'path': str(missing), 'message_count': 0, 'offset': 0, 'prefix_sha256': '',
        }


# ── KV coverage (decision 1) ─────────────────────────────────────────────────

class TestKvCoverage:
    def test_non_streaming_instances_always_kv_null(self, pool, tmp_path, monkeypatch):
        # No instance holds a slot → every kv is null.
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        env = capture_fleet_snapshot(pool)
        raw = env.to_dict()
        assert all(raw['instances'][n]['kv'] is None for n in raw['instances'])

    def test_streaming_child_gets_kv_block_capable_but_no_slot_is_null(self, pool, tmp_path, monkeypatch):
        saved = {}

        def saver(inst, kv_cfg):
            saved[inst.instance_name] = True
            return {'capable': True, 'saved': True, 'reason': None,
                    'label': inst.instance_name, 'session_digest': kv_cfg['session_digest'],
                    'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
                    'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']}}

        # worker1 streams (capable + slot). parked is capable but holds NO slot → null.
        # plain is non-capable → null regardless.
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_worker1',))
        env = capture_fleet_snapshot(pool, kv_saver=saver)
        raw = env.to_dict()

        assert raw['instances']['coder_worker1']['kv'] is not None
        assert raw['instances']['coder_worker1']['kv']['capable'] is True
        assert raw['instances']['coder_worker1']['kv']['saved'] is True
        assert raw['instances']['coder_worker1']['kv']['label'] == 'coder_worker1'
        assert saved == {'coder_worker1': True}

        assert raw['instances']['coder_parked']['kv'] is None   # capable but no slot
        assert raw['instances']['coder_plain']['kv'] is None    # non-capable
        assert raw['instances']['Maine_20261115_143022']['kv'] is None  # root not streaming here

    def test_is_streaming_predicate_uses_capable_and_slot(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_worker1',))
        worker, parked, incapable = (pool.instances['coder_worker1'],
                                      pool.instances['coder_parked'],
                                      pool.instances['coder_plain'])
        cfg_capable = CAPABLE_CFG
        cfg_none = snapshot._kv_config_snapshot(incapable)['endpoint_cfg']  # {} for a None cfg
        assert snapshot._is_streaming(pool, worker, cfg_capable) is True
        assert snapshot._is_streaming(pool, parked, cfg_capable) is False   # capable, no slot
        assert snapshot._is_streaming(pool, incapable, cfg_none) is False    # non-capable

    def test_idle_instance_with_a_resolvable_endpoint_is_not_streaming(self, pool, tmp_path, monkeypatch):
        """A cached endpoint is not a live stream (BUG_0358).

        ``resolve_held_endpoint`` answers for any instance that has an endpoint
        config, so a sibling that is idle (or a caller blocked on its child) still
        resolves one. Requiring a resolvable endpoint therefore saved the whole
        fleet on every task end: multi-second saves of stale sessions, 400s for an
        evicted model, and a 409 for a digest the engine no longer retained.
        """
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_parked',))
        parked = pool.instances['coder_parked']
        parked._llm_call_active = False                     # idle: no live stream
        assert snapshot._is_streaming(pool, parked, CAPABLE_CFG) is False

        parked._llm_call_active = True                      # same endpoint, now streaming
        assert snapshot._is_streaming(pool, parked, CAPABLE_CFG) is True

    def test_capture_saves_only_streaming_and_just_finished(self, pool, tmp_path, monkeypatch):
        """Task end saves the finishing root + live streams, never the idle fleet.

        The finishing root's stream has already ended (its slot was released), so it
        is selected by ``just_finished``; a sibling that is idle at capture time is
        not saved even though its endpoint resolves.
        """
        saved = {}

        def saver(inst, kv_cfg):
            saved[inst.instance_name] = True
            return {'capable': True, 'saved': True, 'reason': None,
                    'label': inst.instance_name, 'session_digest': kv_cfg['session_digest'],
                    'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
                    'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'],
                                     'model': CAPABLE_CFG['model']}}

        root_name = 'Maine_20261115_143022'
        # parked resolves an endpoint but is idle; worker is genuinely mid-stream.
        _make_fleet(pool, tmp_path, monkeypatch,
                    streaming_names=('coder_worker1', 'coder_parked'))
        pool.instances['coder_parked']._llm_call_active = False

        env = capture_fleet_snapshot(pool, trigger='task_end', kv_saver=saver,
                                     just_finished={root_name}, only_streaming=True)
        raw = env.to_dict()

        assert saved == {'coder_worker1': True, root_name: True}
        assert raw['instances']['coder_parked']['kv'] is None
        assert raw['instances']['coder_plain']['kv'] is None

    def test_explicit_capture_keeps_warm_fleet_coverage(self, pool, tmp_path, monkeypatch):
        """Park / Save now stay best-effort over every warm-capable instance.

        Only the automatic task-end save is streaming-only; an explicit capture still
        tries every instance (a stale session is refused by the engine and recorded as
        ``saved: false``, which the restore side already fails closed on).
        """
        saved = {}

        def saver(inst, kv_cfg):
            saved[inst.instance_name] = True
            return {'capable': True, 'saved': True, 'reason': None,
                    'label': inst.instance_name, 'session_digest': kv_cfg['session_digest'],
                    'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
                    'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'],
                                     'model': CAPABLE_CFG['model']}}

        _make_fleet(pool, tmp_path, monkeypatch,
                    streaming_names=('Maine_20261115_143022', 'coder_worker1', 'coder_parked'))
        capture_fleet_snapshot(pool, trigger='user', kv_saver=saver)

        # Everything with a capable endpoint is attempted; the non-capable child is not.
        assert saved == {'Maine_20261115_143022': True, 'coder_worker1': True,
                         'coder_parked': True}

    def test_save_streaming_kv_calls_save_instance_state_set_label_false(self, monkeypatch):
        import agent_cascade.state_ops as state_ops

        calls = []

        def fake_save(inst, set_label=True):
            calls.append(set_label)
            return True

        monkeypatch.setattr(state_ops, 'save_instance_state', fake_save)
        inst = _instance('coder_worker1', cfg=CAPABLE_CFG)
        block = snapshot._save_streaming_kv(inst, snapshot._kv_config_snapshot(inst))
        assert calls == [False]          # warm-only save: never arms an automatic restore
        assert block['saved'] is True
        assert block['capable'] is True
        assert block['label'] == 'coder_worker1_20260101_143022'

    def test_save_streaming_kv_records_failure_not_exception(self, monkeypatch):
        import agent_cascade.state_ops as state_ops

        def fake_save(inst, set_label=True):
            return False

        monkeypatch.setattr(state_ops, 'save_instance_state', fake_save)
        inst = _instance('coder_worker1', cfg=CAPABLE_CFG)
        block = snapshot._save_streaming_kv(inst, snapshot._kv_config_snapshot(inst))
        assert block['saved'] is False
        assert block['reason'] == 'save_failed'

    def test_kv_config_snapshot_freezes_coherent_triple(self):
        # The recorded triple must reflect one coherent endpoint, deep-copied (not aliased).
        inst = _instance('coder_worker1', cfg=dict(CAPABLE_CFG))
        frozen = snapshot._kv_config_snapshot(inst)
        inst._last_endpoint_config['model'] = 'MUTATED'
        assert frozen['endpoint_cfg']['model'] == CAPABLE_CFG['model']
        assert list(frozen['session_digest_endpoint']) == [CAPABLE_CFG['api_base'], CAPABLE_CFG['model']]

    def test_saved_kv_block_is_deep_copied_not_aliased(self, monkeypatch):
        # The block the snapshot RECORDS must not alias the live instance's config,
        # so a later endpoint change cannot retroactively rewrite a saved envelope.
        import agent_cascade.state_ops as state_ops
        monkeypatch.setattr(state_ops, 'save_instance_state', lambda inst, set_label=True: True)

        inst = _instance('coder_worker1', cfg={'api_base': 'http://x/v1', 'model': 'm1', 'state_save_enabled': True})
        block = snapshot._save_streaming_kv(inst, snapshot._kv_config_snapshot(inst))
        # Mutate the live instance AFTER capture; the recorded block must be unaffected.
        inst._last_endpoint_config['model'] = 'MUTATED'
        inst._session_digest_endpoint = ('mutated', 'pair')
        assert block['endpoint_cfg']['model'] == 'm1'
        assert list(block['session_digest_endpoint']) == ['http://x/v1', 'm1']


class TestDeepCopyDiscipline:
    def test_envelope_is_not_aliased_to_live_state(self, pool, tmp_path, monkeypatch):
        pool.instance_summaries['Maine_20261115_143022'] = 'summary-A'
        pool.children['Maine_20261115_143022'] = ['coder_worker1']
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        env = capture_fleet_snapshot(pool)
        raw = env.to_dict()
        # Mutate live state after capture; the returned envelope must be unaffected.
        pool.children['Maine_20261115_143022'].append('injected_child')
        assert 'injected_child' not in raw['pool_state']['children']['Maine_20261115_143022']



# ── Execution graph (D-CONTINUE) ─────────────────────────────────────────────

class TestExecutionGraph:
    def test_active_stack_children_blocking_edges_captured(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=('coder_worker1',))
        env = capture_fleet_snapshot(pool, kv_saver=lambda p, i: {'capable': True, 'saved': True})
        ps = env.to_dict()['pool_state']

        # Ordered active_stack entries are [name, depth] pairs.
        assert [list(e) for e in ps['active_stack']] == [
            ['Maine_20261115_143022', 0], ['coder_worker1', 1],
            ['coder_parked', 1], ['coder_plain', 1],
        ]
        assert ps['children'] == {
            'Maine_20261115_143022': ['coder_worker1', 'coder_parked', 'coder_plain']}

        # A parent blocked on each child currently on the stack below it.
        blocking = {(e['parent'], e['child']) for e in ps['blocking_calls']}
        assert blocking == {
            ('Maine_20261115_143022', 'coder_worker1'),
            ('Maine_20261115_143022', 'coder_parked'),
            ('Maine_20261115_143022', 'coder_plain'),
        }

    def test_blocking_edges_only_between_stack_members(self, pool):
        # worker present but its parent NOT on the stack → no edge.
        worker = _instance('coder_worker1', parent='Maine', depth=1)
        _register(pool, worker)
        assert snapshot._blocking_edges(pool.instances, pool.active_stack) == []


# ── Monotonic → wall-clock projection ────────────────────────────────────────

class TestClockProjection:
    def test_created_at_and_last_activity_projected_to_wall_clock(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        now_mono = time.monotonic()
        pool.instances['Maine_20261115_143022'].created_at = now_mono - 60.0
        pool.instances['Maine_20261115_143022'].last_activity = now_mono - 5.0
        env = capture_fleet_snapshot(pool)
        clocks = env.to_dict()['instances']['Maine_20261115_143022']['clocks']
        # Projected within a second of "60s ago" / "5s ago" in wall-clock terms.
        assert abs(clocks['created_at'] - (time.time() - 60.0)) < 1.0
        assert abs(clocks['last_activity'] - (time.time() - 5.0)) < 1.0

    def test_monotonic_to_wall_roundtrip(self):
        mono = time.monotonic()
        now_wall, now_mono = time.time(), mono
        wall = monotonic_to_wall(mono, now_wall=now_wall, now_mono=now_mono)
        # A value captured at "now" projects to ~current epoch.
        assert abs(wall - now_wall) < 0.5

    def test_zero_sentinel_preserved(self):
        assert monotonic_to_wall(0.0, now_wall=1000.0, now_mono=500.0) == 0.0


# ── Disk write ───────────────────────────────────────────────────────────────

class TestWriteToDisk:
    def test_get_snapshot_dir_is_snapshots_subdir(self, pool, monkeypatch):
        # The envelope lands under <instance log dir>/snapshots, so it inherits the
        # existing per-instance isolation (pytest doubles use a test_<pid> instance id).
        snap_dir = snapshot.get_snapshot_dir(pool)
        assert snap_dir.name == 'snapshots'
        assert snap_dir.suffix == ''  # a directory, not a file

    def test_write_snapshot_atomic_and_readable(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        env = capture_fleet_snapshot(pool)
        snap_dir = tmp_path / 'snapshots'
        out = write_snapshot_to_disk(env, snap_dir)
        assert out.exists()
        assert out.suffix == '.json'
        # Atomic: no leftover .tmp sibling.
        assert list(snap_dir.glob('*.tmp')) == []
        on_disk = json.loads(out.read_text(encoding='utf-8'))
        assert on_disk['schema_version'] == CURRENT_SCHEMA_VERSION
        assert set(on_disk['instances']) == set(pool.instances)

# ── prefix_sha256 (BUG_0345 S6) ───────────────────────────────────────────────

class TestPrefixSha256:
    """The log reference carries a sha256 of exactly bytes[:offset], and the validator is
    append-tolerant. A legacy envelope without prefix_sha256 still loads and validates
    under the old STRICT rule (size == offset), proving the new code is backward-compatible."""

    _FLEET_LOGS = [
        ('maine', 'Maine_20261115_143022', 5),
        ('worker', 'coder_worker1', 3),
        ('parked', 'coder_parked', 2),
        ('plain', 'coder_plain', 1),
    ]

    def test_envelope_carries_prefix_sha256(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        for fname, name, cnt in self._FLEET_LOGS:
            _write_jsonl(tmp_path / f'{fname}.jsonl', name, cnt)
        log = tmp_path / 'maine.jsonl'
        env = capture_fleet_snapshot(pool)
        ref = env.to_dict()['instances']['Maine_20261115_143022']['log']

        assert ref['offset'] == os.path.getsize(log)
        expected = hashlib.sha256(log.read_bytes()[:ref['offset']]).hexdigest()
        assert ref['prefix_sha256'] == expected
        # The captured reference validates against the untouched file.
        assert snapshot.validate_log_reference(ref) is None

    def test_roundtrip_preserves_prefix_sha256(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        for fname, name, cnt in self._FLEET_LOGS:
            _write_jsonl(tmp_path / f'{fname}.jsonl', name, cnt)
        env = capture_fleet_snapshot(pool)
        out = write_snapshot_to_disk(env, tmp_path / 'snapshots')

        # The on-disk JSON carries the prefix hash for every non-empty log reference.
        on_disk = json.loads(out.read_text(encoding='utf-8'))
        for inst in on_disk['instances'].values():
            if inst['log']['offset']:
                assert inst['log']['prefix_sha256'], 'round-trip dropped prefix_sha256'

        reloaded = load_snapshot_from_disk(out)
        m = reloaded.instances['Maine_20261115_143022'].log
        assert m['prefix_sha256'] == \
            env.to_dict()['instances']['Maine_20261115_143022']['log']['prefix_sha256']
        # Append-tolerant: every log is still unchanged, so the reloaded envelope validates.
        snapshot.validate_envelope_integrity(reloaded)

    def test_legacy_envelope_loads_and_validates_under_strict_rule(self, pool, tmp_path, monkeypatch):
        _make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
        for fname, name, cnt in self._FLEET_LOGS:
            _write_jsonl(tmp_path / f'{fname}.jsonl', name, cnt)
        env = capture_fleet_snapshot(pool)
        raw = env.to_dict()
        # Strip the new field -> a pre-BUG_0345 envelope shape.
        for isnap in raw['instances'].values():
            isnap['log'].pop('prefix_sha256', None)

        path = tmp_path / 'legacy.json'
        path.write_text(json.dumps(raw), encoding='utf-8')
        loaded = load_snapshot_from_disk(path)   # legacy envelope must still load
        assert all('prefix_sha256' not in s.log for s in loaded.instances.values())

        # Unchanged on-disk logs validate under the legacy STRICT rule (size == offset).
        snapshot.validate_envelope_integrity(loaded)

        # Prove the legacy branch is STRICT, not append-tolerant: an append — legal under
        # the new prefix-hash rule — is refused here.
        with open(tmp_path / 'worker.jsonl', 'a', encoding='utf-8') as f:
            f.write('{"role":"user","content":"extra"}\n')
        with pytest.raises(snapshot.SnapshotIntegrityError):
            snapshot.validate_envelope_integrity(loaded)

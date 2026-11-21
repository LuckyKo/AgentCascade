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
from pathlib import Path

import pytest

from agent_cascade.agent_pool import AgentPool
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import SYSTEM, Message
from agent_cascade.pool.session_io import _build_working_set
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

# Committed real-log fixture: a 212-line researcher session carrying a REAL
# compression marker (USER message starting '--- CONTEXT COMPRESSED ...' with a
# <context_summary>...</context_summary> block) plus tail messages after it.
# Resolved relative to this test file so the path is portable across drive
# letters. The fixture's own metadata.current_log_path is an absolute Windows
# path that does not exist in the test env; load_session_from_log does NOT read
# it (it reads the path passed to it) and re-persists the FULL loaded history to
# a NEW log under the pool's log_dir, so the post-swap reattach does not depend
# on that dead path (session_io.py:828).
FIXTURE = Path(__file__).parent / 'fixtures' / 'fullstack_e2e_seed_log.jsonl'
# L2 consolidation-marker fixture: a compact slice of a real log (5534 msgs, 7 L1 +
# 1 L2 marker) carrying a genuine `--- CONTEXT COMPRESSED (L2, ...)` marker. Both
# tiers share the same `--- CONTEXT COMPRESSED` prefix, so the swap-compression
# invariant must hold for L1 and L2 markers alike.
L2_FIXTURE = Path(__file__).parent / 'fixtures' / 'l2_consolidation_seed_log.jsonl'
# Fixtures the swap-compression test runs against: L1 single-tier + L2 consolidation.
SWAP_COMPRESSION_FIXTURES = [FIXTURE, L2_FIXTURE]
# Value of agent_cascade.prompts.dna.COMPRESSION_MARKER, inlined so the marker
# predicate stays readable and self-documenting at the assertion site.
COMPRESSED_PREFIX = '--- CONTEXT COMPRESSED'


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


def _cleaned_dicts(conv):
    """Project ``Message`` objects to the ``{role, content}`` dicts _build_working_set reads.

    Only ``role``/``content`` are projected — the two keys ``_is_compression_marker``
    and the working-set split actually consume. Volatile in-memory fields (``ts``,
    injected session-metadata, self-augmentation skill) are intentionally left out so
    the pre-/post-swap comparison targets conversation content only.
    """
    return [
        {'role': getattr(m, 'role', ''), 'content': getattr(m, 'content', '')}
        for m in conv
    ]


def _marker_contents(conv):
    """Compression-marker message contents (str) in conversation order."""
    return [
        m.content
        for m in conv
        if getattr(m, 'role', None) == 'user'
        and isinstance(getattr(m, 'content', None), str)
        and m.content.startswith(COMPRESSED_PREFIX)
    ]


def _ws_signature(working_set):
    """Order-preserving ``(role, content)`` signature of a _build_working_set result."""
    return [(m.get('role'), m.get('content')) for m in working_set]


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

    @pytest.mark.parametrize(
        'fixture', SWAP_COMPRESSION_FIXTURES,
        ids=['l1_single_tier', 'l2_consolidation'])
    def test_compressed_context_survives_real_park_activate_swap(
            self, pool, tmp_path, monkeypatch, fixture):
        """A COMPRESSED context (markers + summary) survives a real park/activate swap.

        Why this test exists
        --------------------
        The sibling test in this class proves a swapped-in *identity* self-heals and
        is functional. Nothing yet proves the OTHER compression-bearing invariant:
        that a context which has ALREADY been compressed — a `--- CONTEXT COMPRESSED`
        marker plus its `<context_summary>`, and the live `compression_summary`
        string derived from it — survives a REAL ``park_active_stack`` /
        ``activate_stack`` (``restore_fleet_snapshot``) swap byte-for-byte, with the
        post-swap working set still being ``[SYS][U0][marker][tail]`` rather than
        regurgitating the summarized history. That contract spans two independent
        code paths that must agree on the SAME marker: ``snapshot`` capture/restore
        (``compression_summary`` round-trips via snapshot.py:502 -> 946) and the
        rehydration path (``_reattach_instance_log`` rebuilds conversation from the
        materialised log through the SAME ``_build_working_set`` that load uses). A
        drift anywhere in that chain silently re-inflates a compressed context.

        Unlike the swap-primitive tests (PoolDouble) and the identity-runtime test
        (SessionIOMixin mock pool), this drives the REAL ``AgentPool(DUMMY_LLM_CFG)``
        and the REAL swap primitives against a REAL committed log with a REAL
        compression marker — no synthetic marker, no mock pool, no real LLM, no
        sleeps and no wall-clock dependency.

        The test is parametrized over SWAP_COMPRESSION_FIXTURES: an L1 single-tier
        marker log and an L2 consolidation-marker log, so the invariant is proven
        for BOTH marker tiers (they share the `--- CONTEXT COMPRESSED` prefix).

        Non-vacuity (BUG_0364 / BUG_0372 consistency gate)
        -------------------------------------------------
        If the fixture ever lost its marker, step 2's preconditions fail LOUDLY
        (empty marker list, empty summary) rather than letting assertions (a)-(e)
        quietly compare vacuous empty sets — the test stops exercising compression
        and announces it. The swap is re-named (target 'Main' != the fixture's own
        'thinking_stream_trace' identity), so load re-persists the full loaded
        history to a fresh tmp log (session_io.py:828) and reattach rebuilds the
        working set from THAT log — proving the compressed shape is reconstructed,
        not merely carried in memory. The "no re-inflation" promise is enforced by
        the RAW conversation-shape assertion (e): (d) only compares the output of
        _build_working_set, which is idempotent/normalizing and therefore cannot by
        itself tell a compressed working set apart from a regurgitated full history,
        so (e) compares the raw in-memory conversation length and byte sequence.
        """
        # Isolate the pool logger to tmp: the re-named load (target 'Main' != fixture
        # identity) takes the copy/rewrite branch, so without this the full-history log
        # it writes would land in the real workspace session-log corpus (rule 12).
        logs_dir = tmp_path / 'swap_logs'
        logs_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(pool._logger, 'log_dir', logs_dir)
        monkeypatch.setattr(pool._logger, 'workspace_dir', tmp_path)

        # 1. Load the real compressed-log fixture as 'Main'. No real LLM is contacted
        #    (DUMMY_LLM_CFG points at a dead port); the dead metadata.current_log_path
        #    is irrelevant because load reads the path we pass and re-persists history.
        status = pool.load_session_from_log(
            str(fixture), target_instance=INSTANCE, clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f'session load failed: {status}'

        inst = pool.instances[INSTANCE]

        # 2. PRECONDITION (non-vacuity). The reattached conversation must carry >=1 real
        #    compression marker; derive ref_summary the SAME way the swap path does —
        #    _build_working_set returns (working_set, marker_summary) and marker_summary
        #    is the stripped text of the LAST marker's FIRST <context_summary> block.
        assert _marker_contents(inst.conversation), (
            'non-vacuity: loaded conversation carries no "CONTEXT COMPRESSED" marker — '
            'the fixture has lost its compression state')
        _, ref_summary = _build_working_set(_cleaned_dicts(inst.conversation))
        assert ref_summary and ref_summary.strip(), (
            'non-vacuity: the fixture marker has no non-empty <context_summary> block')

        # 3. Simulate a live post-compression state so the swap has a real, explicit
        #    summary to preserve. (load_session leaves compression_summary=None —
        #    _reattach_instance_log never touches it — so this set is REQUIRED, not
        #    redundant; without it (b) would compare None to None vacuously.)
        inst.compression_summary = ref_summary

        # 4. PRE-swap fingerprint (raw conversation shape + markers + working set).
        #    pre_conv_raw is the anti-regurgitation baseline compared at (e); pre_ws is
        #    _build_working_set's projection used for the working-set check (d).
        pre_conv_raw = [(m.role, m.content) for m in inst.conversation]
        pre_markers = _marker_contents(inst.conversation)
        pre_ws = _build_working_set(_cleaned_dicts(inst.conversation))[0]

        # 5. Real park -> activate swap (mirrors the sibling test above).
        reg = _ensure_registry(pool)
        reg.register(STACK)
        reg.set_active(STACK)
        env = park_active_stack(
            pool, stack_id=STACK, grace_seconds=0.0,
            sleeper=lambda *_a, **_k: None, clock=lambda: 0.0)
        assert env is not None and env.stack_id == STACK, 'park must return the source envelope'
        assert pool.instances.get(INSTANCE) is None, 'park must empty the pool'
        activate_stack(pool, env, gate_checked=True)

        live = pool.instances[INSTANCE]
        assert live is not None, 'restored instance must be re-installed'

        # 6. POST-swap consistency of the compressed context.
        # (a) Marker count unchanged and the LAST marker byte-identical: the compressed
        #     region of history is neither re-inflated nor lost, and its summary header
        #     (incl. the inlined <context_summary>) is preserved verbatim.
        post_markers = _marker_contents(live.conversation)
        assert len(post_markers) == len(pre_markers), (
            f'marker count changed across swap: {len(pre_markers)} -> {len(post_markers)}')
        assert post_markers[-1] == pre_markers[-1], (
            'last compression marker is not byte-identical across the swap')
        # (b) The compression_summary survived the park/activate snapshot round-trip.
        assert live.compression_summary == ref_summary, (
            'compression_summary did not survive the park/activate swap')
        # (c) latest_marker_index is WRITE-ONLY across the codebase (no readers): marker
        #     position is always re-derived by scanning the conversation via
        #     _build_working_set, so restore_fleet_snapshot deliberately hardcodes -1
        #     on restore ("not captured; matches load_session default" — snapshot.py:947).
        #     Pin it: a future reader-introduction or value-drift should fail here.
        assert live.latest_marker_index == -1, (
            'latest_marker_index must restore to -1 (write-only by design, snapshot.py:947)')
        # (d) Working-set PROJECTION consistency: the compression-aware projection that
        #     _build_working_set derives post-swap matches the one it derived pre-swap.
        #     NOTE: this check alone is NOT an anti-regurgitation guard — _build_working_set
        #     is idempotent/normalizing (it drops non-marker history between the first user
        #     message and the last marker), so a regurgitated full-history conversation and a
        #     compressed one yield the SAME working set. (e) is the load-bearing shape check.
        post_ws = _build_working_set(_cleaned_dicts(live.conversation))[0]
        assert _ws_signature(post_ws) == _ws_signature(pre_ws), (
            'post-swap working set is not byte-consistent with the pre-swap working set')
        # (e) RAW conversation shape — THE anti-regurgitation guard. The in-memory
        #     conversation itself (NOT routed through the normalizing _build_working_set)
        #     must keep the same length and the same byte-identical [SYS][U0][marker][tail]
        #     sequence after restore. A restore that dumped the summarized history back
        #     would blow up the length (compressed working set << full history) and/or
        #     reorder the tail — both caught here, both invisible to (d).
        #     Scope note: this compares (role, content) pairs — i.e. content/shape, NOT
        #     full Message identity (tool-call/extra metadata are out of scope here).
        assert len(live.conversation) == len(pre_conv_raw), (
            f'in-memory conversation length changed across swap: '
            f'{len(pre_conv_raw)} -> {len(live.conversation)} (silent re-inflation?)')
        post_conv_raw = [(m.role, m.content) for m in live.conversation]
        assert post_conv_raw == pre_conv_raw, (
            'raw post-swap conversation is not byte-identical to pre-swap (compression '
            'shape drifted: marker/tail reordered or summarized history re-inflated)')

"""BUG_0439 — L2 session spine: archive hook, spine manifest, transient ToC.

Covers the seams that can silently degrade a spine without crashing:
  * _archive_subsumed_l2s — writes spine files under <log_dir>/spine/ and appends the
    metadata['l2_spine'] manifest, ONLY for subsumed L2 markers, content round-trips, seq
    increments, and the L2 discriminator never fires on L1 markers.
  * caption plumbing — the in-marker <l2_caption> is the manifest caption (NOT the first-line
    fallback) and survives a JSONL serialize/reload round-trip (resume-spanning).
  * _render_spine_toc — deterministic seq order, cap at _SPINE_TOC_MAX + collapse line, '' when
    absent.
  * slice_history_for_llm(spine_toc=...) — the ToC lands as a transient USER message right after
    [user0] before the first marker; empty/absent → byte-identical.
  * session-restore / UI globs never select a spine file (spine/ subdir is non-recursive to them).
  * edit_message_in_place / trim_tail force a re-slice (_last_config_version = -1).

These are deliberately unit-level: they drive the pure seams directly with fakes rather than the
whole engine, which keeps each invariant provable in isolation and avoids flaky engine coupling.
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import threading
from pathlib import Path

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression import core as _core
from agent_cascade.compression.core import _archive_subsumed_l2s
from agent_cascade.compression.helpers import (
    build_consolidation_marker_message,
    build_marker_message,
    extract_l2_caption_from_marker,
    extract_summary_from_marker,
)
from agent_cascade.llm.schema import ASSISTANT, SYSTEM, USER, Message
from agent_cascade.pool.conversation import ConversationMixin


# ──────────────────────────────────────────────────────────────────────────────
# Fakes: a Pool that only exposes what the ToC/archive seams touch.
# ──────────────────────────────────────────────────────────────────────────────


class _FakeLogger:
    def __init__(self, log_path: str, metadata=None):
        self.log_path = log_path
        self.data = {'metadata': metadata if metadata is not None else {}}


class _FakePool(ConversationMixin):
    """ConversationMixin host providing only get_logger; _msg_field supplied locally."""

    def __init__(self, logger):
        self._logger = logger

    @staticmethod
    def _msg_field(msg, field, default=''):
        return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)

    def get_logger(self, name, agent_class):
        return self._logger


class _FakeInst:
    def __init__(self, agent_class='coder'):
        self.agent_class = agent_class


def _mk_pool(tmp_path, metadata=None):
    log_path = str(tmp_path / 'coder_target.jsonl')
    return _FakePool(_FakeLogger(log_path, metadata))


def _l2_marker(caption='Topic A', summary='L2 summary body\nsecond line'):
    return build_consolidation_marker_message(
        summary, 2, first_ts=1000.0, last_ts=2000.0, caption=caption)


def _l1_marker(summary='L1 body'):
    return build_marker_message(summary, first_ts=1000.0, last_ts=1500.0, n_messages=4)


# ──────────────────────────────────────────────────────────────────────────────
# 1. Archive hook
# ──────────────────────────────────────────────────────────────────────────────


class TestArchiveHook:

    def test_writes_spine_file_and_manifest_on_subsumed_l2(self, tmp_path):
        pool = _mk_pool(tmp_path)
        log_dir = tmp_path
        l2 = _l2_marker(caption='Fix the pool bug', summary='L2 summary body')
        current_history = [
            Message(role=SYSTEM, content='sys'),
            Message(role=USER, content='u0'),
            l2,
            _l1_marker(),
        ]
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), current_history, [2, 3])

        manifest = pool._logger.data['metadata']['l2_spine']
        assert len(manifest) == 1, 'only the L2 marker is archived; the L1 is skipped'
        entry = manifest[0]
        assert entry['seq'] == 1
        assert entry['caption'] == 'Fix the pool bug'
        assert entry['time_range'] == [1000.0, 2000.0]

        # file must be absolute, inside the spine/ subdir, and exist
        spine_file = entry['file']
        assert os.path.isabs(spine_file)
        assert os.path.basename(os.path.dirname(spine_file)) == 'spine'
        assert os.path.normpath(os.path.dirname(spine_file)) == os.path.normpath(str(log_dir / 'spine'))
        assert os.path.isfile(spine_file)

        # content round-trips to the subsumed marker's summary
        rec = json.loads(open(spine_file, encoding='utf-8').read().strip())
        assert rec['content'] == 'L2 summary body'
        assert rec['seq'] == 1
        assert rec['caption'] == 'Fix the pool bug'

    def test_multi_l2_seq_increments(self, tmp_path):
        pool = _mk_pool(tmp_path)
        l2a = _l2_marker(caption='First topic', summary='A')
        l2b = _l2_marker(caption='Second topic', summary='B')
        current_history = [
            Message(role=SYSTEM, content='sys'), l2a, l2b,
        ]
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), current_history, [1, 2])
        manifest = pool._logger.data['metadata']['l2_spine']
        assert [e['seq'] for e in manifest] == [1, 2]
        assert [e['caption'] for e in manifest] == ['First topic', 'Second topic']

    def test_no_l2_no_archive(self, tmp_path):
        pool = _mk_pool(tmp_path)
        current_history = [
            Message(role=SYSTEM, content='sys'), _l1_marker('one'), _l1_marker('two'),
        ]
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), current_history, [1, 2])
        manifest = pool._logger.data['metadata'].setdefault('l2_spine', [])
        assert manifest == []
        assert not (tmp_path / 'spine').exists()

    def test_caption_fallback_to_first_line_when_marker_has_none(self, tmp_path):
        pool = _mk_pool(tmp_path)
        legacy = build_consolidation_marker_message(
            'The legacy caption was absent here\nmore body', 2,
            first_ts=1000.0, last_ts=2000.0, caption=None)
        # sanity: this marker genuinely carries no <l2_caption>
        assert extract_l2_caption_from_marker(legacy) is None
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), [legacy], [0])
        entry = pool._logger.data['metadata']['l2_spine'][0]
        assert entry['caption'] == 'The legacy caption was absent here'

    def test_resume_spanning_caption_survives_round_trip(self, tmp_path):
        """🔴 The LLM caption stored in-marker must survive JSONL serialize/reload and reach the
        manifest as the caption — never the first-line fallback."""
        original = _l2_marker(caption='Spine caption from LLM', summary='Body text here')
        # serialize content to JSONL, reload, rebuild a Message, confirm caption is intact
        line = json.dumps({'role': original.role, 'content': original.content})
        reloaded_content = json.loads(line)['content']
        reloaded = Message(role=USER, content=reloaded_content)
        assert extract_l2_caption_from_marker(reloaded) == 'Spine caption from LLM'

        pool = _mk_pool(tmp_path)
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), [reloaded], [0])
        entry = pool._logger.data['metadata']['l2_spine'][0]
        assert entry['caption'] == 'Spine caption from LLM'

    def test_pointer_resolution(self, tmp_path):
        pool = _mk_pool(tmp_path)
        _archive_subsumed_l2s(pool, 'target', _FakeInst(), [_l2_marker()], [0])
        entry = pool._logger.data['metadata']['l2_spine'][0]
        # absolute pointer resolves to an existing, readable file
        assert Path(entry['file']).is_file()
        assert json.loads(Path(entry['file']).read_text(encoding='utf-8').strip())['content']

    def test_archive_failure_propagates_to_caller_try_except(self, tmp_path):
        """Best-effort: an archive failure must not crash the consolidation caller. Verified at the
        seam — a logger that raises propagates to the caller's try/except, so _consolidate_markers
        stays live. Here we confirm the helper raises cleanly (not silently half-writes)."""
        class _BoomPool(_FakePool):
            def get_logger(self, name, agent_class):
                raise RuntimeError('get_logger down')

        pool = _BoomPool(_FakeLogger('x'))
        with pytest.raises(RuntimeError):
            _archive_subsumed_l2s(pool, 'target', _FakeInst(), [_l2_marker()], [0])


# ──────────────────────────────────────────────────────────────────────────────
# 2. ToC rendering
# ──────────────────────────────────────────────────────────────────────────────


class TestRenderSpineToc:

    def test_empty_when_no_manifest(self):
        pool = _FakePool(_FakeLogger('x.jsonl', metadata={}))
        assert pool._render_spine_toc('target', 'coder') == ''

    def test_renders_one_line_per_entry_deterministic(self):
        spine = [
            {'seq': 2, 'caption': 'Second', 'time_range': [200.0, 300.0], 'file': '/s/b.jsonl'},
            {'seq': 1, 'caption': 'First', 'time_range': [100.0, 150.0], 'file': '/s/a.jsonl'},
        ]
        pool = _FakePool(_FakeLogger('x.jsonl', metadata={'l2_spine': spine}))
        toc = pool._render_spine_toc('target', 'coder')
        lines = toc.splitlines()
        assert 'SESSION SPINE' in lines[0]
        assert '[L2#1]' in lines[1] and '[L2#2]' in lines[2], 'sorted by seq regardless of manifest order'
        assert '"First"' in lines[1] and '/s/a.jsonl' in lines[1]

    def test_caps_at_max_with_collapse_line(self):
        spine = [{'seq': i, 'caption': f'cap{i}', 'time_range': [i, i + 1],
                  'file': f'/s/{i}.jsonl'} for i in range(1, 26)]  # 25 entries
        pool = _FakePool(_FakeLogger('x.jsonl', metadata={'l2_spine': spine}))
        toc = pool._render_spine_toc('target', 'coder')
        assert f'[L2#{pool._SPINE_TOC_MAX}]' in toc
        assert '[L2#5]' not in toc, 'oldest dropped'
        assert '… and 5 more (see the JSONL metadata manifest)' in toc

    def test_renders_human_readable_clock_in_span(self):
        """P4: the ToC renders HH:MM (L1 marker clock), not raw epoch floats."""
        spine = [{'seq': 1, 'caption': 'x', 'time_range': [1732892784.0, 1732893784.0], 'file': '/s/a.jsonl'}]
        pool = _FakePool(_FakeLogger('x.jsonl', metadata={'l2_spine': spine}))
        toc = pool._render_spine_toc('target', 'coder')
        assert re.search(r'\(\d{2}:\d{2} -> \d{2}:\d{2}\)', toc), 'span must render as HH:MM -> HH:MM'
        assert '1732892784' not in toc, 'raw epoch must not leak into the ToC'

    def test_render_memoizes_on_manifest_length_and_invalidates(self):
        """P1: render is memoized on the logger keyed by manifest length, and recomputed once a new
        L2 (length change) is archived."""
        logger = _FakeLogger('x.jsonl', metadata={
            'l2_spine': [{'seq': 1, 'caption': 'a', 'time_range': [10, 20], 'file': '/s/a.jsonl'}]})
        pool = _FakePool(logger)
        first = pool._render_spine_toc('target', 'coder')
        cache = logger.__dict__['_spine_toc_cache']
        assert cache[0] == 1 and cache[1] == first, 'first render must be memoized with length key'
        assert pool._render_spine_toc('target', 'coder') is first, 'unchanged length must return the cache'
        logger.data['metadata']['l2_spine'].append(
            {'seq': 2, 'caption': 'b', 'time_range': [30, 40], 'file': '/s/b.jsonl'})
        second = pool._render_spine_toc('target', 'coder')
        assert second != first and '[L2#2]' in second, 'length change must invalidate and re-render'
        assert logger.__dict__['_spine_toc_cache'][0] == 2


# ──────────────────────────────────────────────────────────────────────────────
# 3. slice_history_for_llm chokepoint
# ──────────────────────────────────────────────────────────────────────────────


def _working_history():
    # culled stacked form: [SYS][U0][marker][tail]
    return [
        Message(role=SYSTEM, content='sys'),
        Message(role=USER, content='u0'),
        build_marker_message('old summary', first_ts=100.0, last_ts=200.0, n_messages=5),
        Message(role=USER, content='newest turn'),
    ]


class TestSliceChokepoint:

    def test_to_c_inserted_after_user0_before_first_marker(self):
        pool = _FakePool(_FakeLogger('x.jsonl'))
        hist = _working_history()
        toc = 'SESSION SPINE\n[L2#1] "topic" (100 -> 200) -> /s/a.jsonl'
        out = pool.slice_history_for_llm(hist, spine_toc=toc)
        # role USER, transient, exactly one such synthetic message
        toc_msgs = [i for i, m in enumerate(out)
                    if m.role == USER and toc.splitlines()[0] in (m.content or '')]
        assert len(toc_msgs) == 1
        idx = toc_msgs[0]
        assert out[0].role == SYSTEM, 'system stays first'
        assert out[1].content == 'u0', 'user0 stays second'
        assert idx == 2, 'ToC sits right after [user0], before the first marker'
        assert 'old summary' in (out[3].content or '')

    def test_absent_to_c_is_byte_identical(self):
        pool = _FakePool(_FakeLogger('x.jsonl'))
        hist = _working_history()
        base = pool.slice_history_for_llm(hist, spine_toc=None)
        empty = pool.slice_history_for_llm(hist, spine_toc='')
        assert [ (m.role, m.content) for m in base ] == [ (m.role, m.content) for m in empty ]
        assert not any('SESSION SPINE' in (m.content or '') for m in base), 'no ToC injected'

    def test_to_c_with_no_user0_inserts_after_system(self):
        pool = _FakePool(_FakeLogger('x.jsonl'))
        hist = [
            Message(role=SYSTEM, content='sys'),
            build_marker_message('old', first_ts=1.0, last_ts=2.0, n_messages=1),
            Message(role=USER, content='tail'),
        ]
        toc = 'SESSION SPINE\n[L2#1] t (1 -> 2) -> /x'
        out = pool.slice_history_for_llm(hist, spine_toc=toc)
        assert out[0].role == SYSTEM
        assert 'SESSION SPINE' in (out[1].content or '')
        assert out[1].role == USER, 'ToC is a user-role synthetic message, not system'


# ──────────────────────────────────────────────────────────────────────────────
# 4. Session-restore / UI glob collision (spine/ subdir requirement)
# ──────────────────────────────────────────────────────────────────────────────


class TestNoGlobCollision:

    def test_restore_glob_never_matches_spine_subdir(self, tmp_path):
        name = 'ACDev'
        log_dir = tmp_path
        # a real session log at top level
        (log_dir / f'orchestrator_{name}_000123.jsonl').write_text('{}\n', encoding='utf-8')
        # a spine file whose basename WOULD match the glob if it were top-level
        spine_dir = log_dir / 'spine'
        spine_dir.mkdir()
        (spine_dir / f'{name}_orchestrator_L2_1_20261128_064644.jsonl').write_text('{}\n', encoding='utf-8')
        # api_server.py:1181 glob is log_dir-local (non-recursive)
        potential = list(log_dir.glob(f'*_{name}_*.jsonl'))
        matches = [p.name for p in potential]
        assert f'orchestrator_{name}_000123.jsonl' in matches
        assert not any('L2_1_20261128' in m for m in matches), 'spine file leaked into restore glob'

    def test_ui_glob_never_matches_spine_subdir(self, tmp_path):
        # ws_handlers.py:1113 pattern is also log_dir-local
        agent_class, sa_name = 'orchestrator', 'ACDev'
        log_dir = tmp_path
        (log_dir / f'{agent_class}_{sa_name}_000123.jsonl').write_text('{}\n', encoding='utf-8')
        (log_dir / 'spine').mkdir()
        (log_dir / 'spine' / f'{agent_class}_{sa_name}_L2_1_x.jsonl').write_text('{}\n', encoding='utf-8')
        matches = sorted(_glob.glob(str(log_dir / f'{agent_class}_{sa_name}_*.jsonl')))
        assert len(matches) == 1
        assert 'L2_1_x' not in matches[0]


# ──────────────────────────────────────────────────────────────────────────────
# 5. edit_message_in_place / trim_tail force a re-slice
# ──────────────────────────────────────────────────────────────────────────────


def _bare_instance():
    """Minimal AgentInstance for unit-testing the two cache-impact methods without full init."""
    inst = object.__new__(AgentInstance)
    inst._compression_lock = threading.RLock()
    inst._system_prompt_frozen = False
    inst.instance_name = 'target'
    inst.conversation = [
        Message(role=SYSTEM, content='sys', ts=1.0),
        Message(role=USER, content='u0', ts=2.0),
        Message(role=ASSISTANT, content='tail', ts=3.0),
    ]
    inst._cached_messages = list(inst.conversation)
    inst._cached_llm_messages = list(inst.conversation)
    inst._last_token_count_conversation_length = len(inst.conversation)
    inst._last_config_version = 7
    return inst


class TestForcedReslice:

    def test_edit_message_in_place_resets_config_version_and_mutates_conversation(self):
        inst = _bare_instance()
        inst.edit_message_in_place(1, Message(role=USER, content='u0-edited'))
        assert inst._last_config_version == -1, 'must force re-slice so the ToC-bearing LLM view rebuilds'
        # F3: conversation (persistent) mutated, not only the cached view
        assert inst.conversation[1].content == 'u0-edited'

    def test_trim_tail_resets_config_version(self):
        inst = _bare_instance()
        removed = inst.trim_tail(1)
        assert len(removed) == 1
        assert inst.conversation.__len__() == 2, 'conversation trimmed (F3: persistent mutated)'
        assert inst._last_config_version == -1


# ──────────────────────────────────────────────────────────────────────────────
# 6. Bypass paths — ToC must survive the two cache paths that skip a fresh
#    turn_setup rebuild (KV-cache-safety). These drive the REAL call sites
#    (compression_exec sync / turn_setup extend), not just the raw slice.
# ──────────────────────────────────────────────────────────────────────────────

_SPINE_TOC_TEXT = 'SESSION SPINE\n[L2#1] "topic" (100 -> 200) -> /s/a.jsonl'


def _has_toc(msgs):
    return any(_SPINE_TOC_TEXT.splitlines()[0] in (m.content or '') for m in msgs)


class _EnginePoolStub(ConversationMixin):
    """Pool stub for driving engine call sites: exposes only what the slice sites touch.

    ``_render_spine_toc`` is overridden to return a fixed string so the tests assert the call
    site threads the pool's rendered ToC into ``slice_history_for_llm`` — which is exactly the
    bypass-path invariant under test.
    """

    def __init__(self, conv, toc, inst, config_version=5):
        self._conv = conv
        self._toc = toc
        self._inst = inst
        self._config_version = config_version
        self._logger = _FakeLogger('x.jsonl')

    @staticmethod
    def _msg_field(msg, field, default=''):
        return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)

    def get_logger(self, name, agent_class):
        return self._logger

    def get_conversation(self, name):
        return self._conv

    def get_instance(self, name):
        return self._inst

    def _render_spine_toc(self, inst_name, agent_class):
        return self._toc


class TestBypassPathsCarryToc:

    def test_post_compression_sync_carries_toc(self, monkeypatch):
        """(a) compression_exec writes its OWN slice + stamps _last_config_version (not -1), so
        the NEXT turn is a [CACHE_HIT] that returns this slice without running turn_setup's
        rebuild. Assert the ToC is in that slice AND the version is stamped to the live value
        (proving the cache-hit path will hand the ToC-bearing slice to the LLM)."""
        from types import SimpleNamespace
        from agent_cascade.engine.compression_exec import CompressionExecMixin

        conv = _working_history()
        inst = SimpleNamespace(
            agent_class='coder', _cached_token_count=9,
            _cached_messages=None, _cached_llm_messages=None, _last_config_version=0,
        )
        pool = _EnginePoolStub(conv, _SPINE_TOC_TEXT, inst, config_version=7)
        eng = object.__new__(CompressionExecMixin)
        eng.pool = pool
        eng._clear_llm_preprocess_cache = lambda *a, **k: None
        monkeypatch.setattr('agent_cascade.compression.helpers.rebuild_working_set', lambda *a, **k: None)
        monkeypatch.setattr('agent_cascade.engine.compression_exec._invalidate_token_cache', lambda *a, **k: None)

        messages, llm_messages = [], []
        CompressionExecMixin._rebuild_working_set(eng, messages, llm_messages, 'target')

        assert _has_toc(llm_messages), 'ToC missing in the post-compression-sync slice'
        assert inst._last_config_version == pool._config_version, (
            'sync must stamp config_version (not -1) so the next turn is a CACHE_HIT returning '
            'this exact ToC-bearing slice — if it were -1 we would merely be relying on the '
            'turn_setup rebuild, which is the very bypass we are testing')

    def test_cache_extend_carries_toc(self):
        """(b) turn_setup [CACHE_EXTEND]: with a valid cache and new messages appended, the extend
        branch re-slices _cached_messages. Assert the ToC lands in the extended LLM slice."""
        from types import SimpleNamespace
        from agent_cascade.engine.turn_setup import TurnSetupMixin

        conv = _working_history()
        # cached is the conversation minus the last message → triggers the extend branch
        cached = list(conv[:-1])
        inst = SimpleNamespace(
            instance_name='target', agent_class='coder', conversation=conv,
            _cached_messages=cached, _cached_llm_messages=list(cached),
            _last_config_version=5, _compression_lock=threading.RLock(),
        )
        pool = _EnginePoolStub(conv, _SPINE_TOC_TEXT, inst, config_version=5)
        ts = object.__new__(TurnSetupMixin)
        ts.pool = pool

        result = TurnSetupMixin._get_or_extend_working_set(ts, inst, 'target')
        assert result is not None, 'cache-extend path should return the working set, not rebuild'
        _, llm_messages, _ = result
        assert _has_toc(llm_messages), 'ToC missing in the [CACHE_EXTEND] slice'

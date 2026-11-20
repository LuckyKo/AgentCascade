"""BUG_0350 — media_gen batch output (multi-prompt + incremental-seed takes in one call).

Covers the plan's test matrix (plans/BUG_0350_media_gen_batch_PLAN.md §5):
pure ``_parse_batch`` errors, seed resolution (incl. wrap), schema validation,
fan-out shape, ONE save/unload/restore across a batch (and flag composition),
all-or-nothing preflight, partial / all-failed runtime batches, the unchanged
single path, DMS-bound scaling, and the __help batch line.

Fixtures mirror ``tests/tools/test_media_gen.py`` (same patch points).
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import jsonschema
import pytest

from agent_cascade.tools.media_gen import MediaGen, _invalidate_media_gen_config
from agent_cascade.tools.media_backends.base import MAX_SEED


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


def _mg_cfg(**over):
    cfg = {
        'comfyui': {
            'url': 'http://127.0.0.1:8188',
            'workflow_dir': r'C:\workflows',
            'default_workflow': r'C:\workflows\flux2_t2i_9b_API.json',
            'timeout': 180,
            'media_types': {'image': ['flux2_t2i_9b_API', 'zimg_turbo_API'],
                            'video': [], 'audio': []},
        },
        'sdcpp': {
            'binary': r'C:\bin\sd-cli.exe',
            'models_dir': r'C:\models',
            'config_dir': r'C:\configs',
            'default_sdcpp_config': 'z_image_turbo',
            'timeout': 900,
            'media_types': {'image': ['anima', 'flux2_klein_9b', 'z_image_turbo']},
        },
        'kobold_tts': {
            'timeout': 200,
            'media_types': {'audio': ['kokoro_default']},
        },
    }
    cfg.update(over)
    return cfg


def _resolved_config():
    return {
        'diffusion_model': r'C:\models\c.safetensors',
        'vae': r'C:\models\v.safetensors', 'steps': 20, 'cfg_scale': 7.0,
        'width': 512, 'height': 512, 'sampler': 'euler_a', 'guidance': None,
    }


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst


@pytest.fixture
def sdcpp_cfg(tmp_path):
    """media_gen config whose sdcpp.binary is a REAL temp file so the is_file()
    guard inside generation passes (mirrors test_media_gen.sdcpp_cfg)."""
    bin_path = tmp_path / 'sd-cli.exe'
    bin_path.write_text('fake binary')
    cfg = _mg_cfg()
    cfg['sdcpp']['binary'] = str(bin_path)
    return cfg


def _run_recorder(order, fail_at=None, data=b'\x89PNG-fake'):
    """subprocess.run stand-in: appends 'run', writes PNG bytes to -o. If
    ``fail_at`` (0-based) matches the Nth run, raises RuntimeError instead."""
    state = {'n': 0}

    def _run(argv, **kwargs):
        idx = state['n']
        state['n'] += 1
        order.append('run')
        if fail_at is not None and idx == fail_at:
            raise RuntimeError('render blew up')
        out = argv[argv.index('-o') + 1]
        with open(out, 'wb') as f:
            f.write(data)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')
    return _run


def _patched_call_stack(tool, cfg, order, run_side_effect=None):
    """Common patch bundle for batch generation tests (VRAM ops recorded).

    Mirrors test_media_gen.TestMediaGenVRAM::test_batch_deferral_restore_effect:
    a pool-backed tool whose save arms ``_state_label`` (the deferred-restore marker)
    and whose restore lands on ``restore_held_slot_state``.
    """
    tool.agent_pool = MagicMock()
    inst = _fake_instance()
    inst._state_label = None

    def _save(i):
        order.append('save')
        i._state_label = 'test-model'
        return True

    def _unload(base):
        order.append('unload'); return True

    return (
        patch.object(tool, '_get_instance', return_value=inst),
        patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg),
        patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config',
              return_value=_resolved_config()),
        patch('agent_cascade.tools.image_gen.subprocess.run',
              side_effect=run_side_effect or _run_recorder(order)),
        patch('agent_cascade.tools.media_gen.save_media_to_media',
              return_value='/tmp/media/out.png'),
        patch('agent_cascade.state_ops.save_instance_state', side_effect=_save),
        patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload),
        patch('agent_cascade.state_ops.restore_held_slot_state',
              side_effect=lambda *a, **k: order.append('restore')),
    )


def _run_batch(tool, cfg, order, params, **kwargs):
    """Run ``tool.call`` under the standard patch bundle; returns (result, order)."""
    patches = _patched_call_stack(tool, cfg, order)
    for p in patches:
        p.start()
    try:
        return tool.call(params, **kwargs)
    finally:
        for p in reversed(patches):
            p.stop()


# ── 1. _parse_batch: pure parsing + error matrix (no I/O) ─────────────────────

class TestParseBatchPure:
    def setup_method(self):
        self.tool = MediaGen()

    def test_no_batch_params_returns_empty_list(self):
        assert self.tool._parse_batch({'prompt': 'x', 'backend': 'sdcpp'}) == []
        assert self.tool._parse_batch({'backend': 'sdcpp'}) == []

    def test_prompt_and_prompts_together_rejected(self):
        with pytest.raises(ValueError, match='not both'):
            self.tool._parse_batch({'prompt': 'x', 'prompts': ['y'], 'backend': 'sdcpp'})

    def test_variations_below_one_rejected(self):
        with pytest.raises(ValueError, match='integer >= 1'):
            self.tool._parse_batch({'prompt': 'x', 'backend': 'sdcpp', 'variations': 0})
        # bool is an int subclass — True must not sneak in as 1.
        with pytest.raises(ValueError, match='integer >= 1'):
            self.tool._parse_batch({'prompt': 'x', 'backend': 'sdcpp', 'variations': True})

    def test_empty_prompts_rejected(self):
        with pytest.raises(ValueError, match='must not be empty'):
            self.tool._parse_batch({'prompts': [], 'backend': 'sdcpp'})

    def test_prompts_must_be_strings_non_empty(self):
        with pytest.raises(ValueError, match='non-empty strings'):
            self.tool._parse_batch({'prompts': ['a', '  '], 'backend': 'sdcpp'})

    def test_text_backend_rejects_batch(self):
        with pytest.raises(ValueError, match='produces text'):
            self.tool._parse_batch({'backend': 'voice_to_text', 'variations': 2,
                                    'reference_file': 'x.wav'})

    def test_product_over_cap_rejected_before_anything_else(self):
        # default cap 8; 3 prompts × 3 variations = 9
        with pytest.raises(ValueError, match='MEDIA_GEN_MAX_BATCH'):
            self.tool._parse_batch({'prompts': ['a', 'b', 'c'], 'variations': 3,
                                    'backend': 'sdcpp'})

    def test_cap_zero_is_uncapped(self):
        with patch('agent_cascade.tools.media_gen.settings.MEDIA_GEN_MAX_BATCH', 0):
            items = self.tool._parse_batch({'prompt': 'x', 'variations': 20, 'backend': 'sdcpp'})
        assert len(items) == 20

    def test_fan_out_order_is_prompts_major_with_take_counters(self):
        items = self.tool._parse_batch(
            {'prompts': ['a', 'b'], 'variations': 2, 'backend': 'sdcpp',
             'overrides': {'seed': 100}})
        assert [it['prompt'] for it in items] == ['a', 'a', 'b', 'b']
        assert [(it['take'], it['takes']) for it in items] == [(1, 2), (2, 2), (1, 2), (2, 2)]
        assert [it['seed'] for it in items] == [100, 101, 102, 103]

    def test_variations_only_fans_out_single_prompt(self):
        items = self.tool._parse_batch({'prompt': 'x', 'variations': 3, 'backend': 'sdcpp'})
        assert [it['prompt'] for it in items] == ['x', 'x', 'x']
        seeds = [it['seed'] for it in items]
        assert all(s is not None for s in seeds)
        assert seeds[1] - seeds[0] == 1 and seeds[2] - seeds[1] == 1

    # ── 2. seed resolution ────────────────────────────────────────────────────

    def test_base_seed_yields_contiguous_run(self):
        items = self.tool._parse_batch(
            {'prompt': 'x', 'backend': 'sdcpp', 'variations': 4, 'overrides': {'seed': 1000}})
        assert [it['seed'] for it in items] == [1000, 1001, 1002, 1003]

    def test_no_base_seed_one_random_draw_then_contiguous(self):
        with patch('agent_cascade.tools.media_gen.random.randint', return_value=12345):
            items = self.tool._parse_batch(
                {'prompt': 'x', 'backend': 'sdcpp', 'variations': 3})
        assert [it['seed'] for it in items] == [12345, 12346, 12347]

    def test_seeds_wrap_at_max_seed(self):
        items = self.tool._parse_batch(
            {'prompt': 'x', 'backend': 'sdcpp', 'variations': 3,
             'overrides': {'seed': MAX_SEED}})
        assert [it['seed'] for it in items] == [MAX_SEED, 0, 1]

    def test_seedless_backend_gets_no_seeds_but_still_takes(self):
        # kobold_tts manifest: overrides_keys=() → seed injection skipped (not an error).
        items = self.tool._parse_batch(
            {'prompt': 'hello', 'backend': 'kobold_tts', 'variations': 3})
        assert len(items) == 3
        assert all(it['seed'] is None for it in items)
        assert [it['take'] for it in items] == [1, 2, 3]

    def test_variations_omitted_seeds_stay_none(self):
        items = self.tool._parse_batch({'prompts': ['a', 'b'], 'backend': 'sdcpp'})
        assert all(it['seed'] is None for it in items)

    def test_non_int_seed_with_variations_rejected(self):
        with pytest.raises(ValueError, match='must be an integer'):
            self.tool._parse_batch({'prompt': 'x', 'backend': 'sdcpp',
                                    'variations': 2, 'overrides': {'seed': 'abc'}})


# ── 3. schema validation (tool parameters JSON-schema) ────────────────────────

class TestBatchSchema:
    def setup_method(self):
        self.schema = MediaGen().parameters

    def _validate(self, args):
        jsonschema.validate(args, self.schema)  # raises on invalid

    def test_prompts_array_accepted(self):
        self._validate({'backend': 'sdcpp', 'prompts': ['a', 'b']})

    def test_variations_accepted(self):
        self._validate({'backend': 'sdcpp', 'variations': 2})

    def test_variations_zero_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            self._validate({'backend': 'sdcpp', 'variations': 0})

    def test_non_string_prompt_item_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            self._validate({'backend': 'sdcpp', 'prompts': ['a', 1]})

    def test_prompt_and_prompts_are_schema_valid(self):
        # Schema-level: both may appear; only _parse_batch rejects the mix.
        self._validate({'backend': 'sdcpp', 'prompt': 'a', 'prompts': ['b']})


# ── 4–9. end-to-end batch orchestration ───────────────────────────────────────

class TestBatchOrchestration:
    def test_fan_out_shape_and_labels(self, sdcpp_cfg):
        """2 prompts × 2 variations → 4 outputs, labeled prompt+seed, summary first."""
        tool = MediaGen()
        order = []
        result = _run_batch(
            tool, sdcpp_cfg, order,
            {'prompts': ['a red cube', 'a blue sphere'], 'variations': 2,
             'backend': 'sdcpp', 'overrides': {'seed': 1000}})

        assert order == ['save', 'unload', 'run', 'run', 'run', 'run'] + ['restore']
        assert result[0].text.startswith('Batch: 4/4 generated')
        assert 'backend=sdcpp' in result[0].text and 'config=z_image_turbo' in result[0].text
        assert 'seeds=1000-1003' in result[0].text
        # summary + 4 × (typed item, labeled feedback)
        assert len(result) == 9
        labeled = [ci.text for ci in result if ci.text and ci.text.startswith('[')]
        assert len(labeled) == 4
        for ci in result[1:]:
            if ci.text and not ci.text.startswith('['):
                raise AssertionError('unlabeled success feedback leaked')
        # each label echoes prompt + seed; order matches fan-out order
        for text, prompt, seed in zip(labeled,
                                      ['a red cube', 'a red cube', 'a blue sphere', 'a blue sphere'],
                                      [1000, 1001, 1002, 1003]):
            assert f'prompt="{prompt}"' in text and f'seed={seed}' in text

    def test_vram_exactly_one_save_unload_restore(self, sdcpp_cfg):
        """The whole fan-out shares ONE save/unload/restore, in the right order."""
        tool = MediaGen()
        order = []
        _run_batch(tool, sdcpp_cfg, order,
                   {'prompt': 'x', 'variations': 4, 'backend': 'sdcpp'})
        assert order == ['save', 'unload', 'run', 'run', 'run', 'run', 'restore']

    def test_flag_composition_non_first_non_last_call(self, sdcpp_cfg):
        """Batch embedded in a multi-call turn: is_first/is_last False → zero VRAM ops
        for this call; the run loop still executes all items."""
        tool = MediaGen()
        order = []
        _run_batch(tool, sdcpp_cfg, order,
                   {'prompt': 'x', 'variations': 3, 'backend': 'sdcpp'},
                   is_first_vram=False, is_last_vram=False)
        assert order == ['run', 'run', 'run']

    def test_preflight_failure_is_all_or_nothing_zero_vram(self, sdcpp_cfg):
        """Any item preflight-failing aborts the batch: nothing runs, zero VRAM churn."""
        tool = MediaGen()
        order = []
        from agent_cascade.tools.media_backends import sdcpp as sdcpp_mod
        orig_preflight = sdcpp_mod.SdcppBackend.preflight

        def _preflight(self, req):
            order.append('preflight')
            if len(order) == 3:  # third item blows up
                raise ValueError('bad seed model in config')
            return orig_preflight(self, req)

        patches = list(_patched_call_stack(tool, sdcpp_cfg, order))
        patches.append(patch.object(sdcpp_mod.SdcppBackend, 'preflight', new=_preflight))
        for p in patches:
            p.start()
        try:
            result = tool.call({'prompt': 'x', 'variations': 4, 'backend': 'sdcpp'})
        finally:
            for p in reversed(patches):
                p.stop()

        assert result[0].text.startswith('ERROR:')
        assert 'aborted before generation' in result[0].text
        assert '1 of 4' in result[0].text
        assert 'run' not in order           # nothing generated
        assert 'save' not in order           # zero VRAM churn
        assert 'unload' not in order and 'restore' not in order
        # failing item is identified by its label
        assert any('[3/4]' in ci.text for ci in result if ci.text)

    def test_partial_runtime_failure_partial_summary(self, sdcpp_cfg):
        """One render failing → partial batch, first line NOT 'ERROR:', tool call counts
        as succeeded; failed item still labelled."""
        tool = MediaGen()
        order = []
        run = _run_recorder(order, fail_at=2)
        result = _run_batch_with_run(
            tool, sdcpp_cfg, order, run,
            {'prompt': 'x', 'variations': 4, 'backend': 'sdcpp'})

        assert result[0].text.startswith('Batch: 3/4 generated, 1 failed')
        assert not result[0].text.startswith('ERROR:')
        errs = [ci.text for ci in result if ci.text and 'ERROR:' in ci.text]
        assert len(errs) == 1 and '[3/4]' in errs[0] and 'seed=' in errs[0]
        assert order == ['save', 'unload', 'run', 'run', 'run', 'run', 'restore']

    def test_all_failed_runtime_batch_first_line_is_error(self, sdcpp_cfg):
        """Every render failing → first line starts with 'ERROR:' (telemetry/frontend
        detection contract)."""
        tool = MediaGen()
        order = []

        def _always_fail(argv, **kwargs):
            order.append('run')
            raise RuntimeError('gpu wedged')
        patches = list(_patched_call_stack(tool, sdcpp_cfg, order, run_side_effect=_always_fail))
        for p in patches:
            p.start()
        try:
            result = tool.call({'prompts': ['a', 'b'], 'variations': 2, 'backend': 'sdcpp'})
        finally:
            for p in reversed(patches):
                p.stop()

        assert result[0].text.startswith('ERROR: all 4 media_gen batch items failed')
        # restore still happened exactly once (last item owns it even when it fails)
        assert order.count('restore') == 1
        assert order.count('save') == 1

    def test_cap_violation_rejected_before_any_preflight(self, sdcpp_cfg):
        """9 outputs > default cap 8 → error before any preflight/VRAM/GPU work."""
        tool = MediaGen()
        order = []
        patches = list(_patched_call_stack(tool, sdcpp_cfg, order))
        for p in patches:
            p.start()
        try:
            result = tool.call({'prompts': ['a', 'b', 'c'], 'variations': 3, 'backend': 'sdcpp'})
        finally:
            for p in reversed(patches):
                p.stop()

        assert result[0].text.startswith('ERROR:')
        assert 'MEDIA_GEN_MAX_BATCH' in result[0].text
        assert order == []

    def test_prompt_and_prompts_rejected_at_call_level(self, sdcpp_cfg):
        tool = MediaGen()
        order = []
        result = _run_batch(tool, sdcpp_cfg, order,
                            {'prompt': 'x', 'prompts': ['y'], 'backend': 'sdcpp'})
        assert result[0].text.startswith("ERROR: pass either 'prompt' or 'prompts', not both.")
        assert order == []


# helper for the partial-failure test: standard bundle but with a custom run stand-in
def _run_batch_with_run(tool, cfg, order, run, params, **kwargs):
    patches = _patched_call_stack(tool, cfg, order, run_side_effect=run)
    for p in patches:
        p.start()
    try:
        return tool.call(params, **kwargs)
    finally:
        for p in reversed(patches):
            p.stop()


# ── 10. single path must be byte-identical ────────────────────────────────────

class TestSinglePathUnchanged:
    def test_no_batch_params_single_output_shape(self, sdcpp_cfg):
        tool = MediaGen()
        order = []
        result = _run_batch(tool, sdcpp_cfg, order, {'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert order == ['save', 'unload', 'run', 'restore']
        assert len(result) == 2
        assert result[0].image == '/tmp/media/out.png'
        assert result[1].text and not result[1].text.startswith('[')
        assert not result[1].text.startswith('Batch:')
        assert 'Generated image' in result[1].text

    def test_help_sentinel_still_short_circuits(self):
        tool = MediaGen()
        result = tool.call({'prompt': '__help'})
        assert len(result) == 1 and '== generic ==' in result[0].text

    def test_prompts_with_help_string_is_literal_prompt(self, sdcpp_cfg):
        """'__help' inside prompts[] is NOT the sentinel — it renders literally."""
        tool = MediaGen()
        order = []
        result = _run_batch(tool, sdcpp_cfg, order, {'prompts': ['__help'], 'backend': 'sdcpp'})
        assert order == ['save', 'unload', 'run', 'restore']
        assert any('prompt="__help"' in ci.text for ci in result if ci.text)


# ── 11. DMS bound scales with the batch (same parser) ─────────────────────────

class TestDmsBoundScaling:
    def setup_method(self):
        self.tool = MediaGen()

    def _cfg(self):
        return _mg_cfg()

    def _bound(self, args):
        return self.tool.dms_bound(args, None)  # dms_bound(tool_args, instance)

    def test_single_path_bound_unchanged(self):
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            assert self._bound({'backend': 'sdcpp'}) == 900.0

    def test_variations_scale_bound(self):
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            assert self._bound({'backend': 'sdcpp', 'variations': 4}) == 3600.0

    def test_prompts_cross_product_scales_bound(self):
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            assert self._bound({'backend': 'sdcpp', 'prompts': ['a', 'b'], 'variations': 2}) == 3600.0

    def test_over_cap_falls_back_to_single_bound(self):
        # _parse_batch raises (cap) → bound stays single-render, never unbounded.
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            assert self._bound({'backend': 'sdcpp', 'variations': 999}) == 900.0

    def test_json_string_args_still_parse(self):
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            assert self._bound(json.dumps({'backend': 'sdcpp', 'variations': 2})) == 1800.0

    def test_kobold_tts_headroom_still_carries(self):
        # Existing behaviour must survive scaling: 200*2 + headroom
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=self._cfg()):
            with patch('agent_cascade.tools.media_gen.BACKEND_MANIFEST',
                       {'kobold_tts': {'teardown_headroom': 30, 'overrides_keys': ()}}):
                assert self._bound({'backend': 'kobold_tts', 'variations': 2}) == 200 * 2 + 30


# ── 12. __help batch line ─────────────────────────────────────────────────────

class TestHelpBatchLine:
    def test_generic_help_documents_batch(self):
        tool = MediaGen()
        result = tool.call({'prompt': '__help'})
        text = result[0].text
        assert '  batch:' in text
        assert 'variations=N' in text
        assert 'MEDIA_GEN_MAX_BATCH' in text

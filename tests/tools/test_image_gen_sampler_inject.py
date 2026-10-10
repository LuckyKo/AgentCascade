"""Tests for ``_inject_sampler_params`` — the optional, non-destructive cfg/steps
override applied to ComfyUI workflows.

The critical guarantee is the byte-identical no-op: when both ``guidance`` and
``steps`` are None the helper must return immediately and mutate nothing. The
rest of the suite proves the scalar-only guard, the KSampler-family scoping,
the ambiguity rule, and the never-raises contract against real workflow shapes
(API-format, flux2 node-ref cfg, and UI-format graphs).
"""

import copy

from agent_cascade.tools.image_gen import _inject_sampler_params


def _ksteps(steps=6, cfg=1.0, extra=None):
    """An API-format KSampler node with scalar steps/cfg."""
    inputs = {'steps': steps, 'cfg': cfg, 'seed': 1, 'sampler_name': 'euler',
              'scheduler': 'normal', 'denoise': 1.0,
              'model': ['4', 0], 'positive': ['6', 0], 'negative': ['7', 0],
              'latent_image': ['5', 0]}
    if extra:
        inputs.update(extra)
    return {'1': {'class_type': 'KSampler', 'inputs': inputs}}


def _flux2_shape():
    """A flux2-style workflow: CFGGuider (node-ref cfg), SamplerCustomAdvanced,
    KSamplerSelect, Flux2Scheduler — no KSampler/KSamplerAdvanced."""
    return {
        '75:63': {'class_type': 'CFGGuider',
                  'inputs': {'cfg': ['75:79:106', 0], 'model': ['75:86', 0]}},
        '75:64': {'class_type': 'SamplerCustomAdvanced',
                  'inputs': {'noise': ['75:73', 0], 'guider': ['75:63', 0],
                             'sampler': ['75:61', 0], 'sigmas': ['75:62', 0]}},
        '75:61': {'class_type': 'KSamplerSelect',
                  'inputs': {'sampler_name': 'euler'}},
        '75:62': {'class_type': 'Flux2Scheduler',
                  'inputs': {'steps': 20, 'base_model': 'flux'}},
    }


def _ui_format():
    """A yue2-style UI-format graph: "type"/"widgets_values", no class_type/inputs."""
    return {
        '1': {'type': 'KSampler', 'widgets_values': [6, 1.0, 1, 'euler', 'normal', 1.0]},
        '2': {'type': 'CheckpointLoaderSimple', 'widgets_values': ['ckpt.safetensors']},
    }


# ── byte-identical no-op gate ────────────────────────────────────────────────

class TestNoOpGate:
    def test_both_none_is_true_noop(self):
        wf = _ksteps()
        original = copy.deepcopy(wf)
        result, report = _inject_sampler_params(wf, guidance=None, steps=None)
        assert result is wf  # same object, mutated in place
        assert wf == original  # deep-equal: nothing changed
        assert report == []

    def test_both_none_noop_on_flux2_shape(self):
        wf = _flux2_shape()
        original = copy.deepcopy(wf)
        result, report = _inject_sampler_params(wf, guidance=None, steps=None)
        assert wf == original
        assert report == []

    def test_both_none_noop_on_ui_format(self):
        wf = _ui_format()
        original = copy.deepcopy(wf)
        result, report = _inject_sampler_params(wf, guidance=None, steps=None)
        assert wf == original
        assert report == []


# ── single KSampler / KSamplerAdvanced ───────────────────────────────────────

class TestSingleSampler:
    def test_single_ksampler_updates_both(self):
        wf = _ksteps()
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        assert wf['1']['inputs']['cfg'] == 5.0
        assert wf['1']['inputs']['steps'] == 30
        assert any('cfg=5.0' in r for r in report)
        assert any('steps=30' in r for r in report)

    def test_ksampler_advanced_updates(self):
        wf = {'1': {'class_type': 'KSamplerAdvanced',
                    'inputs': {'steps': 20, 'cfg': 5.0, 'add_noise': 'enable'}}}
        _, _ = _inject_sampler_params(wf, guidance=2.5, steps=12)
        assert wf['1']['inputs']['cfg'] == 2.5
        assert wf['1']['inputs']['steps'] == 12

    def test_steps_only_writes_steps(self):
        wf = _ksteps()
        _, report = _inject_sampler_params(wf, guidance=None, steps=25)
        assert wf['1']['inputs']['steps'] == 25
        assert wf['1']['inputs']['cfg'] == 1.0  # untouched
        assert not any('cfg' in r for r in report)

    def test_guidance_only_writes_cfg(self):
        wf = _ksteps()
        _, report = _inject_sampler_params(wf, guidance=3.3, steps=None)
        assert wf['1']['inputs']['cfg'] == 3.3
        assert wf['1']['inputs']['steps'] == 6  # untouched
        assert not any('steps' in r for r in report)

    def test_zero_values_honoured(self):
        wf = _ksteps()
        _, report = _inject_sampler_params(wf, guidance=0, steps=0)
        assert wf['1']['inputs']['cfg'] == 0.0
        assert wf['1']['inputs']['steps'] == 0
        assert report  # 0 is not None, so it is applied


# ── scoping: KSamplerSelect / flux2 / UI-format untouched ────────────────────

class TestScoping:
    def test_ksampler_select_untouched(self):
        wf = {'1': {'class_type': 'KSamplerSelect',
                    'inputs': {'sampler_name': 'euler'}}}
        original = copy.deepcopy(wf)
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        assert wf == original  # no cfg/steps to write
        assert 'no KSampler node' in report[0]

    def test_flux2_shape_noop_no_exception(self):
        wf = _flux2_shape()
        original = copy.deepcopy(wf)
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        assert wf == original  # CFGGuider/SamplerCustomAdvanced untouched
        assert 'no KSampler node' in report[0]

    def test_ui_format_graph_noop_no_exception(self):
        wf = _ui_format()
        original = copy.deepcopy(wf)
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        assert wf == original  # "type"/"widgets_values" is not API-format
        assert 'no KSampler node' in report[0]


# ── ambiguity rule (multiple KSampler nodes) ─────────────────────────────────

class TestAmbiguity:
    def _two_ksamplers(self, steps_a, steps_b):
        wf = _ksteps(steps=steps_a)
        wf['2'] = _ksteps(steps=steps_b)['1']
        return wf

    def test_two_ksamplers_identical_both_updated(self):
        wf = self._two_ksamplers(6, 6)
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        assert wf['1']['inputs']['steps'] == 30
        assert wf['2']['inputs']['steps'] == 30
        assert wf['1']['inputs']['cfg'] == 5.0
        assert wf['2']['inputs']['cfg'] == 5.0

    def test_two_ksamplers_different_steps_skipped(self):
        wf = self._two_ksamplers(6, 20)
        _, report = _inject_sampler_params(wf, guidance=5.0, steps=30)
        # cfg is identical (1.0) so it IS written; steps differ so it is skipped.
        assert wf['1']['inputs']['cfg'] == 5.0
        assert wf['2']['inputs']['cfg'] == 5.0
        assert wf['1']['inputs']['steps'] == 6  # skipped
        assert wf['2']['inputs']['steps'] == 20  # skipped
        assert any('steps' in r and 'ambiguous' in r for r in report)

    def test_two_ksamplers_different_steps_steps_only_skipped(self):
        wf = self._two_ksamplers(6, 20)
        _, report = _inject_sampler_params(wf, guidance=None, steps=30)
        assert wf['1']['inputs']['steps'] == 6
        assert wf['2']['inputs']['steps'] == 20
        assert any('ambiguous' in r for r in report)

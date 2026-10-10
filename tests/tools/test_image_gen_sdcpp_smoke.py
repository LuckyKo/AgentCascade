"""Opt-in REAL smoke test for the stable-diffusion.cpp (sd-cli) backend.

Runs ONLY when ``AGENTCASCADE_SDCPP_SMOKE=1`` AND the configured ``sd-cli`` binary
exists. It executes the real binary against the real ``config/image_gen.json``
proven preset (``sdxl-pony``) at 512x512 / 4 steps (~35 s) and asserts the
end-to-end invariants that the mocked unit suite cannot:

  * VRAM ordering: save -> unload -> (subprocess) -> restore  (regression gate, F5)
  * a real, >1 KB, PIL-openable image is returned
  * the temp output file is unlinked (no leak, F4)
  * the returned image item is UNCAPTIONED (router auto-captions on return)

The VRAM save/unload/restore seams are mocked (call-recording side effects) so the
ordering can be asserted without a live autoloader; the subprocess.run is the REAL
binary. This file is kept separate from the always-run unit suite so the skip
logic cannot contaminate it.

EXCLUDED FROM THE DEFAULT REGRESSION RUN: it loads external model files that are
NOT part of AgentCascade (a 5-7 GB diffusion checkpoint + TE + VAE) and requires a
specific sd-cli binary + GPU. It is marked ``extra_tools`` (excluded by the default
``-m`` filter in pytest.ini) AND gated on ``AGENTCASCADE_SDCPP_SMOKE=1``, so it never
runs as a side-effect of ``pytest``. Run it explicitly:
    AGENTCASCADE_SDCPP_SMOKE=1 pytest -m extra_tools tests/tools/test_image_gen_sdcpp_smoke.py
"""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.image_gen import (
    ImageGen,
    _get_image_gen_config,
    _invalidate_image_gen_config,
)


class TestSdcppSmoke:
    # Excluded from the default regression run (see module docstring): this test
    # loads external model files that are not part of AgentCascade and requires a
    # specific sd-cli binary + GPU. The extra_tools marker drops it from the default
    # -m filter; AGENTCASCADE_SDCPP_SMOKE=1 is the second, explicit opt-in gate.
    pytestmark = pytest.mark.extra_tools

    def test_end_to_end_proven_preset(self):
        # ── Skip guards ──────────────────────────────────────────────────────
        if os.environ.get('AGENTCASCADE_SDCPP_SMOKE') != '1':
            pytest.skip('set AGENTCASCADE_SDCPP_SMOKE=1 to run the sd-cli smoke test')

        _invalidate_image_gen_config()
        config = _get_image_gen_config()
        sdcpp = config.get('sdcpp') or {}
        binary = sdcpp.get('binary', '')
        if not binary or not Path(binary).is_file():
            pytest.skip(f'sd-cli binary not found at {binary!r}')

        default_config = sdcpp.get('default_sdcpp_config', '')
        assert default_config, 'default_sdcpp_config missing from config'

        # ── Fake instance with a qualifying endpoint config ──────────────────
        inst = MagicMock()
        inst._last_endpoint_config = {
            'state_save_enabled': True,
            'api_base': 'http://localhost:1234',
            'model': 'test',
        }

        # ── Call-recording VRAM seams (real subprocess) ──────────────────────
        order = []

        def _save(inst_):
            order.append('save')
            return True

        def _unload(base):
            order.append('unload')
            return True

        def _restore(inst_, held_endpoint_cfg=None):
            order.append('restore')
            return True

        real_run = subprocess.run

        def _run(*a, **k):
            order.append('run')
            return real_run(*a, **k)

        created = {}
        real_mkstemp = tempfile.mkstemp

        def _mkstemp(*a, **k):
            fd, path = real_mkstemp(*a, **k)
            created['path'] = path
            return fd, path

        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=inst), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
             patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
             patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.tempfile.mkstemp', side_effect=_mkstemp):
            result = tool.call({
                'prompt': 'a red cube on white background',
                'sdcpp_config': default_config,
                'width': 512, 'height': 512, 'steps': 4, 'seed': 42,
            })

        # ── VRAM ordering (regression gate, F5) ──────────────────────────────
        assert order == ['save', 'unload', 'run', 'restore'], f'VRAM order was {order}'

        # ── Real, >1 KB, PIL-openable image ──────────────────────────────────
        img_path = result[0].image
        assert img_path and Path(img_path).is_file(), 'image file missing'
        assert Path(img_path).stat().st_size > 1024, 'image is too small to be real'
        from PIL import Image
        with Image.open(img_path) as im:
            im.verify()  # structural integrity check (raises on corruption)

        # ── F4: temp output file unlinked (no leak) ──────────────────────────
        assert 'path' in created, 'mkstemp was not called'
        assert not Path(created['path']).exists(), 'temp output file leaked'

        # ── UNCAPTIONED invariant ────────────────────────────────────────────
        assert not result[0].caption, 'sdcpp image must be returned uncaptioned'

"""Unit tests for the ``log_api_post`` debug-capture merge path.

``log_api_post`` is a top-level General Settings LLM key persisted to
``api_endpoints.json::default_llm_cfg``. The template LLM's ``generate_cfg`` only
carries the nested ``generate_cfg`` block (plus ``max_input_tokens``), so the flag
must be lifted into the per-call merged cfg by ``_build_merged_cfg`` for
``llm/oai.py`` to see it and dump the raw POST body.

Design invariants guarded here:
  * The live value comes from ``api_router.default_llm_cfg`` (the persisted source),
    with ``pool.llm_cfg`` as a fallback for router-less setups.
  * The live value wins over a stale copy already present in the endpoint layer.
  * Backends that do not implement the capture never receive the flag (SDK-based
    backends forward ``**generate_cfg`` verbatim and would reject it).

No LLM or network connections required.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.engine.llm_call import LLMCallMixin


class _FakeLLM:
    """Minimal stand-in for a template LLM object."""

    def __init__(self, generate_cfg=None, support_log_api_post=True):
        self.generate_cfg = generate_cfg or {}
        self.support_log_api_post = support_log_api_post


class _FakeRouter:
    def __init__(self, default_llm_cfg=None):
        self.default_llm_cfg = default_llm_cfg or {}


class _FakePool:
    def __init__(self, default_llm_cfg=None, llm_cfg=None):
        self.api_router = _FakeRouter(default_llm_cfg)
        self.llm_cfg = llm_cfg or {}


class _FakeInstance:
    def __init__(self, pool=None, override=None):
        self._pool = pool
        self.agent_class = 'coder'
        self._generate_cfg_override = override


def _merge(llm=None, pool=None):
    return LLMCallMixin._build_merged_cfg(llm or _FakeLLM(), _FakeInstance(pool))


class TestLogApiPostMerge:
    def test_lifted_from_router_default_llm_cfg(self):
        # The real source of truth: the flag lives in default_llm_cfg, NOT pool.llm_cfg.
        pool = _FakePool(default_llm_cfg={'log_api_post': True})
        assert _merge(pool=pool).get('log_api_post') is True

    def test_pool_llm_cfg_fallback_when_router_absent(self):
        pool = _FakePool(llm_cfg={'log_api_post': True})
        pool.api_router = None
        assert _merge(pool=pool).get('log_api_post') is True

    def test_explicit_false_flows_as_false(self):
        pool = _FakePool(default_llm_cfg={'log_api_post': False})
        assert _merge(pool=pool).get('log_api_post') is False

    def test_absent_flag_not_added(self):
        pool = _FakePool(default_llm_cfg={}, llm_cfg={})
        assert 'log_api_post' not in _merge(pool=pool)

    def test_live_value_overrides_endpoint_layer(self):
        # Tier-4 endpoint cfg is a snapshot copy of default_llm_cfg; the live source wins.
        pool = _FakePool(default_llm_cfg={'log_api_post': True})
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM(), _FakeInstance(pool), endpoint_cfg={'log_api_post': False})
        assert merged.get('log_api_post') is True

    def test_unsupported_backend_never_receives_flag(self):
        pool = _FakePool(default_llm_cfg={'log_api_post': True})
        llm = _FakeLLM(support_log_api_post=False)
        merged = LLMCallMixin._build_merged_cfg(
            llm, _FakeInstance(pool), endpoint_cfg={'log_api_post': True})
        assert 'log_api_post' not in merged

    def test_flag_never_added_to_template_generate_cfg(self):
        pool = _FakePool(default_llm_cfg={'log_api_post': True})
        llm = _FakeLLM({'temperature': 0.7})
        _merge(llm=llm, pool=pool)
        assert 'log_api_post' not in llm.generate_cfg

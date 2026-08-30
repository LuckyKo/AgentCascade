"""Unit tests for per-endpoint reasoning effort.

The feature adds a per-API-endpoint ``reasoning_effort`` pulldown (none/low/medium/
high/xhigh) that controls the ``reasoning_effort`` kwarg sent to the LLM API. The value
lives on the ``APIEndpoint`` dataclass, is emitted by ``to_llm_cfg()`` as Layer 3 of the
config merge, and flows through ``_build_merged_cfg()`` into the OpenAI SDK call.

Design invariants guarded here:
  * "none" / unset / invalid value → param is NOT sent (model uses default behavior).
  * Valid values pass through unchanged (low/medium/high).
  * "xhigh" maps to "high" at the API level (future-proofing for extended levels).
  * The value survives the custom-sampling strip path (it is not a sampling key).
  * ``to_dict()`` / ``from_dict()`` round-trip the field for persistence.

No LLM or network connections required.
"""

import sys
from pathlib import Path

import pytest

# Ensure top-level imports work
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.api_router_pkg.endpoints import APIEndpoint
from agent_cascade.engine.llm_call import LLMCallMixin
from agent_cascade.settings import REASONING_EFFORT_VALUES


# ============================================================================
# Helpers / fakes
# ============================================================================


class _FakeLLM:
    """Minimal stand-in for a template LLM object (only generate_cfg is read)."""

    def __init__(self, generate_cfg=None):
        self.generate_cfg = generate_cfg or {}


class _FakeInstance:
    """Minimal stand-in for an agent instance (agent_class + pool + override)."""

    def __init__(self, pool=None, agent_class="coder", override=None):
        self._pool = pool
        self.agent_class = agent_class
        self._generate_cfg_override = override


def _make_endpoint(reasoning_effort="none", use_custom_sampling=False, **kw):
    ep = APIEndpoint(id="test-ep", name="Test Endpoint",
                     api_base="http://fake-api", model="fake-model")
    ep.reasoning_effort = reasoning_effort
    ep.use_custom_sampling = use_custom_sampling
    for k, v in kw.items():
        setattr(ep, k, v)
    return ep


# ============================================================================
# 1. to_llm_cfg() — endpoint → generate_cfg emission
# ============================================================================


class TestToLlmCfg:
    def test_none_omits_param(self):
        cfg = _make_endpoint("none").to_llm_cfg()
        assert "reasoning_effort" not in cfg

    def test_unset_defaults_to_none_and_omits(self):
        # A freshly constructed endpoint has reasoning_effort == "none".
        ep = APIEndpoint(id="x", name="t", api_base="http://a", model="m")
        assert ep.reasoning_effort == "none"
        assert "reasoning_effort" not in ep.to_llm_cfg()

    def test_empty_string_omits_param(self):
        cfg = _make_endpoint("").to_llm_cfg()
        assert "reasoning_effort" not in cfg

    @pytest.mark.parametrize("value", ["low", "medium", "high"])
    def test_valid_values_pass_through(self, value):
        cfg = _make_endpoint(value).to_llm_cfg()
        assert cfg.get("reasoning_effort") == value

    def test_xhigh_maps_to_high(self):
        cfg = _make_endpoint("xhigh").to_llm_cfg()
        assert cfg.get("reasoning_effort") == "high"

    @pytest.mark.parametrize("value", ["ultra", "MAX", "low ", ""])
    def test_invalid_values_omitted(self, value):
        cfg = _make_endpoint(value).to_llm_cfg()
        assert "reasoning_effort" not in cfg

    def test_independent_of_custom_sampling_off(self):
        # reasoning_effort must be emitted even when custom sampling is disabled.
        cfg = _make_endpoint("high", use_custom_sampling=False).to_llm_cfg()
        assert cfg.get("_use_custom_sampling") is False
        assert cfg.get("reasoning_effort") == "high"

    def test_independent_of_custom_sampling_on(self):
        cfg = _make_endpoint("low", use_custom_sampling=True, temperature=0.2).to_llm_cfg()
        assert cfg.get("_use_custom_sampling") is True
        assert cfg.get("reasoning_effort") == "low"
        assert cfg.get("temperature") == 0.2


# ============================================================================
# 2. _build_merged_cfg() — merge behavior
# ============================================================================


class TestBuildMergedCfg:
    def test_includes_reasoning_effort_from_endpoint(self):
        ep = _make_endpoint("medium")
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM({"temperature": 0.7}), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        assert merged.get("reasoning_effort") == "medium"

    def test_survives_custom_sampling_strip(self):
        # When custom sampling is OFF, SAMPLING_AND_LIMIT_KEYS are stripped from lower
        # layers. reasoning_effort is NOT a sampling key, so it must survive the strip.
        ep = _make_endpoint("high", use_custom_sampling=False)
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM({"temperature": 0.7}), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        assert merged.get("reasoning_effort") == "high"

    def test_xhigh_maps_to_high_in_merge(self):
        ep = _make_endpoint("xhigh")
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM(), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        assert merged.get("reasoning_effort") == "high"

    def test_none_not_included(self):
        ep = _make_endpoint("none")
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM(), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        assert "reasoning_effort" not in merged

    def test_no_endpoint_cfg_means_not_included(self):
        # Direct-call path (no endpoint config layer) → param absent.
        merged = LLMCallMixin._build_merged_cfg(_FakeLLM(), _FakeInstance())
        assert "reasoning_effort" not in merged

    def test_endpoint_value_overrides_template_default(self):
        # Layer 3 (endpoint) wins over a template default that also sets the key.
        ep = _make_endpoint("low")
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM({"reasoning_effort": "high"}), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        assert merged.get("reasoning_effort") == "low"


# ============================================================================
# 3. Persistence — to_dict / from_dict round-trip
# ============================================================================


class TestPersistence:
    def test_to_dict_includes_field(self):
        ep = _make_endpoint("medium")
        d = ep.to_dict()
        assert d.get("reasoning_effort") == "medium"

    def test_from_dict_round_trip(self):
        original = _make_endpoint("xhigh")
        restored = APIEndpoint.from_dict(original.to_dict())
        assert restored.reasoning_effort == "xhigh"

    def test_from_dict_missing_key_uses_default(self):
        d = _make_endpoint("high").to_dict()
        del d["reasoning_effort"]
        restored = APIEndpoint.from_dict(d)
        assert restored.reasoning_effort == "none"

    def test_from_dict_unknown_value_preserved_but_not_emitted(self):
        # A corrupted/legacy value is preserved on the dataclass but to_llm_cfg()
        # drops it (never sent to the API). This keeps persistence lossless while
        # keeping the API call safe.
        d = _make_endpoint("high").to_dict()
        d["reasoning_effort"] = "corrupted-value"
        restored = APIEndpoint.from_dict(d)
        assert restored.reasoning_effort == "corrupted-value"
        assert "reasoning_effort" not in restored.to_llm_cfg()


# ============================================================================
# 4. OAI allowlist — the param must survive the strict ALLOWED_LLM_PARAMS filter
#    (oai.py strips generate_cfg down to this set before the SDK call). Without
#    this, reasoning_effort is silently dropped and the feature does nothing.
# ============================================================================


class TestOaiAllowlist:
    def test_reasoning_effort_in_allowed_params(self):
        from agent_cascade.llm.oai import ALLOWED_LLM_PARAMS
        assert "reasoning_effort" in ALLOWED_LLM_PARAMS

    @pytest.mark.parametrize("value", ["low", "medium", "high"])
    def test_survives_allowlist_filter_end_to_end(self, value):
        """Full flow: endpoint → to_llm_cfg → _build_merged_cfg → allowlist filter."""
        from agent_cascade.llm.oai import ALLOWED_LLM_PARAMS
        ep = _make_endpoint(value)
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM({"temperature": 0.7}), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        filtered = {k: v for k, v in merged.items() if k in ALLOWED_LLM_PARAMS}
        assert filtered.get("reasoning_effort") == value

    def test_xhigh_survives_as_high_end_to_end(self):
        from agent_cascade.llm.oai import ALLOWED_LLM_PARAMS
        ep = _make_endpoint("xhigh")
        merged = LLMCallMixin._build_merged_cfg(
            _FakeLLM(), _FakeInstance(), endpoint_cfg=ep.to_llm_cfg())
        filtered = {k: v for k, v in merged.items() if k in ALLOWED_LLM_PARAMS}
        assert filtered.get("reasoning_effort") == "high"


# ============================================================================
# 5. Constant sanity
# ============================================================================


def test_reasoning_effort_values_constant():
    assert REASONING_EFFORT_VALUES == ("none", "low", "medium", "high", "xhigh")

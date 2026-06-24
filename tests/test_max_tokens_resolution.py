"""Tests for _resolve_max_tokens() resolution priority chain.

Verifies that max_input_tokens is resolved correctly using the unified
priority order defined in api_integration._resolve_max_tokens():
  1. Per-instance override (_generate_cfg_override)
  2. API Router effective limit
  3. Template static config (llm.cfg)
  4. Instance allocated max_input_tokens
  5. Runtime-detected LLM limit (shared generate_cfg)
  6. DEFAULT_MAX_INPUT_TOKENS from settings

All tests are self-contained — no LLM or API server required.
"""

import pytest


# ──────────────────────────────────────────────
# Lightweight stubs instead of MagicMock to avoid
# hasattr() always returning True for any attribute.
# ──────────────────────────────────────────────

class _InstanceStub:
    """Minimal agent instance stub with explicit attributes only."""

    def __init__(self, agent_class="coder", override=None, allocated=0):
        self.agent_class = agent_class
        if override is not None:
            self._generate_cfg_override = override
        # else: no _generate_cfg_override attribute at all
        if allocated > 0:
            self._allocated_max_input_tokens = allocated


class _LLMStub:
    """Minimal LLM stub with configurable cfg and generate_cfg."""

    def __init__(self, static_limit=0, runtime_limit=0):
        # Static config lives in llm.cfg -> generate_cfg or directly in cfg
        self.cfg = {}
        if static_limit:
            self.cfg['generate_cfg'] = {'max_input_tokens': static_limit}

        # Runtime-detected limit lives in llm.generate_cfg (shared mutable dict)
        self.generate_cfg = {}
        if runtime_limit:
            self.generate_cfg['max_input_tokens'] = runtime_limit


class _TemplateStub:
    """Minimal template stub wrapping an LLM."""

    def __init__(self, static_limit=0, runtime_limit=0):
        self.llm = _LLMStub(static_limit=static_limit, runtime_limit=runtime_limit)


class _RouterStub:
    """Minimal API router stub returning a fixed limit."""

    def __init__(self, limit=0):
        self._limit = limit

    def get_effective_max_tokens(self, agent_class):
        return self._limit


class _PoolStub:
    """Minimal AgentPool stub with optional router and template lookup."""

    def __init__(self, static_limit=0, runtime_limit=0, router_limit=0):
        if router_limit > 0:
            self.api_router = _RouterStub(router_limit)
        else:
            self.api_router = None

        # Store template for get_template() to return (or None)
        if static_limit > 0 or runtime_limit > 0:
            self._template = _TemplateStub(static_limit, runtime_limit)
        else:
            self._template = None

        # _resolve_max_tokens checks hasattr(pool, 'templates') before
        # calling get_template(), so we need this attribute present.
        self.templates = {}

    def get_template(self, agent_class):
        return self._template


# ──────────────────────────────────────────────
# Test Cases
# ──────────────────────────────────────────────

class TestResolveMaxTokensRouterReturnsValue:
    """Test that the API Router value is returned when available (Step 2)."""

    def test_router_value_returned(self):
        """Primary path: router returns a valid limit."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub(router_limit=128000)
        inst = _InstanceStub()

        result = _resolve_max_tokens(pool, inst)
        assert result == 128000


class TestResolveMaxTokensTemplateFallback:
    """Test that template static config is used when router returns 0 (Step 3)."""

    def test_template_static_config_used_when_router_returns_zero(self):
        """Router returns 0, template has a static max_input_tokens."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub(static_limit=64000)
        inst = _InstanceStub()

        result = _resolve_max_tokens(pool, inst)
        assert result == 64000


class TestResolveMaxTokensDefaultFallback:
    """Test that the default is used when everything else fails (Step 6)."""

    def test_default_returned_when_no_template(self):
        """Router returns 0 and no template exists."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub()
        inst = _InstanceStub()

        result = _resolve_max_tokens(pool, inst)
        assert isinstance(result, int) and result > 0


class TestResolveMaxTokensOverridePriority:
    """Test that per-instance override short-circuits everything (Step 1)."""

    def test_override_beats_router_value(self):
        """Per-instance override should be returned even when router has a value."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub(router_limit=128000)
        inst = _InstanceStub(override={'max_input_tokens': 50000})

        result = _resolve_max_tokens(pool, inst)
        assert result == 50000


class TestResolveMaxTokensNullInputs:
    """Test graceful handling of None pool and instance."""

    def test_pool_none_falls_through_to_defaults(self):
        """When pool is None, the function should fall through to defaults."""
        from agent_cascade.api_integration import _resolve_max_tokens

        inst = _InstanceStub()

        result = _resolve_max_tokens(None, inst)
        assert isinstance(result, int) and result > 0

    def test_instance_none_uses_orchestrator_class(self):
        """When instance is None, 'orchestrator' should be used as agent_class."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub(router_limit=80000)

        result = _resolve_max_tokens(pool, None)
        assert result == 80000


class TestResolveMaxTokensRouterException:
    """Test that router exceptions are handled gracefully."""

    def test_router_exception_falls_through(self):
        """When the API Router raises an exception, fallback to template/default."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub(static_limit=48000)
        pool.api_router = _RouterStub(80000)
        # Replace with a stub that raises
        pool.api_router.get_effective_max_tokens = lambda cls: (_ for _ in ()).throw(RuntimeError("endpoint down"))  # noqa

        inst = _InstanceStub()

        result = _resolve_max_tokens(pool, inst)
        assert result == 48000


class TestResolveMaxTokensGenerateCfgNone:
    """Test that generate_cfg being None doesn't crash (the `or {}` fix)."""

    def test_generate_cfg_none_does_not_crash(self):
        """When llm.cfg['generate_cfg'] is None, the function should not crash."""
        from agent_cascade.api_integration import _resolve_max_tokens

        pool = _PoolStub()
        # Override: no template but make one with generate_cfg=None
        pool._template = _TemplateStub(static_limit=0)
        pool._template.llm.cfg = {'generate_cfg': None}

        inst = _InstanceStub()

        result = _resolve_max_tokens(pool, inst)
        assert isinstance(result, int) and result > 0
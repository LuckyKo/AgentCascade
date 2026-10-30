"""Freshness test: 'current_model' tracks the top (chain-head) endpoint's live model.

Regression guard for the stale `_get_current_model` fix.
The template's `llm.model` is a creation-time snapshot; the fix makes state building
read the router's chain-head endpoint model fresh at call time, so a runtime
reorder/swap of the top endpoint is reflected on the next call.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.api_integration_pkg.state_builder import build_state_from_pool
from agent_cascade.api_router_pkg.router import APIRouter
from agent_cascade.api_router_pkg.endpoints import APIEndpoint


class _FakeLlm:
    """Minimal LLM double with a static .model attribute."""
    def __init__(self, model: str):
        self.model = model


class _FakeTemplate:
    """Minimal template double with .llm.model."""
    def __init__(self, model: str):
        self.llm = _FakeLlm(model)


class _FakePool:
    """Minimal pool double that supports build_state_from_pool with a live router."""

    def __init__(self):
        self.instances: dict = {}
        self.templates: dict = {}
        self.api_router = None
        self.stopped = False
        self._template = _FakeTemplate('template-static-model')

    def get_template(self, name):
        return self._template

    def get_instance(self, name):
        return self.instances.get(str(name).strip())

    def slice_history_for_llm(self, history):
        return list(history or [])

    @property
    def active_stack(self):
        return []

    def is_instance_halted(self, name):
        return False

    def has_messages(self, name):
        return False

    def get_queue_messages(self, name):
        return []

    def is_paused(self):
        return False

    def _mark_activity(self, name):
        pass


class TestCurrentModelFreshness:
    """'current_model' tracks the top endpoint's live model, not the template's creation-time snapshot."""

    def _mk(self):
        """Build a pool with a live endpoint 'model-A' as chain-head and a template holding 'template-static-model'."""
        pool = _FakePool()
        ep = APIEndpoint(
            name='live',
            api_base='http://127.0.0.1:9001/v1',
            model='model-A',
            enabled=True,
        )
        pool.api_router = APIRouter(default_llm_cfg={'model': 'template-static-model'})
        pool.api_router.endpoints[ep.id] = ep
        pool.api_router.agent_priorities = {'coder': [ep.id]}

        inst = AgentInstance(
            instance_name='Maine', agent_class='coder', conversation=[],
            created_at=0.0, last_activity=0.0, latest_marker_index=-1,
            state=AgentState.IDLE,
        )
        pool.instances['Maine'] = inst
        return pool, inst, ep

    def test_live_chain_head_wins_over_template(self):
        """The top endpoint's live model wins; the old code returned the template's static value.

        This is the RED discriminator: fails on the pre-fix template-snapshot code.
        """
        pool, inst, ep = self._mk()
        state = build_state_from_pool(pool, 'Maine')
        assert state['current_model'] == 'model-A', (
            f"Expected live chain-head model 'model-A', got {state['current_model']!r} — "
            'the fix is not applied (still reading template)'
        )

    def test_reorder_swap_reflected_fresh(self):
        """Mutating the top endpoint's model is reflected on the next call (fresh, not a snapshot).

        Simulates a user reconfiguring the endpoint while the agent is running.
        """
        pool, inst, ep = self._mk()
        assert build_state_from_pool(pool, 'Maine')['current_model'] == 'model-A'

        # User reconfigures the endpoint (model swap).
        ep.model = 'model-B'

        state = build_state_from_pool(pool, 'Maine')
        assert state['current_model'] == 'model-B', (
            f"Expected live model 'model-B' after mutation, got {state['current_model']!r}"
        )
        # Non-vacuity: the template value never moved.
        assert pool._template.llm.model == 'template-static-model'

    def test_no_router_falls_back_to_template(self):
        """A pool with no api_router falls back to the template's static model (keeps _FakePool working)."""
        pool, inst, ep = self._mk()
        pool.api_router = None

        state = build_state_from_pool(pool, 'Maine')
        assert state['current_model'] == 'template-static-model', (
            f"Expected template fallback 'template-static-model', got {state['current_model']!r}"
        )

    def test_empty_chain_falls_back_to_template(self):
        """A router with no endpoints for this agent_class falls back to the template."""
        pool, inst, ep = self._mk()
        # Remove the endpoint and clear priorities → empty chain for 'coder'.
        pool.api_router.endpoints.clear()
        pool.api_router.agent_priorities = {}

        state = build_state_from_pool(pool, 'Maine')
        # get_endpoint_chain falls through to Tier-4 (default_llm_cfg) which has model='template-static-model'
        # OR returns an empty chain → template fallback.
        # Either way, it should NOT be 'Unknown' because the template has a model.
        assert state['current_model'] in ('template-static-model',), (
            f"Expected template fallback, got {state['current_model']!r}"
        )

"""BUG_0404 skill-cache /api/telemetry surfacing tests.

Proves the new read-only `skill_cache` slice reaches the client:
  - the live route returns it with all seven stable keys;
  - the no-pool fallback returns `skill_cache: {}` (shape stability, matches session: {}).

The handler (api_server.create_app -> api_telemetry) is a closure over the `agent_pool`
PARAMETER, not a module attribute, so it is exercised at the HTTP layer via TestClient with a
stub pool passed to create_app. The pool is a MagicMock (startup-safe); only `.telemetry` is a
real TelemetryCollector so the slice getter (already unit-tested in tests/test_telemetry.py)
drives the real projection.
"""

import pytest
from fastapi.testclient import TestClient

from agent_cascade.api_server import create_app
from agent_cascade.telemetry import TelemetryCollector
from unittest.mock import MagicMock


SKILL_CACHE_KEYS = {
    'prefetch_admissions', 'evict_victims', 'evict_by_category',
    'probation_promotions', 'active_set_size', 'shadow_decisions', 'commit_decisions',
}


def _make_app(agent_pool):
    return create_app(agents=[], agent_pool=agent_pool,
                      config={'session_name': 'unit_test_skill_cache'}, auto_security=False)


@pytest.fixture
def live_client(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='api_unit_test')
    collector.record_skill_cache_prefetch(commit=False, admitted=3)
    collector.record_skill_cache_evict(commit=True, victims_by_category={'demand': 2})
    pool = MagicMock()
    pool.telemetry = collector
    with TestClient(_make_app(pool)) as client:
        yield client
    collector.close()


def test_skill_cache_slice_surfaced(live_client):
    resp = live_client.get('/api/telemetry')
    assert resp.status_code == 200
    sc = resp.json().get('skill_cache')
    assert sc is not None, 'skill_cache key must always be present on the live route'
    assert set(sc.keys()) == SKILL_CACHE_KEYS        # exactly the seven stable keys
    assert sc['prefetch_admissions'] == 3
    assert sc['evict_victims'] == 2
    assert sc['shadow_decisions'] == 1
    assert sc['commit_decisions'] == 1


def test_skill_cache_key_is_shape_stable_without_pool():
    with TestClient(_make_app(None)) as client:
        resp = client.get('/api/telemetry')
    assert resp.status_code == 200
    assert resp.json().get('skill_cache') == {}      # present, empty — never a missing key

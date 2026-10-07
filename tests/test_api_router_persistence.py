"""Persistence / serialization tests for APIRouter — BUG_0060.

The bug: the Web UI blur handler re-read every endpoint field from the DOM and turned a
blank number input into ``parseInt('') -> NaN -> 0``, then sent the whole endpoint list to
the server, which persisted it. Values the user never touched (e.g. ``concurrency_limit:
-1``, ``max_input_tokens: 120000``) were silently destroyed. ``config/api_endpoints.json``
is gitignored, so there was no VCS recovery path — hence the atomic ``_save`` + ``.bak``
generation tested here as well.

ISOLATION (required under pytest-xdist): every test builds a FRESH APIRouter inside
``patch.dict(os.environ, {'AGENT_CASCADE_TEST_CONFIG_DIR': str(tmp_path)})``. A router
constructed outside a patched environment resolves its config dir to the project root and
WRITES THE LIVE gitignored config/api_endpoints.json. Never construct one at module scope.
"""

import json
import os
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIEndpoint, APIRouter


def make_router(config_dir):
    """Build an APIRouter whose persistence is confined to *config_dir*.

    Must be called INSIDE the patching context; the router reads the env var in __init__
    and every _save()/_load() resolves against it.
    """
    r = APIRouter(default_llm_cfg={
        'api_base': 'http://default-api',
        'model': 'default-model',
        'max_tokens': 2048,
    })
    r._pool = None
    return r


@pytest.fixture
def cfg_dir(tmp_path):
    """Per-test config dir wired via the env var the router honours."""
    d = tmp_path / 'config'
    with patch.dict(os.environ, {'AGENT_CASCADE_TEST_CONFIG_DIR': str(d)}):
        yield d


@pytest.fixture
def router(cfg_dir):
    """Fresh router bound to the per-test config dir."""
    return make_router(cfg_dir)


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def add_ep(router, **overrides):
    """Add an endpoint via the public API and return it."""
    ep = APIEndpoint(**overrides)
    router.endpoints[ep.id] = ep
    return ep


# ── Layer 2: from_dict presence-based guard ────────────────────────────────────────

def test_from_dict_preserves_value_when_key_absent(router):
    """A key ABSENT from the payload is a transport artefact — inherit the stored value."""
    ep = add_ep(router, name='A', api_base='http://a', model='m',
                max_input_tokens=120000, concurrency_limit=3)
    payload = {'endpoints': [{'id': ep.id, 'name': 'A'}], 'agent_priorities': {}}

    router.from_dict(payload)

    stored = router.endpoints[ep.id]
    assert stored.max_input_tokens == 120000
    # concurrency_limit=3, not the dataclass default of -1 — a non-default value is
    # required here or this assertion cannot discriminate.
    assert stored.concurrency_limit == 3


def test_from_dict_honours_explicit_zero(router):
    """ANTI-OVER-CORRECTION GUARD.

    A key PRESENT with value 0 is a deliberate user edit (0 = "auto") and must be stored
    as 0. This test fails if the Layer 2 guard is ever "helpfully" changed to preserve the
    old value whenever the new one is 0 — which would silently make 0 unsettable.
    """
    ep = add_ep(router, name='A', api_base='http://a', model='m',
                max_input_tokens=120000, concurrency_limit=-1)

    # Full payload, as the real frontend sends: every key present, two of them zero.
    ep_data = ep.to_dict()
    ep_data['max_input_tokens'] = 0
    ep_data['concurrency_limit'] = 0
    router.from_dict({'endpoints': [ep_data], 'agent_priorities': {}})

    stored = router.endpoints[ep.id]
    assert stored.max_input_tokens == 0, 'explicit 0 must be honoured, not restored to 120000'
    assert stored.concurrency_limit == 0, 'explicit 0 must be honoured, not restored to -1'

    # And it must have been persisted, not just held in memory.
    on_disk = read_json(router._config_path)
    saved = next(e for e in on_disk['endpoints'] if e['id'] == ep.id)
    assert saved['max_input_tokens'] == 0
    assert saved['concurrency_limit'] == 0


def test_from_dict_respects_endpoint_deletion(router):
    """The UI deletes an endpoint by filtering it out of the sent list.

    Guards against a future id-merge regression that would resurrect deleted endpoints.
    """
    keep = add_ep(router, name='keep', api_base='http://keep', model='m')
    drop = add_ep(router, name='drop', api_base='http://drop', model='m')

    router.from_dict({
        'endpoints': [keep.to_dict()],
        'agent_priorities': {},
    })

    assert drop.id not in router.endpoints, 'deleted endpoint must not be resurrected'
    assert len(router.endpoints) == 1
    assert keep.id in router.endpoints


def test_from_dict_does_not_resurrect_runtime_state(router):
    """The blacklist / failure-counter / last-successful clears are intentional (see the
    FIX comment in from_dict) and must survive Layer 2's guard."""
    ep = add_ep(router, name='A', api_base='http://a', model='m')
    router._endpoint_blacklist[('http://a', 'm')] = 9999999999
    router._endpoint_failure_times[('http://a', 'm')] = 5
    router._last_successful_endpoint_cfg = {'api_base': 'http://a', 'model': 'm'}

    router.from_dict({'endpoints': [ep.to_dict()], 'agent_priorities': {}})

    assert router._endpoint_blacklist == {}
    assert router._endpoint_failure_times == {}
    assert router._last_successful_endpoint_cfg is None


def test_from_dict_warns_on_nonzero_to_zero(router, caplog):
    """The zero-drop is logged loudly (diagnostic only — it never overrides the payload)."""
    ep = add_ep(router, name='ZeroDrop', api_base='http://a', model='m',
                max_input_tokens=120000)

    ep_data = ep.to_dict()
    ep_data['max_input_tokens'] = 0

    with caplog.at_level('WARNING'):
        router.from_dict({'endpoints': [ep_data], 'agent_priorities': {}})

    warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any('max_input_tokens' in m and '120000' in m and '0' in m for m in warnings), (
        f"expected a zero-drop warning naming the field and both values; got {warnings}")

    # Diagnostic only: the payload still wins.
    assert router.endpoints[ep.id].max_input_tokens == 0


def test_from_dict_does_not_warn_when_value_is_unchanged(router, caplog):
    """Anti-noise guard: an unchanged non-zero value must NOT produce a warning."""
    ep = add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)

    with caplog.at_level('WARNING'):
        router.from_dict({'endpoints': [ep.to_dict()], 'agent_priorities': {}})

    assert not [r.getMessage() for r in caplog.records
                if r.levelname == 'WARNING' and 'max_input_tokens' in r.getMessage()]


def test_from_dict_roundtrip_preserves_all_fields(router):
    """to_dict() -> from_dict() -> to_dict() must be lossless, including -1 values."""
    ep = add_ep(router,
                name='Full', api_base='http://full/v1', api_key='sk-test', model='qwen',
                model_type='openai', enabled=False,
                max_retries=5, concurrency_limit=-1, max_input_tokens=125000,
                rate_limit_rpm=30,
                temperature=0.7, top_p=0.9, top_k=40, min_p=0.05,
                repeat_penalty=1.1, presence_penalty=0.2, frequency_penalty=0.3,
                max_tokens=4096,
                vision_enabled=False, use_custom_sampling=True, state_save_enabled=True,
                reasoning_effort='xhigh')
    before = ep.to_dict()

    router.from_dict({'endpoints': [before], 'agent_priorities': {}})

    assert router.endpoints[ep.id].to_dict() == before


def test_from_dict_tolerates_non_dict_endpoint_entry(router):
    """A junk entry must not abort the whole update (existing behaviour, now with the
    isinstance guard in front of the presence-based inheritance)."""
    good = add_ep(router, name='good', api_base='http://good', model='m')

    router.from_dict({
        'endpoints': ['not-a-dict', good.to_dict()],
        'agent_priorities': {},
    })

    assert good.id in router.endpoints


# ── Layer 3: atomic _save + .bak generation ───────────────────────────────────────

def test_save_is_atomic_no_tmp_left(router):
    """After a save the primary parses and no *.tmp file is left behind."""
    add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)

    router._save()

    assert router._config_path.exists()
    assert read_json(router._config_path)['endpoints'][0]['max_input_tokens'] == 120000
    leftovers = [p.name for p in router._config_dir.glob('*.tmp')]
    assert leftovers == [], f"stray temp files left after save: {leftovers}"


def test_save_creates_single_bak_generation(router):
    """The .bak holds the PREVIOUS generation, not the one just written.

    Guards against a copy-after-write ordering bug, which would leave .bak identical to
    the primary and make the backup worthless.
    """
    add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)
    router._save()

    add_ep(router, name='B', api_base='http://b', model='m', max_input_tokens=32000)
    router._save()

    bak = router._backup_path()
    assert bak.name == 'api_endpoints.json.bak', f"unexpected backup filename: {bak.name}"
    assert bak.exists()

    bak_names = [e['name'] for e in read_json(bak)['endpoints']]
    primary_names = [e['name'] for e in read_json(router._config_path)['endpoints']]
    assert bak_names == ['A'], f"bak should hold generation A, got {bak_names}"
    assert primary_names == ['A', 'B'], f"primary should hold A+B, got {primary_names}"


def test_save_preserves_prior_file_on_failure(router, monkeypatch):
    """If serialization blows up mid-write, the existing file must still be intact.

    The old implementation opened the primary with 'w' (truncating it first), so a
    failure left a zero-length config that _load could not recover from.
    """
    add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)
    router._save()
    before = read_json(router._config_path)

    def boom(*args, **kwargs):
        raise RuntimeError('simulated serialization failure')

    monkeypatch.setattr(json, 'dump', boom)
    add_ep(router, name='B', api_base='http://b', model='m')
    router._save()  # must swallow the error, as it always has

    monkeypatch.undo()
    after = read_json(router._config_path)
    assert after == before, 'primary config was truncated/corrupted by a failed save'
    assert [e['name'] for e in after['endpoints']] == ['A']


def test_load_recovers_from_backup_when_primary_corrupt(router, cfg_dir):
    """A corrupt primary must not leave the router empty when a .bak exists."""
    ep = add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)
    router._save()
    # Second save so the .bak generation holds a real endpoint list.
    add_ep(router, name='B', api_base='http://b', model='m', max_input_tokens=32000)
    router._save()
    assert router._config_path.with_suffix('.json.bak').exists()

    # Simulate a truncated/corrupted primary.
    router._config_path.write_text('{"endpoints": [ {"id": "trunc', encoding='utf-8')

    recovered = make_router(cfg_dir)  # fresh router -> _load() in __init__
    assert len(recovered.endpoints) == 1
    assert next(iter(recovered.endpoints.values())).name == 'A'
    assert recovered.endpoints[ep.id].max_input_tokens == 120000


def test_load_recovers_from_backup_when_primary_empty(router, cfg_dir):
    """A zero-length primary (the signature of the old truncating write) also recovers."""
    add_ep(router, name='A', api_base='http://a', model='m', max_input_tokens=120000)
    router._save()
    add_ep(router, name='B', api_base='http://b', model='m')
    router._save()

    router._config_path.write_text('', encoding='utf-8')

    recovered = make_router(cfg_dir)
    assert [e.name for e in recovered.endpoints.values()] == ['A']


def test_load_starts_empty_when_primary_and_backup_both_unusable(router, cfg_dir, caplog):
    """No usable config anywhere -> log an error and start empty (unchanged behaviour)."""
    router._config_path.write_text('not json at all', encoding='utf-8')

    with caplog.at_level('ERROR'):
        recovered = make_router(cfg_dir)

    assert recovered.endpoints == {}
    errors = [r.getMessage() for r in caplog.records if r.levelname == 'ERROR']
    assert any('Failed to load config' in m for m in errors), f"got {errors}"


def test_save_load_roundtrip_preserves_values(router, cfg_dir):
    """End-to-end: values written by _save come back identically through _load."""
    add_ep(router, name='A', api_base='http://a', model='m',
           max_input_tokens=120000, concurrency_limit=-1, rate_limit_rpm=30)
    add_ep(router, name='B', api_base='http://b', model='m2',
           max_input_tokens=32000, concurrency_limit=0)
    router.agent_priorities = {'Coder': []}
    router._save()

    reloaded = make_router(cfg_dir)

    by_name = {e.name: e for e in reloaded.endpoints.values()}
    assert by_name['A'].max_input_tokens == 120000
    # -1 == unlimited must survive the round trip rather than collapsing to 0. Asserted
    # via the file on disk too, since that is what the UI echo loop used to rewrite.
    assert by_name['A'].concurrency_limit == -1, '-1 (unlimited) must survive the round trip'
    assert by_name['A'].rate_limit_rpm == 30
    assert by_name['B'].max_input_tokens == 32000
    assert by_name['B'].concurrency_limit == 0

    on_disk = {e['name']: e for e in read_json(reloaded._config_path)['endpoints']}
    assert on_disk['A']['concurrency_limit'] == -1

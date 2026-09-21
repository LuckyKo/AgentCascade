"""Phase E: expose every skill-scoring constant as a user-tweakable setting through the full
6-seam pattern (research §18 + D-SAFE).

Covers:
1. The 10 config handlers — happy path + clamp-low / clamp-high edges (each clamp must be
   byte-identical to the api_server preview endpoint and web_ui/app.js getGenerateCfg).
2. The ``skill_score_rflood`` "must stay < 1" invariant specifically.
3. Backward compatibility: a config file missing every key restores safely (no crash, defaults).
4. The 6-seam wiring smoke test — each new key is present in the persistence list, has a
   registered handler, is seeded in ``initial_llm_cfg``, and is broadcast to the frontend.

Related: plans/skill_scoring_PLAN.md Phase E; design §18.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

# (llm_cfg key, default, low, high, is_int) — the single source of truth for the test grid.
# MUST match config_handlers.py clamps, api_server.py score_overrides, and app.js getGenerateCfg.
_SCORING_SETTINGS = [
    ('skill_score_q0', 5.0, 0.0, 10.0, False),
    ('skill_score_kq', 5, 0, 20, True),
    ('skill_score_nhalf', 8, 1.0, 50.0, False),
    ('skill_score_ghalf', 20, 1.0, 100.0, False),
    ('skill_score_rflood', 0.5, 0.0, 0.99, False),
    ('skill_score_tau_turns', 200, 10, 5000, True),
    ('skill_score_dq', 0.5, 0.1, 3.0, False),
    ('skill_score_nmin', 5, 1, 20, True),
    ('skill_fair_window_turns', 50, 0, 1000, True),
    ('skill_max_evictions_per_pass', 25, 0, 1000, True),
]


def _run_handler(key: str, value):
    """Drive the registered handler for ``key`` with a single-value ui_cfg; return llm_cfg[key]."""
    from agent_cascade.config_handlers import CONFIG_HANDLERS

    pool = MagicMock()
    pool.llm_cfg = {}
    handler = CONFIG_HANDLERS[key]
    handler({key: value}, pool, [])
    return pool.llm_cfg.get(key)


class TestSkillScoringConfigHandlers:
    """Validation of the 10 skill-scoring config handlers (clamp [lo, hi])."""

    def test_all_handlers_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        for key, *_ in _SCORING_SETTINGS:
            assert key in CONFIG_HANDLERS, f"missing handler for {key}"

    def test_handler_valid_value_round_trips(self):
        for key, default, lo, hi, is_int in _SCORING_SETTINGS:
            got = _run_handler(key, default)
            assert got == default, f"{key}: valid value {default} not preserved (got {got})"

    def test_handler_clamps_low(self):
        for key, default, lo, hi, is_int in _SCORING_SETTINGS:
            got = _run_handler(key, lo - 100)
            assert got == lo, f"{key}: low clamp failed (in={lo - 100}, got {got}, want {lo})"

    def test_handler_clamps_high(self):
        for key, default, lo, hi, is_int in _SCORING_SETTINGS:
            got = _run_handler(key, hi + 100)
            assert got == hi, f"{key}: high clamp failed (in={hi + 100}, got {got}, want {hi})"

    def test_handler_unparseable_falls_back_to_default(self):
        for key, default, *_ in _SCORING_SETTINGS:
            got = _run_handler(key, 'not-a-number')
            assert got == default, f"{key}: unparseable input should fall back to default {default}"

    def test_skill_score_rflood_stays_below_one(self):
        """r_floor must stay < 1 (the recency factor r = r_floor + (1-r_floor)*exp(...) needs r_floor<1)."""
        assert _run_handler('skill_score_rflood', 0.99) == 0.99
        assert _run_handler('skill_score_rflood', 1.0) == 0.99
        assert _run_handler('skill_score_rflood', 5.0) == 0.99


class TestBackwardCompatMissingKeys:
    """A pool_settings.json that lacks every new key must load without crashing and leave the
    llm_cfg keys unset (pool/core.py then reads .get() with defaults)."""

    def test_load_pool_settings_missing_keys_is_safe(self, tmp_path):
        from agent_cascade.pool.config_persist import ConfigPersistMixin

        class _Pool(ConfigPersistMixin):
            def __init__(self, path: Path):
                self.llm_cfg = {}
                self._pool_settings_path = path
                self.settings = None

        old_file = tmp_path / 'pool_settings.json'
        # An OLD file with none of the new keys (and a couple of unrelated ones).
        old_file.write_text(json.dumps({'idle_timeout_seconds': 900}), encoding='utf-8')

        pool = _Pool(old_file)
        pool._load_pool_settings()  # must not raise
        for key, *_ in _SCORING_SETTINGS:
            assert key not in pool.llm_cfg, f"{key} should NOT be set from a missing key"

    def test_load_pool_settings_clamps_persisted_values(self, tmp_path):
        """Persisted out-of-range values are re-clamped on restore (defense in depth)."""
        from agent_cascade.pool.config_persist import ConfigPersistMixin

        class _Pool(ConfigPersistMixin):
            def __init__(self, path: Path):
                self.llm_cfg = {}
                self._pool_settings_path = path
                self.settings = None

        f = tmp_path / 'pool_settings.json'
        f.write_text(json.dumps({
            'skill_score_q0': 99.0,          # → 10.0
            'skill_score_rflood': 2.5,       # → 0.99
            'skill_max_evictions_per_pass': -3,  # → 0
        }), encoding='utf-8')

        pool = _Pool(f)
        pool._load_pool_settings()
        assert pool.llm_cfg['skill_score_q0'] == 10.0
        assert pool.llm_cfg['skill_score_rflood'] == 0.99
        assert pool.llm_cfg['skill_max_evictions_per_pass'] == 0


class TestSixSeamWiring:
    """Smoke test: each new key is present in every Python seam that carries SKILL_ACTIVE_*."""

    def _keys(self):
        return [key for key, *_ in _SCORING_SETTINGS]

    def test_in_persist_list_and_broadcast_and_save_trigger(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS
        from agent_cascade.constants import POOL_SETTINGS_TO_BROADCAST

        for key in self._keys():
            assert key in POOL_SETTINGS_KEYS, f"{key} missing from POOL_SETTINGS_KEYS (save trigger)"
            assert key in POOL_SETTINGS_TO_BROADCAST, f"{key} missing from broadcast tuple"

    def test_persist_save_list_and_restore_block(self):
        """config_persist.py: the save-list and the restore data.pop() block both reference each key."""
        src = Path('agent_cascade/pool/config_persist.py').read_text(encoding='utf-8')
        for key in self._keys():
            assert f"'{key}'" in src, f"{key} missing from config_persist save/restore"

    def test_initial_llm_cfg_seed(self):
        """api_server.py initial_llm_cfg seeds every key with its default."""
        src = Path('agent_cascade/api_server.py').read_text(encoding='utf-8')
        for key, default, *_ in _SCORING_SETTINGS:
            assert f"'{key}'" in src, f"{key} missing from api_server.py (initial_llm_cfg seed)"

    def test_settings_module_constants_exist(self):
        import agent_cascade.settings as s

        mapping = {
            'skill_score_q0': s.SKILL_SCORE_Q0,
            'skill_score_kq': s.SKILL_SCORE_KQ,
            'skill_score_nhalf': s.SKILL_SCORE_NHALF,
            'skill_score_ghalf': s.SKILL_SCORE_GHALF,
            'skill_score_rflood': s.SKILL_SCORE_RFLOOR,
            'skill_score_tau_turns': s.SKILL_SCORE_TAU_TURNS,
            'skill_score_dq': s.SKILL_SCORE_DQ,
            'skill_score_nmin': s.SKILL_SCORE_NMIN,
            'skill_fair_window_turns': s.SKILL_FAIR_WINDOW_TURNS,
            'skill_max_evictions_per_pass': s.SKILL_MAX_EVICTIONS_PER_PASS,
        }
        for key, default, *_ in _SCORING_SETTINGS:
            assert mapping[key] == default, f"settings constant for {key} != default {default}"

    def test_frontend_seams_reference_each_key(self):
        """app.js (registry/save/restore/getGenerateCfg) + index.html all reference each key.

        JS has no automated harness here; this is the manual grep checklist promoted to a test
        (OD-4 in the plan). Each key must appear in app.js at least 3× (registry, save, restore,
        getGenerateCfg = 4 sites) and once as an input id in index.html.
        """
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')
        for key in self._keys():
            count = app_js.count(f"'{key}'")
            assert count >= 3, f"{key}: app.js references it {count}× (want ≥3)"
            # index.html input id: 'setting-' + key with underscores → hyphens.
            dom_id = 'setting-' + key.replace('_', '-')
            assert f'id="{dom_id}"' in index_html, f"{key}: missing input id {dom_id} in index.html"


class TestAlwaysProtectedWiring:
    """The ``skill_always_protected`` STRING setting (unremovable meta-skill set) is wired through
    the same non-clamped pattern as ``skill_auto_invalidate_enabled`` — NOT the numeric
    SKILL_SCORE_SETTINGS clamp table. It must be present in every Python seam that carries it."""

    KEY = 'skill_always_protected'

    def test_handler_registered_and_parses_to_lowercase_set(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        pool = MagicMock()
        pool.llm_cfg = {}
        assert self.KEY in CONFIG_HANDLERS, f"missing handler for {self.KEY}"
        CONFIG_HANDLERS[self.KEY]({self.KEY: 'Self-Augmentation, x'}, pool, [])
        assert pool.llm_cfg[self.KEY] == {'self-augmentation', 'x'}

    def test_in_persist_list_and_broadcast(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS
        from agent_cascade.constants import POOL_SETTINGS_TO_BROADCAST
        assert self.KEY in POOL_SETTINGS_KEYS, f"{self.KEY} missing from POOL_SETTINGS_KEYS"
        assert self.KEY in POOL_SETTINGS_TO_BROADCAST, f"{self.KEY} missing from broadcast tuple"

    def test_persist_save_list_and_restore_block(self):
        src = Path('agent_cascade/pool/config_persist.py').read_text(encoding='utf-8')
        assert f"'{self.KEY}'" in src, f"{self.KEY} missing from config_persist save/restore"

    def test_initial_llm_cfg_seed(self):
        src = Path('agent_cascade/api_server.py').read_text(encoding='utf-8')
        assert f"'{self.KEY}'" in src, f"{self.KEY} missing from api_server.py (initial_llm_cfg seed)"

    def test_settings_default_constant_and_parser_exist(self):
        import agent_cascade.settings as s
        assert s.SKILL_ALWAYS_PROTECTED_DEFAULT == (
            'self-augmentation,project-memory-writing,bug-tracker-entry-format,skill-creator')
        assert callable(s.parse_skill_always_protected)

    def test_not_in_numeric_clamp_table(self):
        """It is a STRING setting — deliberately NOT in the numeric-only SKILL_SCORE_SETTINGS table."""
        import agent_cascade.settings as s
        assert self.KEY not in s.SKILL_SCORE_SETTINGS

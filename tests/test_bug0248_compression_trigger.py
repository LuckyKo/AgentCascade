"""BUG_0248 — compression-trigger lowering (force 96->80, warning 90->72, proactive 95->78).

Pins the new env-reversible defaults AND the trigger seams that must fire at them:
  * engine/compression_exec.py::_check_and_trigger_compression  (pre-LLM force + warning band)
  * engine/compression_exec.py::_proactive_compression_check     (post-tool / async-drain proactive)

Counterfactual discipline: every positive case sits ABOVE the new threshold but BELOW the
old one (81 % < old force 96; 79 % > new warning 72 but < old 90; 79 % > new proactive 78 but
< old 95), so each test fails under the legacy defaults — it proves the *lowering*, not merely
that 81 % > 0. Env reversibility is asserted by loading a throwaway copy of settings.py in an
isolated module namespace (mirrors tests/test_sanity_probe.py: `importlib.reload` leaks under
pytest-xdist and its "restore" reload still pins the value).

Mock idiom mirrors tests/test_fallback_compression.py::TestPreSendCompressionEndpointTruthful.
All tests are self-contained — no LLM or API server required.
"""

import os
from unittest.mock import MagicMock, patch

from agent_cascade.llm.schema import USER, Message

# Raw window / reserve chosen so the reserve-reduced effective limit (W_eff) is exactly
# 100_000, making the threshold math exact: 81 % -> 81_000 tok, 79 % -> 79_000 tok.
_MAX_WINDOW = 103_000
_RESERVE = 3_000
_W_EFF = _MAX_WINDOW - _RESERVE  # 100_000

# Old defaults, kept only for the counterfactual bounds (asserted as "< old floor", never as
# the live value).
_OLD_FORCE, _OLD_WARNING, _OLD_PROACTIVE = 96.0, 90.0, 95.0

# Env vars for the three thresholds (the knobs the constants read at import time).
_ENV_FORCE = 'AGENT_CASCADE_COMPRESSION_FORCE_THRESHOLD'
_ENV_WARNING = 'AGENT_CASCADE_COMPRESSION_WARNING_THRESHOLD'
_ENV_PROACTIVE = 'AGENT_CASCADE_DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD'


def _tokens_at(pct: float) -> int:
    """Token count giving exactly ``pct`` percent of W_eff (= 100_000)."""
    return int(round(_W_EFF * pct / 100.0))


def _load_settings_copy(monkeypatch, env: dict):
    """Load a throwaway copy of settings.py in an isolated namespace with ``env`` applied.

    Returns the module so we read the RAW, import-time-computed defaults without mutating the
    global ``agent_cascade.settings`` module (which would leak across parallel workers). Any of
    the three threshold env vars not present in ``env`` are explicitly removed so the default
    path is exercised deterministically.
    """
    import importlib.util

    for key in (_ENV_FORCE, _ENV_WARNING, _ENV_PROACTIVE):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'agent_cascade', 'settings.py')
    spec = importlib.util.spec_from_file_location('_bug0248_settings_copy', settings_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────────────────────────────────────────────────────────────
# Unit: the two trigger seams at the new thresholds
# ──────────────────────────────────────────────────────────────────────────────

class TestCompressionTriggerLowering:
    """Pre-LLM force guard and post-tool proactive check must fire at the lowered defaults."""

    def _make_engine(self, force=80.0, warning=72.0, proactive=78.0):
        """Real ExecutionEngine over a MagicMock pool; thresholds set on pool.settings
        (the same attributes the production code reads)."""
        from agent_cascade.execution_engine import ExecutionEngine

        pool = MagicMock()
        pool.settings.compression_force_threshold = force
        pool.settings.compression_warning_threshold = warning
        pool.settings.compression_proactive_threshold = proactive
        pool.settings.compression_context_reserve_tokens = _RESERVE
        pool.settings.compression_max_attempts = 100
        pool.settings.compression_force_cooldown = 0.0

        instance = MagicMock()
        lock = MagicMock()
        lock.__enter__ = MagicMock()
        lock.__exit__ = MagicMock()
        instance._compression_lock = lock
        instance.instance_name = 'bug0248-test'
        instance.agent_class = 'Coder'
        # First-turn path: no cached ground-truth count -> guard recounts messages.
        instance._last_actual_token_count = 0
        instance._allocated_max_input_tokens = 0
        instance._last_token_count_conversation_length = -1
        instance._cached_token_count = 0
        instance._force_compress_count = 0
        instance._last_force_compress_time = 0.0

        engine = ExecutionEngine(pool)
        return engine, pool, instance

    def _run_pre_llm_guard(self, engine, instance, token_count):
        """Drive the pre-LLM guard with an exact token count.

        Returns (triggered, force_compression_mock, warning_inject_mock).
        """
        with patch.object(engine, '_count_history_tokens', return_value=token_count), \
             patch.object(engine, '_get_max_tokens', return_value=_MAX_WINDOW), \
             patch.object(engine, '_force_compression', return_value=True) as mock_force, \
             patch.object(engine, '_inject_compression_warning') as mock_warn:
            messages = [Message(role=USER, content='payload')]
            triggered = engine._check_and_trigger_compression(
                instance, messages, list(messages), None)
        return triggered, mock_force, mock_warn

    def test_force_fires_at_81_pct_pre_llm(self):
        """81 % of W_eff (> new force 80) triggers pre-LLM compression.

        Counterfactual: 81 % < old force 96 — this fires ONLY because the default dropped.
        """
        engine, _pool, instance = self._make_engine(force=80.0)
        # Sanity: 81k sits strictly below the old 96k floor, so legacy would not have fired.
        assert _tokens_at(81.0) < _OLD_FORCE / 100.0 * _W_EFF

        triggered, mock_force, mock_warn = self._run_pre_llm_guard(
            engine, instance, _tokens_at(81.0))
        assert triggered is True, '81 % > force 80 must trigger pre-LLM compression'
        mock_force.assert_called_once()
        mock_warn.assert_not_called()  # force returns before the warning band

    def test_below_force_above_warning_injects_warning(self):
        """79 % of W_eff: not forced (79 < 80) but above the warning band (> new warning 72),
        so a warning banner is injected and compression is skipped.

        Counterfactual: 79 % < old warning 90 — the banner fires ONLY because warning dropped
        to 72.
        """
        engine, _pool, instance = self._make_engine(force=80.0, warning=72.0)
        # Sanity: 79k sits strictly below the old 90k floor, so legacy would not have warned.
        assert _tokens_at(79.0) < _OLD_WARNING / 100.0 * _W_EFF

        triggered, mock_force, mock_warn = self._run_pre_llm_guard(
            engine, instance, _tokens_at(79.0))
        assert triggered is False, '79 % < force 80 must not compress'
        mock_force.assert_not_called()
        mock_warn.assert_called_once()  # 79 % > warning 72 -> banner injected

    def _run_proactive_check(self, engine, instance, token_count):
        """Drive the post-tool / async-drain proactive check with an exact token count."""
        with patch.object(engine, '_count_history_tokens', return_value=token_count), \
             patch.object(engine, '_get_max_tokens', return_value=_MAX_WINDOW), \
             patch.object(engine, '_force_compression', return_value=True) as mock_force:
            messages = [Message(role=USER, content='payload')]
            engine._proactive_compression_check(instance, messages, list(messages),
                                                check_label='post-tool')
        return mock_force

    def test_proactive_fires_at_79_pct_post_tool(self):
        """Proactive check (post-tool path) compresses at 79 % (> new proactive 78).

        Counterfactual: 79 % < old proactive 95 — fires ONLY because proactive dropped to 78.
        """
        engine, _pool, instance = self._make_engine(proactive=78.0)
        # Sanity: 79k sits strictly below the old 95k floor, so legacy would not have fired.
        assert _tokens_at(79.0) < _OLD_PROACTIVE / 100.0 * _W_EFF

        mock_force = self._run_proactive_check(engine, instance, _tokens_at(79.0))
        mock_force.assert_called_once()  # 79 % > proactive 78 -> post-tool compression

    def test_proactive_below_threshold_does_not_fire(self):
        """77 % of W_eff (< new proactive 78) must NOT fire the proactive check — guards
        against an off-by-one that would over-compress."""
        engine, _pool, instance = self._make_engine(proactive=78.0)

        mock_force = self._run_proactive_check(engine, instance, _tokens_at(77.0))
        mock_force.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# Env reversibility: the three AGENT_CASCADE_* vars restore the old defaults
# ──────────────────────────────────────────────────────────────────────────────

class TestCompressionTriggerEnvReversibility:

    def test_new_defaults_are_the_source_constants(self, monkeypatch):
        """With the env vars unset, a fresh import of settings.py yields the NEW defaults."""
        mod = _load_settings_copy(monkeypatch, {})
        assert mod.COMPRESSION_FORCE_THRESHOLD == 80.0
        assert mod.COMPRESSION_WARNING_THRESHOLD == 72.0
        assert mod.DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD == 78.0

    def test_env_reverts_to_old_defaults(self, monkeypatch):
        """AGENT_CASCADE_* set to the old values restores 96 / 90 / 95 on a fresh import."""
        mod = _load_settings_copy(monkeypatch, {
            _ENV_FORCE: '96',
            _ENV_WARNING: '90',
            _ENV_PROACTIVE: '95',
        })
        assert mod.COMPRESSION_FORCE_THRESHOLD == 96.0
        assert mod.COMPRESSION_WARNING_THRESHOLD == 90.0
        assert mod.DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD == 95.0

"""Pin the compiled default of the default-ON feature flag.

This is an env-backed setting (overridable via AGENT_CASCADE_*), enabled by default:

- ``SOFT_CONTINUE_NUDGE_ENABLED``: inject an escalating USER nudge on reasoning-only soft
  continues (engine/response_pipeline.py) instead of a bare pure-resend.

The default behavior is otherwise covered by the feature suite
(tests/test_reasoning_only_continue_retry.py). This test only guards that
the flag does not silently regress back to OFF-by-default. Mirrors ``TestDefaults`` in
test_skill_cache_settings.py.
"""


class TestDefaults:
    def test_soft_continue_nudge_defaults_on(self):
        import agent_cascade.settings as s
        assert s.SOFT_CONTINUE_NUDGE_ENABLED is True

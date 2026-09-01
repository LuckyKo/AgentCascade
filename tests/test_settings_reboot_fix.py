"""Regression test for FIX 1 (server-side boot clobber) of the "UI settings reset after reboot" bug.

todo.md line 145 · see reports/settings_reboot_reset_investigation.md (R1).

Root cause: on every server start, the ``if __name__ == "__main__"`` block in api_server.py
resolved idle-timeout values from CLI > env > hardcoded defaults and applied them to
``pool.settings`` UNCONDITIONALLY, then called ``_save_pool_settings()`` — overwriting the user's
persisted ``pool_settings.json`` with the defaults (1600 / 60 / 60) on every boot.

Fix: only apply + persist a key when it was EXPLICITLY overridden (CLI arg not None, or the env var
is present). When nothing is overridden, pool.settings and the file are left untouched so the values
loaded from ``pool_settings.json`` in AgentPool.__init__ survive boot.

This test drives the REAL api_server.py startup code path (the ``if __name__ == "__main__"`` block)
via runpy, with:
  * a pre-seeded isolated config dir (AGENT_CASCADE_TEST_CONFIG_DIR) holding pool_settings.json with
    user values that differ from the hardcoded defaults, and
  * AgentPool / OperationManager patched to lightweight fakes so no real model init / network occurs.

Run:  pytest tests/test_settings_reboot_fix.py -v
"""

import json
import os
import runpy
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_SERVER = os.path.join(PROJECT_ROOT, "agent_cascade", "api_server.py")

# The three env vars the startup block checks for explicit overrides.
IDLE_ENV_VARS = (
    "QWEN_AGENT_IDLE_TIMEOUT",
    "QWEN_AGENT_SYSTEM_AGENT_IDLE_TIMEOUT",
    "QWEN_AGENT_IDLE_CHECK_INTERVAL",
)


class _FakeSettings:
    """Minimal stand-in for PoolSettings carrying the three startup-managed fields."""

    def __init__(self, **kw):
        self.idle_timeout_seconds = kw.get("idle_timeout_seconds", 1600.0)
        self.system_agent_idle_timeout_seconds = kw.get("system_agent_idle_timeout_seconds", 60.0)
        self.idle_check_interval = kw.get("idle_check_interval", 60.0)


class _FakePool:
    """Lightweight AgentPool stand-in.

    ``settings`` carries the values that (in production) would have been loaded from
    pool_settings.json by AgentPool.__init__ via _load_pool_settings(). We seed it here with the
    user's persisted values so we can assert the startup block does NOT clobber them.
    """

    def __init__(self, **settings_kw):
        self.settings = _FakeSettings(**settings_kw)
        self.save_calls = 0
        # Attributes create_app() reads after the startup block (must not raise).
        self.instances = {}
        self.instance_summaries = {}
        self._ws_loop = None
        self.instance_state = {}
        self.message_queues = {}
        self.stopped = False
        self.operation_manager = None
        # Template registry: create_app() reads agent_pool.agents['orchestrator'] and iterates list_agents().
        self.agents = {"orchestrator": SimpleNamespace(name="orchestrator")}

    def _save_pool_settings(self):
        self.save_calls += 1

    def create_instance(self, *a, **k):
        return SimpleNamespace(conversation=[])

    def start(self):
        return None

    def get_agent(self, name):
        # Return a template-like object; _extract_system_message must treat it as "no system msg".
        return self.agents.get(name) or SimpleNamespace(
            system_prompt=None,
            soul_content=None,
            conversation=[],
            name=name,
        )

    def get_instance(self, name):
        # create_app() calls agent_pool.get_instance('Maine') to inject the system message.
        return self.instances.get(name)

    def list_agents(self):
        return list(self.agents.keys())


def _clear_idle_env():
    for var in IDLE_ENV_VARS:
        os.environ.pop(var, None)


def _seed_pool_settings(config_dir):
    """Write a pool_settings.json with user values that differ from the hardcoded defaults."""
    data = {
        "idle_timeout_seconds": 900.0,          # user value (NOT the 1600 default)
        "system_agent_idle_timeout_seconds": 45.0,  # user value (NOT the 60 default)
        "idle_check_interval": 30.0,            # user value (NOT the 60 default)
    }
    path = os.path.join(config_dir, "pool_settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def _run_api_server_main(tmp_path, cli_kwargs):
    """Run api_server.py's __main__ block via runpy with a controlled argv and patched deps.

    Returns (fake_pool, pre_data). The fake pool is the one injected as AgentPool's return value.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    pre_data = _seed_pool_settings(str(config_dir))

    # Pre-seed the fake pool with the user's persisted values (what AgentPool.__init__ would load).
    fake_pool = _FakePool(**pre_data)

    argv = ["api_server.py", "--workspace", str(tmp_path)]
    if cli_kwargs.get("idle_timeout") is not None:
        argv += ["--idle-timeout", str(cli_kwargs["idle_timeout"])]
    if cli_kwargs.get("system_agent_idle_timeout") is not None:
        argv += ["--system-agent-idle-timeout", str(cli_kwargs["system_agent_idle_timeout"])]
    if cli_kwargs.get("idle_check_interval") is not None:
        argv += ["--idle-check-interval", str(cli_kwargs["idle_check_interval"])]

    _clear_idle_env()

    # Patch the heavy deps so no real model init / network / file churn occurs. uvicorn.run is
    # patched to a no-op so the server does not actually start (it would block the test).
    with mock.patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": str(config_dir)}), \
         mock.patch("agent_cascade.agent_pool.AgentPool", return_value=fake_pool) as pool_mock, \
         mock.patch("agent_cascade.operation_manager.OperationManager") as om_mock, \
         mock.patch("uvicorn.run"):
        # OperationManager must expose the attrs create_app() reads.
        om = SimpleNamespace(
            base_dir=str(tmp_path),
            extra_work_folders_ro=[],
            extra_work_folders_rw=[],
            enable_timeout=False,
            approval_timeout_seconds=300,
            agent_pool=None,
        )
        om_mock.return_value = om

        # runpy executes the module top-level; the `if __name__ == "__main__"` block runs because
        # runpy sets __name__ to "__main__" for the executed script.
        with mock.patch.object(sys, "argv", argv):
            runpy.run_path(API_SERVER, run_name="__main__")

    assert pool_mock.called, "AgentPool was not constructed — startup path not exercised"
    return fake_pool, pre_data


class TestBootClobberFix:
    """FIX 1: boot must not clobber persisted idle-timeout settings unless explicitly overridden."""

    def test_no_override_presists_persisted_values(self, tmp_path):
        """Boot with NO CLI/env overrides → pool.settings keeps the file's values; no re-save."""
        fake_pool, pre = _run_api_server_main(tmp_path, {})

        # The user's persisted values must survive boot (not reset to 1600/60/60).
        assert fake_pool.settings.idle_timeout_seconds == 900.0, \
            f"idle_timeout_seconds clobbered: {fake_pool.settings.idle_timeout_seconds} != 900.0"
        assert fake_pool.settings.system_agent_idle_timeout_seconds == 45.0, \
            f"system idle clobbered: {fake_pool.settings.system_agent_idle_timeout_seconds}"
        assert fake_pool.settings.idle_check_interval == 30.0, \
            f"idle_check_interval clobbered: {fake_pool.settings.idle_check_interval}"

        # No re-save → the file on disk is NOT re-stamped with defaults.
        assert fake_pool.save_calls == 0, \
            f"_save_pool_settings called {fake_pool.save_calls}x — file would be re-stamped"

        # The file still holds the user's values (not rewritten).
        data = json.load(open(os.path.join(str(tmp_path / "config"), "pool_settings.json")))
        assert data["idle_timeout_seconds"] == 900.0
        assert data["system_agent_idle_timeout_seconds"] == 45.0

    def test_cli_override_wins_and_persists(self, tmp_path):
        """Boot WITH an explicit CLI override → the override wins and is persisted."""
        fake_pool, _ = _run_api_server_main(
            tmp_path,
            {"idle_timeout": 1234.0, "system_agent_idle_timeout": 77.0, "idle_check_interval": 15.0},
        )

        # The explicit CLI values must win over the persisted ones.
        assert fake_pool.settings.idle_timeout_seconds == 1234.0
        assert fake_pool.settings.system_agent_idle_timeout_seconds == 77.0
        assert fake_pool.settings.idle_check_interval == 15.0

        # The override path must persist (re-save) so the new values survive future boots.
        assert fake_pool.save_calls >= 1, "override was applied but not persisted"

    def test_env_override_wins_and_persists(self, tmp_path):
        """Boot with an explicit env-var override (no CLI) → the env value wins and is persisted."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        pre = _seed_pool_settings(str(config_dir))
        fake_pool = _FakePool(**pre)

        argv = ["api_server.py", "--workspace", str(tmp_path)]
        env = {
            "AGENT_CASCADE_TEST_CONFIG_DIR": str(config_dir),
            "QWEN_AGENT_IDLE_TIMEOUT": "2000",  # explicit env override
        }

        _clear_idle_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch("agent_cascade.agent_pool.AgentPool", return_value=fake_pool) as pool_mock, \
             mock.patch("agent_cascade.operation_manager.OperationManager") as om_mock, \
             mock.patch("uvicorn.run"):
            om = SimpleNamespace(
                base_dir=str(tmp_path), extra_work_folders_ro=[], extra_work_folders_rw=[],
                enable_timeout=False, approval_timeout_seconds=300, agent_pool=None,
            )
            om_mock.return_value = om
            with mock.patch.object(sys, "argv", argv):
                runpy.run_path(API_SERVER, run_name="__main__")

        assert pool_mock.called
        # Env override wins for idle_timeout_seconds; the other two (no override) keep file values.
        assert fake_pool.settings.idle_timeout_seconds == 2000.0, \
            f"env override not applied: {fake_pool.settings.idle_timeout_seconds}"
        assert fake_pool.settings.system_agent_idle_timeout_seconds == 45.0, \
            "non-overridden system idle was clobbered"
        assert fake_pool.save_calls >= 1, "env override was applied but not persisted"

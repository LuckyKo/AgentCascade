"""Tests for the Windows console Ctrl+C defense-in-depth guard (shared_init).

The guard installs a SetConsoleCtrlHandler on the AC server process so a
console-wide Ctrl+C broadcast cannot hard-kill the instance. The critical
correctness constraint is that the callback must RE-DISPATCH SIGINT (not be a
pure no-op) for CTRL_C/BREAK/CLOSE/SHUTDOWN, and return False for CTRL_LOGOFF.

These tests run on non-Windows CI too: the public entry point is a natural
no-op there, and the internal installer is exercised via an injected fake
kernel32 (we do NOT monkeypatch os.name — modules cache it at import).
"""

import ctypes
from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.shared_init as _shared
from agent_cascade.shared_init import (
    install_console_ctrl_guard,
    _install_windows_console_guard,
)


# CTRL_* event types (wincon.h) — pin the exact values used by the guard.
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 4
CTRL_SHUTDOWN_EVENT = 5


class _FakeKernel32:
    """Minimal stand-in for ctypes.windll.kernel32 that records registrations."""

    def __init__(self, ok=True):
        self.ok = ok
        self.registrations = []  # list of (handler, add) tuples

    def SetConsoleCtrlHandler(self, handler, add):
        self.registrations.append((handler, add))
        return int(self.ok)


@pytest.fixture(autouse=True)
def _reset_guard_state():
    """Reset module-level guard state before and after each test.

    Essential for the idempotency test (#2): without this, a previous test's
    install would make a later call short-circuit to False.
    """
    _shared._console_ctrl_handler = None
    _shared._console_ctrl_raw_handler = None
    _shared._console_guard_installed = False
    yield
    _shared._console_ctrl_handler = None
    _shared._console_ctrl_raw_handler = None
    _shared._console_guard_installed = False


class TestConsoleCtrlGuard:

    def test_guard_noop_on_non_windows(self):
        """install_console_ctrl_guard() is a safe no-op on non-Windows.

        On Windows CI it will actually install (returns True); on Linux/macOS
        it must return False, raise nothing, and leave the SIGINT handler alone.
        Idempotent: a second call also returns the same value without error.
        """
        import os
        import signal as _sig

        before = _sig.getsignal(_sig.SIGINT)
        result1 = install_console_ctrl_guard()
        assert _sig.getsignal(_sig.SIGINT) is before  # SIGINT handler unchanged

        if os.name != 'nt':
            assert result1 is False
            # Idempotent second call also returns False.
            assert install_console_ctrl_guard() is False
        else:
            # On Windows the guard installs; a second call must be idempotent (False).
            assert result1 is True
            assert install_console_ctrl_guard() is False

    def test_guard_install_idempotent_with_fake_kernel32(self):
        """The internal installer registers exactly once and is idempotent.

        First call -> True, one registration, module retains a non-None handler.
        Second call -> False, no new registration.
        """
        fake = _FakeKernel32(ok=True)

        first = _install_windows_console_guard(kernel32=fake)
        assert first is True
        assert len(fake.registrations) == 1
        handler, add = fake.registrations[0]
        assert add is True
        # Module retains a live ctypes callback (must not be GC'd).
        assert _shared._console_ctrl_handler is not None
        assert _shared._console_guard_installed is True

        # Second call: idempotent — returns False, no additional registration.
        second = _install_windows_console_guard(kernel32=fake)
        assert second is False
        assert len(fake.registrations) == 1

    def test_guard_callback_redispatches_sigint(self):
        """CRITICAL regression pin: the callback re-dispatches SIGINT (not a no-op).

        A revert to a pure no-op handler (returns True without raise_signal)
        would fail this test. We invoke the raw Python callable behind the ctypes
        wrapper (exposed via _console_ctrl_raw_handler) so the logic runs on this
        thread, and we monkeypatch signal.raise_signal to observe re-dispatch.
        """
        import signal as _sig

        fake = _FakeKernel32(ok=True)
        assert _install_windows_console_guard(kernel32=fake) is True
        raw = _shared._console_ctrl_raw_handler
        assert callable(raw), "test hook: raw handler must be reachable"

        with patch.object(_sig, 'raise_signal') as mock_raise:
            # CTRL_C / CTRL_BREAK / CTRL_CLOSE / CTRL_SHUTDOWN -> re-dispatch + handled.
            for evt in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_SHUTDOWN_EVENT):
                mock_raise.reset_mock()
                result = raw(evt)
                assert result is True, f"event {evt} should be marked handled"
                mock_raise.assert_called_once_with(_sig.SIGINT)

            # CTRL_LOGOFF -> not handled, and NO re-dispatch.
            mock_raise.reset_mock()
            result = raw(CTRL_LOGOFF_EVENT)
            assert result is False, "logoff must fall through to default handler"
            mock_raise.assert_not_called()

    def test_wiring_pins_source_contains_guard_call(self):
        """Wiring pins (source inspection): both entry points invoke the guard.

        Mirrors the source-pinning pattern in test_async_shell_cmd.py — fails on
        revert even on non-Windows CI.
        """
        import inspect
        from pathlib import Path

        # 1) shared_init.setup_signal_handler calls install_console_ctrl_guard(
        setup_src = inspect.getsource(_shared.setup_signal_handler)
        assert 'install_console_ctrl_guard(' in setup_src

        # 2) start_multi_agent.py source contains the call.
        repo_root = Path(__file__).resolve().parent.parent
        sma_src = (repo_root / 'start_multi_agent.py').read_text(encoding='utf-8')
        assert 'install_console_ctrl_guard(' in sma_src

"""BUG_0036 regression tests — EBADF-tolerant terminal sink proxy.

The flake: under pytest-xdist on Windows the xdist CONTROLLER's stdout is a real
tty, so ``TerminalWriter`` wraps it in colorama.  When an environmental trigger
closes that console handle mid-run (upstream pytest-dev/pytest#10434), pytest 9's
per-report ``write_raw(..., flush=True)`` hits the dead handle and kills the whole
session with ``INTERNALERROR: OSError: [Errno 9] Bad file descriptor``.

The fix wraps the terminal reporter's file sink in ``_TolerantFileProxy`` (see
tests/_ebadf_sink.py).  These tests pin down that behavior deterministically.
"""

import errno
import os
from pathlib import Path

import pytest

from _pytest._io.terminalwriter import TerminalWriter

from tests._ebadf_sink import _TolerantFileProxy, install_tolerant_terminal_sink


# ---------------------------------------------------------------------------
# 1. Deterministic reproduction of the BUG_0036 crash + proof the proxy fixes it
# ---------------------------------------------------------------------------

def test_write_raw_after_fd_close_does_not_raise(tmp_path: Path):
    """Closing the underlying fd then write_raw(flush=True) must NOT raise.

    Without the proxy this reproduces the exact BUG_0036 traceback
    (OSError EBADF from colorama's flush of a dead handle).
    """
    f = open(tmp_path / 'sink.txt', 'w', encoding='utf-8')
    writer = TerminalWriter(f)
    # Sanity: with the raw file, closing the fd and flushing DOES raise EBADF.
    fd = f.fileno()
    os.close(fd)  # close the real handle; f still holds a dead fd
    try:
        with pytest.raises(OSError) as excinfo:
            writer.write_raw('x', flush=True)
        assert excinfo.value.errno == errno.EBADF
    finally:
        try:
            f.close()  # os.close already done; file object close is a no-op on fd
        except OSError:
            pass

    # Now with the proxy in place — the exact BUG_0036 call must not raise.
    f2 = open(tmp_path / 'sink2.txt', 'w', encoding='utf-8')
    writer2 = TerminalWriter(f2)
    writer2._file = _TolerantFileProxy(writer2._file)  # wrap what the writer uses
    os.close(f2.fileno())
    try:
        writer2.write_raw('x', flush=True)  # must NOT raise (BUG_0036 fix)
    finally:
        try:
            f2.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 2. Non-tolerated errors are still re-raised (no over-masking)
# ---------------------------------------------------------------------------

class _FakeFileWithValueErrorFlush:
    """File-like object whose flush() raises a NON-OSError (ValueError)."""

    encoding = 'utf-8'

    def write(self, data):
        return len(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        raise ValueError('simulated non-I/O fault')


def test_non_ebadf_error_is_reraised():
    proxy = _TolerantFileProxy(_FakeFileWithValueErrorFlush())
    with pytest.raises(ValueError, match='simulated non-I/O fault'):
        proxy.flush()

    # A real OSError with a DIFFERENT errno (e.g. EIO) must also propagate.
    class _FakeEioFile:
        def write(self, data):
            raise OSError(errno.EIO, 'simulated I/O error')

        def flush(self):
            pass

    proxy2 = _TolerantFileProxy(_FakeEioFile())
    with pytest.raises(OSError) as excinfo:
        proxy2.write('x')
    assert excinfo.value.errno == errno.EIO

    # UnicodeEncodeError (handled specially by TerminalWriter.write_raw) must propagate too.
    class _FakeUnicodeFile:
        def write(self, data):
            raise UnicodeEncodeError('utf-8', 'x', 0, 1, 'invalid start byte')

        def flush(self):
            pass

    proxy3 = _TolerantFileProxy(_FakeUnicodeFile())
    with pytest.raises(UnicodeEncodeError):
        proxy3.write('x')


# ---------------------------------------------------------------------------
# 3. Proxy delegates attributes to the wrapped file transparently
# ---------------------------------------------------------------------------

def test_proxy_delegates_attributes(tmp_path: Path):
    f = open(tmp_path / 'sink.txt', 'w', encoding='utf-8')
    proxy = _TolerantFileProxy(f)
    try:
        assert proxy.isatty() == f.isatty()
        assert proxy.fileno() == f.fileno()
        assert proxy.encoding == f.encoding
        assert proxy.mode == f.mode
        assert proxy.closed == f.closed
        # write works normally while the handle is alive and returns like a file
        n = proxy.write('hello')
        assert n == len('hello')
    finally:
        f.close()


# ---------------------------------------------------------------------------
# 4. install_tolerant_terminal_sink swaps the real reporter's sink (integration)
# ---------------------------------------------------------------------------

def test_install_hook_wraps_reporter_sink():
    """install_tolerant_terminal_sink must wrap terminalreporter._tw._file."""
    from _pytest.config import Config
    from _pytest.terminal import TerminalReporter

    config = Config.fromdictargs({}, [])  # minimal in-memory config
    tr = TerminalReporter(config, file=open(os.devnull, 'w', encoding='utf-8'))
    config.pluginmanager.register(tr, 'terminalreporter')
    try:
        install_tolerant_terminal_sink(config)
        assert isinstance(tr._tw._file, _TolerantFileProxy)
        # Idempotent: a second call must not double-wrap.
        first = tr._tw._file
        install_tolerant_terminal_sink(config)
        assert tr._tw._file is first
    finally:
        config.pluginmanager.unregister(tr)
        if isinstance(tr._tw._file, _TolerantFileProxy):
            tr._tw._file._real.close()
        else:
            tr._tw._file.close()

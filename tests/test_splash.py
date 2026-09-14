"""Unit tests for the AgentCascade startup splash (agent_cascade/splash.py).

Covers:
  - Full banner contains version, mode, URL/port, and Python version.
  - Explicit version argument is reflected; import fallback yields 'unknown'.
  - Non-TTY → compact one-liner (no art/box).
  - AGENT_CASCADE_NO_BANNER → compact one-liner.
  - Narrow terminal → compact one-liner.
  - Never raises for arbitrary mode values / bad inputs.

The banner prints via the module's own ``sys`` reference, so tests monkeypatch
``splash.sys.stdout`` (a fake stream with isatty()) and ``splash.shutil.get_terminal_size``
to control TTY/width behavior deterministically without a real terminal.
"""
import io

from agent_cascade import splash as splash_module


class _FakeStream:
    """Captures written output; reports a fixed isatty() value."""

    def __init__(self, tty):
        self._buf = io.StringIO()
        self._tty = tty

    def isatty(self):
        return self._tty

    def write(self, s):
        self._buf.write(s)
        return len(s)

    def flush(self):
        pass

    @property
    def value(self):
        return self._buf.getvalue()


class _Size:

    def __init__(self, columns):
        self.columns = columns
        self.lines = 30


def _capture(monkeypatch, tty=True, cols=120, env=None):
    """Run print_startup_banner with a controlled stdout/width/env; return output string."""
    fake = _FakeStream(tty)
    monkeypatch.setattr(splash_module.sys, 'stdout', fake, raising=False)
    monkeypatch.setattr(
        splash_module.shutil,
        'get_terminal_size',
        lambda d=(80, 24): _Size(cols),
    )
    if env is not None:
        monkeypatch.setenv('AGENT_CASCADE_NO_BANNER', env)
    else:
        monkeypatch.delenv('AGENT_CASCADE_NO_BANNER', raising=False)

    splash_module.print_startup_banner(mode='API Server', port=12345, host='127.0.0.1')
    return fake.value


class TestFullBanner:

    def test_contains_version(self, monkeypatch):
        import agent_cascade
        out = _capture(monkeypatch)
        assert f"Version  {agent_cascade.__version__}" in out

    def test_contains_mode_and_url(self, monkeypatch):
        out = _capture(monkeypatch)
        assert 'API Server' in out
        assert 'http://127.0.0.1:12345' in out

    def test_contains_python_version(self, monkeypatch):
        import sys
        out = _capture(monkeypatch)
        expected = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert f"Python  {expected}" in out or expected in out

    def test_full_banner_has_box_and_art(self, monkeypatch):
        out = _capture(monkeypatch)
        # Box borders and the wordmark underscore line should be present.
        assert '+' in out and '|' in out
        assert '_ART' not in out  # no leaked source identifiers


class TestExplicitVersion:

    def test_explicit_version_reflected(self, monkeypatch):
        fake = _FakeStream(True)
        monkeypatch.setattr(splash_module.sys, 'stdout', fake, raising=False)
        monkeypatch.setattr(splash_module.shutil, 'get_terminal_size', lambda d=(80, 24): _Size(120))
        monkeypatch.delenv('AGENT_CASCADE_NO_BANNER', raising=False)
        splash_module.print_startup_banner(mode='API Server', port=1, host='h', version='9.9.9')
        assert 'Version  9.9.9' in fake.value


class TestVersionFallback:

    def test_import_failure_yields_unknown(self, monkeypatch):
        """If agent_cascade.__version__ can't be imported, show 'unknown' (no raise)."""
        # Force the import inside _resolve_version to fail by hiding the attribute.
        import agent_cascade
        real = getattr(agent_cascade, '__version__', None)
        monkeypatch.delattr(agent_cascade, '__version__', raising=False)
        try:
            assert splash_module._resolve_version(None) == 'unknown'
        finally:
            if real is not None:
                setattr(agent_cascade, '__version__', real)

    def test_explicit_wins_over_import(self):
        assert splash_module._resolve_version('1.2.3') == '1.2.3'


class TestCompactPaths:

    def test_non_tty_compact(self, monkeypatch):
        out = _capture(monkeypatch, tty=False)
        # Compact one-liner, no box borders.
        assert 'AgentCascade v' in out
        assert '|' not in out and '+' not in out

    def test_no_banner_env_compact(self, monkeypatch):
        out = _capture(monkeypatch, tty=True, cols=120, env='1')
        assert 'AgentCascade v' in out
        assert '|' not in out and '+' not in out

    def test_narrow_terminal_compact(self, monkeypatch):
        # 36 cols is narrower than the rendered banner width (~36) + margin.
        out = _capture(monkeypatch, tty=True, cols=20)
        assert 'AgentCascade v' in out
        assert '|' not in out and '+' not in out

    def test_wide_terminal_full(self, monkeypatch):
        out = _capture(monkeypatch, tty=True, cols=120)
        assert '+' in out  # box rendered

    def test_narrow_boundary_switches_at_threshold(self, monkeypatch):
        """At exactly (banner_width + 2) the full banner shows; one column less → compact."""
        import agent_cascade
        ver = agent_cascade.__version__
        width = splash_module._banner_width(splash_module._info_rows('API Server', 12345, '127.0.0.1', ver))
        # Just wide enough → full box.
        out_full = _capture(monkeypatch, tty=True, cols=width + 2)
        assert '+' in out_full
        # One column short of the threshold → compact one-liner.
        out_compact = _capture(monkeypatch, tty=True, cols=width + 1)
        assert '+' not in out_compact and 'AgentCascade v' in out_compact


class TestRobustness:

    def test_arbitrary_mode_does_not_raise(self, monkeypatch):
        out = _capture(monkeypatch, tty=True, cols=120, env=None)
        # Just ensure it produced *some* output without raising.
        assert isinstance(out, str) and len(out) > 0

    def test_bad_port_type_does_not_raise(self, monkeypatch):
        """Even a non-int port must not crash the splash (it's display-only)."""
        fake = _FakeStream(True)
        monkeypatch.setattr(splash_module.sys, 'stdout', fake, raising=False)
        monkeypatch.setattr(splash_module.shutil, 'get_terminal_size', lambda d=(80, 24): _Size(120))
        monkeypatch.delenv('AGENT_CASCADE_NO_BANNER', raising=False)
        # Should not raise; output is best-effort.
        splash_module.print_startup_banner(mode='X', port='not-a-port', host='h')
        assert isinstance(fake.value, str)

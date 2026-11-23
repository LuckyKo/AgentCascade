"""BUG_0389 static guard: agent tab refresh after session load.

After a session swap (loadSession(path, 'new_stack')) or new session
(loadSession(path, 'replace')), the freshly auto-activated agent tab's panel was
left empty until a manual click. The fix sets ``state.sessionJustLoaded`` in
``loadSession``, consumes it into a local at the top of ``renderSubAgents`` (so
re-entry and the empty-agents early return can't strand it), tracks whether the
auto-activation block fired (``didAutoActivate``), and calls ``switchMainTab`` on
the active tab — gated on all three.

No DOM-level JS harness exists, so this is a presence guard only: it proves the
setter, the consume-and-clear, the didAutoActivate tracking, and the gated
``switchMainTab`` call are all still present in app.js. Removing any seam would
re-introduce the empty-tab bug with zero Python-test coverage. Mirrors the shape
of test_hint_usage_ui_wiring.py (feature-specific static-assert on app.js text).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / 'web_ui' / 'app.js').read_text(encoding='utf-8')


class TestBug0389AgentTabRefreshGuard:
    """Each seam of the BUG_0389 fix must remain present in app.js."""

    def test_setter_present(self):
        # loadSession sets the flag at the top of its ws-OPEN branch.
        assert 'state.sessionJustLoaded = true;' in APP_JS

    def test_consumer_present(self):
        # renderSubAgents consumes the flag into a local and clears it immediately.
        assert 'const sessionJustLoaded = state.sessionJustLoaded;' in APP_JS
        assert 'state.sessionJustLoaded = false;' in APP_JS

    def test_autoactivate_tracking_present(self):
        # renderSubAgents records whether the auto-activation block fired this pass.
        assert 'let didAutoActivate = false;' in APP_JS
        assert 'didAutoActivate = true;' in APP_JS

    def test_gated_switchmantab_present(self):
        # The refresh fires only when a session just loaded AND auto-activation
        # fired AND a tab is active — reuses the click-refresh path (switchMainTab).
        assert (
            'if (sessionJustLoaded && didAutoActivate && state.activeSubTab) {'
            in APP_JS
        )
        assert 'switchMainTab(state.activeSubTab);' in APP_JS

"""BUG_0345 S6: stack-swap / sessions UI wiring round-trip test.

Mirrors ``tests/test_slot_queue_timeout_ui_wiring.py``: it promotes the manual grep
checklist for the two stack-swap toggles (``enable_stack_swap``, ``stack_restore_on_boot``)
to automated coverage across every seam that carries them:

  * the registry-derived ``POOL_SETTINGS_KEYS`` save-trigger set (both keys),
  * the registered config handlers (``CONFIG_HANDLERS``),
  * the SHARED state_builder seam (one ``build_pool_settings_snapshot`` used by both
    state paths) and the live values it emits for these two keys,
  * ``app.js``: the ``POOL_SETTINGS_MAP`` entries, the ``saveSettings`` localStorage
    mirrors, the ``stacks_result`` + ``warning`` WS cases, the ``stacks_new`` /
    ``stacks_delete`` / ``stacks_swap`` sends, and the New Session + Import modal ids,
  * ``index.html``: the sessions panel + New Session + Import modal + both toggle ids,
  * ``session_caption.py`` imported by BOTH ``api_server`` and ``snapshot`` (single source).

This is a static / string / registry wiring test — it proves the key is wired through
every seam so the UI value cannot be silently dropped or reverted. Behavioral coverage
lives in ``tests/test_stack_sessions_backend.py`` and ``tests/test_stack_api_e2e.py``.
"""

from pathlib import Path

# Two toggles and their DOM ids (DOM ids use kebab-case, keys use snake_case).
KEY_ENABLE = 'enable_stack_swap'
KEY_RESTORE = 'stack_restore_on_boot'
ENABLE_DOM_ID = 'setting-enable-stack-swap'
RESTORE_DOM_ID = 'setting-stack-restore-on-boot'

# Repo-root-anchored paths so the test is independent of the invocation CWD
# (the reference file relies on CWD == repo root; anchoring is strictly safer).
REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / 'web_ui' / 'app.js'
INDEX_HTML = REPO_ROOT / 'web_ui' / 'index.html'
API_SERVER = REPO_ROOT / 'agent_cascade' / 'api_server.py'
SNAPSHOT_PY = REPO_ROOT / 'agent_cascade' / 'snapshot.py'
SESSION_CAPTION = REPO_ROOT / 'agent_cascade' / 'session_caption.py'


class _StubPool:
    """Minimal pool surface ``build_pool_settings_snapshot`` needs for these two keys."""

    def __init__(self):
        from agent_cascade.agent_instance import PoolSettings
        self.settings = PoolSettings()
        self.llm_cfg = {}


class TestRegistrySeams:
    """Both toggles are wired through the save-trigger / handler / snapshot seams."""

    def test_in_extra_persist_keys(self):
        """Both toggles are ``kind='extra'`` registry keys, so they persist through
        EXTRA_PERSIST_KEYS (the save_trigger is POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS)."""
        from agent_cascade.config_handlers import EXTRA_PERSIST_KEYS

        assert KEY_ENABLE in EXTRA_PERSIST_KEYS, \
            f"{KEY_ENABLE} missing from EXTRA_PERSIST_KEYS (ws_handlers save trigger)"
        assert KEY_RESTORE in EXTRA_PERSIST_KEYS, \
            f"{KEY_RESTORE} missing from EXTRA_PERSIST_KEYS (ws_handlers save trigger)"

    def test_handlers_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert KEY_ENABLE in CONFIG_HANDLERS, f"missing handler for {KEY_ENABLE}"
        assert KEY_RESTORE in CONFIG_HANDLERS, f"missing handler for {KEY_RESTORE}"

    def test_emitted_by_shared_state_builder_snapshot(self, monkeypatch):
        """These keys are emitted live from settings.py (not PoolSettings), so both
        state_builder paths must carry them via the single shared snapshot seam. Assert
        the shared seam exists AND that both keys are really emitted, with live values."""
        src = (REPO_ROOT / 'agent_cascade' / 'api_integration_pkg' / 'state_builder.py'
               ).read_text(encoding='utf-8')
        assert src.count('pool_settings = build_pool_settings_snapshot(pool)') >= 2, \
            'both state_builder paths must share build_pool_settings_snapshot'

        from agent_cascade import settings
        from agent_cascade.api_integration_pkg.state_builder import build_pool_settings_snapshot

        pool = _StubPool()

        # Live values, both directions — proves the snapshot reads settings.py at call
        # time rather than a frozen default.
        monkeypatch.setattr(settings, 'ENABLE_STACK_SWAP', True)
        monkeypatch.setattr(settings, 'STACK_RESTORE_ON_BOOT', False)
        snap = build_pool_settings_snapshot(pool)
        assert snap[KEY_ENABLE] is True, f"{KEY_ENABLE} not emitted live (True case)"
        assert snap[KEY_RESTORE] is False, f"{KEY_RESTORE} not emitted live (False case)"

        monkeypatch.setattr(settings, 'ENABLE_STACK_SWAP', False)
        monkeypatch.setattr(settings, 'STACK_RESTORE_ON_BOOT', True)
        snap = build_pool_settings_snapshot(pool)
        assert snap[KEY_ENABLE] is False, f"{KEY_ENABLE} not emitted live (False case)"
        assert snap[KEY_RESTORE] is True, f"{KEY_RESTORE} not emitted live (True case)"


class TestFrontendSeams:
    """app.js references both keys in the real settings seams and the WS / send sites."""

    def test_pool_settings_map_entries(self):
        """POOL_SETTINGS_MAP must map each DOM element id to its snake_case key."""
        app_js = APP_JS.read_text(encoding='utf-8')
        assert f"id: '#{ENABLE_DOM_ID}', prop: 'checked', key: '{KEY_ENABLE}'" in app_js, \
            f"missing POOL_SETTINGS_MAP entry #{ENABLE_DOM_ID} -> {KEY_ENABLE}"
        assert f"id: '#{RESTORE_DOM_ID}', prop: 'checked', key: '{KEY_RESTORE}'" in app_js, \
            f"missing POOL_SETTINGS_MAP entry #{RESTORE_DOM_ID} -> {KEY_RESTORE}"

    def test_save_settings_persists_both(self):
        """saveSettings() mirrors both toggles into the localStorage snapshot object."""
        app_js = APP_JS.read_text(encoding='utf-8')
        assert f"s['{KEY_ENABLE}']" in app_js, f"{KEY_ENABLE}: missing saveSettings mirror"
        assert f"s['{KEY_RESTORE}']" in app_js, f"{KEY_RESTORE}: missing saveSettings mirror"

    def test_ws_result_and_warning_cases(self):
        """The WS dispatcher must handle the stacks_result and warning message types."""
        app_js = APP_JS.read_text(encoding='utf-8')
        assert "case 'stacks_result'" in app_js, 'missing stacks_result WS case'
        assert "case 'warning'" in app_js, 'missing warning WS case'

    def test_session_send_calls(self):
        """The sessions panel must send stacks_new / stacks_delete / stacks_swap."""
        app_js = APP_JS.read_text(encoding='utf-8')
        for msg in ('stacks_new', 'stacks_delete', 'stacks_swap'):
            assert f"type: '{msg}'" in app_js, f"missing send({{type: '{msg}'}}) call"

    def test_modal_ids_in_app_js(self):
        """app.js must drive the New Session and Import modal element ids."""
        app_js = APP_JS.read_text(encoding='utf-8')
        for sel in ('#new-session-modal', '#new-session-name', '#import-modal'):
            assert sel in app_js, f"missing modal selector {sel} in app.js"


class TestIndexHtmlSeam:
    """index.html carries every element id the sessions UI references."""

    def _ids_present(self):
        html = INDEX_HTML.read_text(encoding='utf-8')
        for dom_id in ('sessionsList', 'activeSessionCard', 'new-session-modal',
                       'import-modal', ENABLE_DOM_ID, RESTORE_DOM_ID):
            assert f'id="{dom_id}"' in html, f"missing id {dom_id} in index.html"

    def test_all_ids_present(self):
        self._ids_present()


class TestSessionCaptionSingleSource:
    """session_caption.py is the single source of truth: imported by BOTH surfaces."""

    def test_module_exists(self):
        assert SESSION_CAPTION.is_file(), 'agent_cascade/session_caption.py is missing'

    def test_imported_by_both_api_server_and_snapshot(self):
        api = API_SERVER.read_text(encoding='utf-8')
        snap = SNAPSHOT_PY.read_text(encoding='utf-8')
        assert 'from agent_cascade.session_caption import' in api, \
            'api_server.py does not import session_caption (caption logic would duplicate)'
        assert 'from agent_cascade.session_caption import' in snap, \
            'snapshot.py does not import session_caption (caption logic would duplicate)'

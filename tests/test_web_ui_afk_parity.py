"""Static drift guard: the browser's duplicate AFK logic must stay deleted.

The whole point of the AFK Telegram-parity fix is that ONE server-backed flag on
OperationManager owns BOTH behaviors (auto-reject + idle auto-reply). If any copy
of that logic creeps back into web_ui/app.js, WebUI and Telegram bridge will drift
again — exactly the bug class this change fixes.

This test greps the UI source for the deleted patterns and asserts the new
server-backed wiring is present. Cheap, durable, no browser needed.
"""

from pathlib import Path

import pytest

APP_JS = Path(__file__).parent.parent / 'web_ui' / 'app.js'


@pytest.fixture(scope='module')
def app_js_source() -> str:
    assert APP_JS.exists(), f'missing {APP_JS}'
    return APP_JS.read_text(encoding='utf-8')


class TestBrowserAfkDuplicatesGone:

    def test_no_check_afk_auto_reply_function(self, app_js_source):
        assert 'function checkAfkAutoReply' not in app_js_source

    def test_no_trigger_afk_send_function(self, app_js_source):
        assert 'function triggerAfkSend' not in app_js_source

    def test_no_automated_rejects_sent_from_browser(self, app_js_source):
        # The old renderApprovals loop sent {type:'reject', ..., automated:true}.
        assert 'automated: true' not in app_js_source

    def test_no_localstorage_afk_keys_written(self, app_js_source):
        # saveSettings() must no longer persist afk-enabled/afk-message to localStorage.
        assert "s['afk-enabled']" not in app_js_source
        assert "s['afk-message']" not in app_js_source

    def test_no_last_afk_timer_state(self, app_js_source):
        # The cooldown bookkeeping that lived in the browser is gone with it.
        assert 'lastAfkTime' not in app_js_source
        assert 'afkPendingTimer' not in app_js_source


class TestServerBackedWiringPresent:

    def test_toggle_sends_set_afk_over_ws(self, app_js_source):
        # The toggle change listener must push the flag to the server.
        assert "type: 'set_afk'" in app_js_source

    def test_settings_sync_reads_server_state(self, app_js_source):
        # syncPoolSettings() must read the authoritative values from state.
        assert 'ps.afk_enabled' in app_js_source
        assert 'ps.afk_message' in app_js_source

"""Static wiring guard for the BUG_0311 "Hint Usage" telemetry panel.

No browser: like tests/test_slot_queue_timeout_ui_wiring.py, this reads the frontend
source and asserts every seam the feature needs is present, so a later refactor that
renames an id or drops the fetch wiring fails loudly instead of silently rendering
nothing. Complements the behavioural tests in tests/test_api_endpoints.py and
tests/test_telemetry.py.

Covers:
  index.html  — Hint Usage table tbody id + the two new Session-Stats card ids.
  app.js      — updateTelemetryHintTable() + the fetchTelemetry wiring + the two card
                set() calls.
"""

from pathlib import Path


# Read relative to the repo root (the dir above this tests/ folder), so the guard
# works regardless of the pytest working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / 'web_ui' / 'app.js').read_text(encoding='utf-8')
INDEX_HTML = (REPO_ROOT / 'web_ui' / 'index.html').read_text(encoding='utf-8')


class TestHintUsageUiWiring:

    def test_html_has_hint_table_tbody(self):
        assert 'id="telem-hint-tbody"' in INDEX_HTML, \
            'index.html: Hint Usage table tbody id missing'

    def test_html_has_two_session_cards(self):
        assert 'id="telem-hints-posted"' in INDEX_HTML, \
            'index.html: telem-hints-posted card id missing'
        assert 'id="telem-hint-chars"' in INDEX_HTML, \
            'index.html: telem-hint-chars card id missing'

    def test_js_has_hint_table_renderer(self):
        assert 'function updateTelemetryHintTable(rows)' in APP_JS, \
            'app.js: updateTelemetryHintTable renderer missing'
        # the renderer must target the HTML tbody id (catches an id rename mismatch)
        assert "document.getElementById('telem-hint-tbody')" in APP_JS, \
            'app.js: renderer not wired to #telem-hint-tbody'

    def test_js_fetch_wires_hint_data(self):
        assert 'data.hints?.skill_table' in APP_JS and \
               'updateTelemetryHintTable(data.hints.skill_table)' in APP_JS, \
            'app.js: fetchTelemetry does not pass data.hints.skill_table to the renderer'

    def test_js_populates_the_two_cards(self):
        assert "set('telem-hints-posted'" in APP_JS, \
            'app.js: telem-hints-posted card never populated'
        assert "set('telem-hint-chars'" in APP_JS, \
            'app.js: telem-hint-chars card never populated'

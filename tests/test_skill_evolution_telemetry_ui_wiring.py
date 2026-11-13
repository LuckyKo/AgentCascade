"""Static wiring guard for the BUG_0312 "Skill Evolution" telemetry panel.

No browser: mirrors tests/test_hint_usage_ui_wiring.py — reads the frontend source and
asserts every seam the feature needs, so a later refactor that renames an id or drops the
fetch wiring fails loudly instead of silently rendering nothing. Complements the
behavioural API test (tests/test_api_endpoints.py::test_get_telemetry_includes_evolution).
"""

from pathlib import Path


# Read relative to the repo root (the dir above this tests/ folder), so the guard
# works regardless of the pytest working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / 'web_ui' / 'app.js').read_text(encoding='utf-8')
INDEX_HTML = (REPO_ROOT / 'web_ui' / 'index.html').read_text(encoding='utf-8')


class TestSkillEvolutionUiWiring:

    def test_html_has_evolution_table_tbody(self):
        assert 'id="telem-evo-tbody"' in INDEX_HTML, \
            'index.html: Skill Evolution table tbody id missing'

    def test_html_has_evolution_cards(self):
        assert 'id="telem-evo-accept"' in INDEX_HTML, \
            'index.html: telem-evo-accept card id missing'
        assert 'id="telem-evo-churn"' in INDEX_HTML, \
            'index.html: telem-evo-churn card id missing'

    def test_js_has_evolution_table_renderer(self):
        assert 'function updateTelemetryEvoTable(rows)' in APP_JS, \
            'app.js: updateTelemetryEvoTable renderer missing'
        # the renderer must target the HTML tbody id (catches an id rename mismatch)
        assert "document.getElementById('telem-evo-tbody')" in APP_JS, \
            'app.js: renderer not wired to #telem-evo-tbody'

    def test_js_fetch_wires_evolution_data(self):
        assert 'data.evolution?.table' in APP_JS and \
               'updateTelemetryEvoTable(data.evolution.table)' in APP_JS, \
            'app.js: fetchTelemetry does not pass data.evolution.table to the renderer'
        assert 'data.evolution?.aggregate' in APP_JS and \
               'updateTelemetryEvoCards(data.evolution.aggregate)' in APP_JS, \
            'app.js: fetchTelemetry does not pass data.evolution.aggregate to the cards'

    def test_js_populates_the_evolution_cards(self):
        assert "set('telem-evo-accept'" in APP_JS, \
            'app.js: telem-evo-accept card never populated'
        assert "set('telem-evo-churn'" in APP_JS, \
            'app.js: telem-evo-churn card never populated'

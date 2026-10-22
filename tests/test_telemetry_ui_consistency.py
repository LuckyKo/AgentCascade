"""Static guards for telemetry UI semantics that have no JS test harness.

BUG_0130: the Session Stats "Turns" card must agree with the Agent Class Usage
"Turns" column, and the "Wallclock Time" card must not be labelled as elapsed
wall-clock (it is LLM + non-delegation tool time). BUG_0126: the Config
Fingerprint and Agent Class Usage tables must show comparable LLM/Turn time
columns rather than two different quantities under the same "Total Time" label.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_APP_JS = _REPO / 'web_ui' / 'app.js'
_INDEX_HTML = _REPO / 'web_ui' / 'index.html'


def test_turns_card_uses_completed_turns():
    js = _APP_JS.read_text(encoding='utf-8')
    assert "set('telem-turns', formatNumber(telemetry.total_turns))" in js
    # The ambiguous user-turn fallback must be gone.
    assert 'total_user_turns ?? telemetry.total_turns' not in js


def test_config_table_shows_llm_time_and_turn_time():
    html = _INDEX_HTML.read_text(encoding='utf-8')
    assert 'LLM Time' in html
    assert 'Turn Time' in html
    # The old streaming-only "Total Time" header must be gone from both tables.
    assert '<th>Total Time</th>' not in html

    js = _APP_JS.read_text(encoding='utf-8')
    assert 'c.total_llm_latency_sec' in js
    assert 'c.total_duration_sec' in js
    assert 'r.llm_time_sec' in js


def test_config_label_does_not_advertise_sampling():
    """The fingerprint is model-only, so the row label must not show a T= value
    that only describes the first-seen turn (BUG_0131)."""
    js = _APP_JS.read_text(encoding='utf-8')
    assert 'T=${desc.temperature' not in js
    assert 'T=${desc.temperature ??' not in js


def test_wallclock_card_is_labelled_llm_plus_tool():
    html = _INDEX_HTML.read_text(encoding='utf-8')
    assert 'Wallclock Time' not in html
    assert 'LLM + Tool Time' in html

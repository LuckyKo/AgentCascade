"""Static test: assert that web_ui/app.js only WRITES to documented localStorage keys.

Phase 4 of the backend-authoritative settings refactor split presentation-only
(class D) keys into a dedicated `agent-cascade-ui-prefs` store. This test scans
the source of app.js for every `localStorage.setItem(...)` call and asserts the
key argument is in the documented allowlist. No server-backed setting may be
persisted to browser storage.

Run:  pytest tests/test_settings_localstorage_keys.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / 'web_ui' / 'app.js'

# ── Allowlist: the ONLY keys that may appear as the first arg to setItem ──
# Class D (ui-prefs store):
UI_PREFS_KEY = 'agent-cascade-ui-prefs'
# View-state / one-time sentinels:
VIEW_STATE_KEYS = frozenset({
    'side-panel-width',
    'sidebar-width',
    'agent-cascade-closed-tabs',
    'agent-cascade-tools-migrated-v2',
    'agent-cascade-agent-messages',
    '_settings_migrated_v1',
})

ALLOWED_WRITE_KEYS = frozenset({UI_PREFS_KEY}) | VIEW_STATE_KEYS

# Pattern: localStorage.setItem('key', ...) or localStorage.setItem("key", ...)
_SETITEM_RE = re.compile(
    r"""localStorage\.setItem\(\s*['"]([^'"]+)['"]""",
)


def _extract_setitem_keys(source: str) -> list[str]:
    """Return all literal key strings passed to localStorage.setItem in the source."""
    return [m.group(1) for m in _SETITEM_RE.finditer(source)]


@pytest.fixture(scope='module')
def app_js_source() -> str:
    return APP_JS.read_text(encoding='utf-8')


def test_only_allowed_localstorage_keys_are_written(app_js_source: str):
    """Every localStorage.setItem call in app.js must use a documented key."""
    written_keys = _extract_setitem_keys(app_js_source)
    # Deduplicate while preserving order for the assertion message.
    unique_keys = list(dict.fromkeys(written_keys))
    violations = [k for k in unique_keys if k not in ALLOWED_WRITE_KEYS]

    assert not violations, (
        'app.js writes to undocumented localStorage key(s): '
        f"{violations}. Allowed: {sorted(ALLOWED_WRITE_KEYS)}. "
        'Server-backed settings must NOT be persisted to browser storage.'
    )


def test_ui_prefs_key_is_the_only_class_d_store(app_js_source: str):
    """The ui-prefs key must be referenced (loadUiPrefs/saveUiPrefs exist)."""
    assert UI_PREFS_KEY in app_js_source, (
        f"Expected '{UI_PREFS_KEY}' to be defined in app.js"
    )


def test_no_server_setting_keys_in_localstorage_writes(app_js_source: str):
    """Spot-check: common server-backed keys must NOT appear as setItem args."""
    server_keys = frozenset({
        'agent-cascade-settings',   # legacy blob — read-only after Phase 4
        'qwen-settings',           # legacy fallback — read-only
        'agent-cascade-disabled-tools',  # server-authoritative since Batch B
        'qwen-disabled-tools',
    })
    written_keys = set(_extract_setitem_keys(app_js_source))
    overlap = written_keys & server_keys
    assert not overlap, (
        f"app.js still writes server-backed keys to localStorage: {sorted(overlap)}. "
        'These are now server-authoritative (Batch B/C).'
    )

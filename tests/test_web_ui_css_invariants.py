"""Regression pin: the WebUI has no per-frame CSS animation or backdrop blur.

An infinite CSS animation keeps the browser producing composited frames for as
long as the tab is focused. When the browser and the inference engine share the
display GPU (Windows/WDDM), that work steals time from token generation (~9%
measured on a small background window; more for a full viewed window).

A `filter` or non-`none` `box-shadow` on the animated element or any ancestor
re-rasterizes every frame, which is why the indicator was made static and its
glow moved to a static sibling. This test parses web_ui/styles.css and asserts:

  - no `animation: ... infinite` declaration anywhere,
  - `.sub-tab-pulse` does not reference an animation,
  - the persistent `.approval-bar` carries no `backdrop-filter`,
  - the dot and its ancestor carry no filter / non-none box-shadow.
"""

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).parent.parent / 'web_ui' / 'styles.css'

# A filter declaration, but not `backdrop-filter` (which may legitimately sit on a
# transient element such as a modal overlay).
_FILTER_RE = re.compile(r'(?<![A-Za-z-])filter\s*:')
# box-shadow declaration, capturing its value so `box-shadow: none` is allowed.
_SHADOW_RE = re.compile(r'box-shadow\s*:\s*([^;{}]*)')


def _strip_comments(css: str) -> str:
    """Remove /* ... */ blocks so words inside comments can't trip the checks."""
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


def _blocks(css: str) -> list[tuple[str, str]]:
    """Yield (selector, body) for every `{ ... }` block in css.

    Nested blocks (e.g. the steps inside `@keyframes`) are yielded too, and an
    `@keyframes` block's body includes its nested step blocks, so a flat scan of
    that body covers every stop.
    """
    out = []
    stack = []  # (selector, body_start_index)
    pending_start = 0
    for i, ch in enumerate(css):
        if ch == '{':
            stack.append((css[pending_start:i].strip(), i + 1))
            pending_start = i + 1
        elif ch == '}':
            if stack:
                sel, body_start = stack.pop()
                out.append((sel, css[body_start:i]))
            pending_start = i + 1
    return out


def _bad_properties(body: str) -> list[str]:
    """Return offending filter / non-none box-shadow declarations in `body`."""
    bad = []
    if _FILTER_RE.search(body):
        bad.append('filter')
    for m in _SHADOW_RE.finditer(body):
        if m.group(1).strip().lower() != 'none':
            bad.append(f'box-shadow: {m.group(1).strip()}')
    return bad


@pytest.fixture(scope='module')
def blocks() -> list[tuple[str, str]]:
    assert CSS_PATH.exists(), f'missing {CSS_PATH}'
    return _blocks(_strip_comments(CSS_PATH.read_text(encoding='utf-8')))


class TestIndicatorHasNoFilteredAncestor:

    def test_tab_icon_container_carries_no_filter_or_shadow(self, blocks):
        # `.tab-icon-container` is the dot's DIRECT PARENT. A filter or shadow here
        # would re-raster every frame if a dot animation is ever reintroduced.
        offenders = []
        for sel, body in blocks:
            if '.tab-icon-container' in sel:
                offenders.extend((sel, p) for p in _bad_properties(body))
        assert not offenders, f'.tab-icon-container (ancestor of .sub-tab-pulse) must not carry: {offenders}'

    def test_subtab_pulse_dot_carries_no_filter_or_shadow(self, blocks):
        # Keep the dot free of filter/shadow; the active-tab glow lives on the
        # static `.main-tab-icon` sibling instead.
        offenders = []
        for sel, body in blocks:
            if '.sub-tab-pulse' in sel and not sel.startswith('@keyframes'):
                offenders.extend((sel, p) for p in _bad_properties(body))
        assert not offenders, f'.sub-tab-pulse rules must not carry: {offenders}'


class TestNoContinuousAnimation:

    def test_no_infinite_animation_anywhere(self, blocks):
        # An infinite animation keeps the compositor producing frames while the tab
        # is focused; that is the steady GPU cost. Pausing it while idle is not
        # enough: it must not run *during generation*.
        offenders = [
            sel for sel, body in blocks
            if re.search(r'animation\s*:[^;{}]*\binfinite\b', body)
        ]
        assert not offenders, f'infinite CSS animation(s) reintroduced: {offenders}'

    def test_pulse_dot_is_static(self, blocks):
        rule = next((b for sel, b in blocks if sel == '.sub-tab-pulse'), None)
        assert rule is not None, '.sub-tab-pulse base rule missing'
        assert re.search(r'animation\s*:', rule) is None, \
            '.sub-tab-pulse must stay static (no animation)'

    def test_approval_bar_has_no_backdrop_filter(self, blocks):
        # This bar spans the full width and persists while approvals are pending; a
        # backdrop blur behind it re-rasters whenever the page composites a frame,
        # so it is a per-frame GPU cost during streaming.
        rules = [b for sel, b in blocks if sel == '.approval-bar']
        assert rules, '.approval-bar rule missing'
        for body in rules:
            assert 'backdrop-filter' not in body, \
                '.approval-bar must not carry backdrop-filter'

"""Regression pin: no filter/box-shadow on any ancestor of the pulsing tab dot.

The per-frame animated `.sub-tab-pulse` dot is a direct child of
`.tab-icon-container` (see web_ui/app.js: the tab icon is built as
`<span class="sub-tab-pulse"> ... <span class="main-tab-icon">...`). A CSS
`filter` (e.g. `drop-shadow`) or a non-`none` `box-shadow` placed on the dot
itself or on ANY ancestor of it is re-rasterized on every animation frame the
dot scales/opacity-animates. That per-frame filter re-raster is the GPU cost
this fix eliminated by relocating the active-tab glow onto the static
`.main-tab-icon` sibling.

This test parses web_ui/styles.css and asserts the invariant holds. It targets
the SPECIFIC rules that previously carried the filter/shadow (the direct parent
and the dot itself) rather than a blanket "no filter anywhere" check, so it is a
true pin on the regression vector and does not forbid unrelated, non-ancestor
shadows elsewhere in the sheet.
"""

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).parent.parent / 'web_ui' / 'styles.css'

# A filter declaration, but not `backdrop-filter` (which legitimately sits on
# non-animated elements such as the approval bar).
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


class TestAnimatedDotHasNoFilteredAncestor:

    def test_tab_icon_container_carries_no_filter_or_shadow(self, blocks):
        # `.tab-icon-container` is the dot's DIRECT PARENT: the original glow
        # lived here (`filter: drop-shadow(...)`) and was the regression source.
        offenders = []
        for sel, body in blocks:
            if '.tab-icon-container' in sel:
                offenders.extend((sel, p) for p in _bad_properties(body))
        assert not offenders, f'.tab-icon-container (ancestor of .sub-tab-pulse) must not carry: {offenders}'

    def test_subtab_pulse_dot_carries_no_filter_or_shadow(self, blocks):
        # The dot's own state rules must keep `box-shadow: none` (allowed) and
        # never add a `filter`.
        offenders = []
        for sel, body in blocks:
            if '.sub-tab-pulse' in sel and not sel.startswith('@keyframes'):
                offenders.extend((sel, p) for p in _bad_properties(body))
        assert not offenders, f'.sub-tab-pulse rules must not carry: {offenders}'

    def test_pulse_keyframes_have_no_filter_or_shadow(self, blocks):
        # The animated keyframe itself must animate opacity/transform only.
        offenders = []
        for sel, body in blocks:
            if sel.replace('-', ' ').split()[:1] == ['@keyframes'] and 'subtab-pulse' in sel:
                offenders.extend((sel, p) for p in _bad_properties(body))
        assert offenders == [], f'@keyframes subtab-pulse must not carry filter/box-shadow: {offenders}'


class TestPulseIndicatorStillPresent:

    def _keyframes_body(self, blocks):
        for sel, body in blocks:
            if sel.split()[:1] == ['@keyframes'] and 'subtab-pulse' in sel:
                return body
        return None

    def test_pulse_keyframes_still_defined(self, blocks):
        # Pin: the glow fix must not have deleted the animation.
        body = self._keyframes_body(blocks)
        assert body is not None, '@keyframes subtab-pulse was removed'
        assert re.search(r'opacity\s*:', body), 'subtab-pulse keyframes must still animate opacity'

    def test_dot_still_references_the_pulse_animation(self, blocks):
        rule = next((b for sel, b in blocks if sel == '.sub-tab-pulse'), None)
        assert rule is not None, '.sub-tab-pulse base rule missing'
        assert 'subtab-pulse' in rule, 'the dot must still reference the subtab-pulse animation'

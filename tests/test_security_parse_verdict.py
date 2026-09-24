"""Regression tests for SecurityAdvisorHandler._parse_verdict (todo.md:155).

The security advisor's approval/rejection *reason* was sometimes rendered in ALL CAPS.
Root cause: the "Fallback 2" branch of ``_parse_verdict`` (find the LAST [YES]/[NO]
anywhere in the text) sliced the justification from an ``.upper()``'d copy of the line,
so the reason arrived upper-cased. The primary path (Strategy 1) and the single-word
fallback already produced correct mixed case — which is why the bug was intermittent.

These tests are revert-proof: the two Fallback-2 caps cases FAIL against the pre-fix
behavior (ALL CAPS justification) and PASS after the fix (original-case justification).

``_parse_verdict`` only uses module-level regexes and a local logger import — no instance
state — so we build a bare instance with ``SecurityAdvisorHandler.__new__`` to avoid the
heavy constructor.
"""

from agent_cascade.security_handler import SecurityAdvisorHandler


def _handler() -> SecurityAdvisorHandler:
    """A bare handler exposing only what ``_parse_verdict`` needs (no __init__)."""
    return SecurityAdvisorHandler.__new__(SecurityAdvisorHandler)


def test_fallback2_no_mixed_case_preserved():
    """Load-bearing guard: Fallback 2 NO with a trailing non-verdict line.

    The last non-empty line is NOT a verdict, so Strategy 1 misses and Fallback 2 fires.
    Pre-fix this returned the justification in ALL CAPS; post-fix it keeps original case.
    """
    h = _handler()
    text = (
        'This operation modifies system files in-place.\n'
        '[NO] This operation modifies system files in-place\n'
        'Please confirm before proceeding.'
    )
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_no is True
    assert is_yes is False
    # Must be original case, NOT 'THIS OPERATION MODIFIES SYSTEM FILES IN-PLACE'
    assert justification == 'This operation modifies system files in-place'


def test_fallback2_yes_mixed_case_preserved():
    """Fallback 2 YES analogue: trailing non-verdict line after a [YES] line."""
    h = _handler()
    text = (
        'Looks safe to proceed.\n'
        '[YES] Looks safe to proceed\n'
        'No further concerns.'
    )
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_yes is True
    assert is_no is False
    # Must be original case, NOT 'LOOKS SAFE TO PROCEED'
    assert justification == 'Looks safe to proceed'


def test_strategy1_no_still_correct():
    """Strategy 1 (last line IS the verdict) was already correct — guard against regression."""
    h = _handler()
    text = 'I reviewed the command.\n[NO] Deletes critical files'
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_no is True
    assert is_yes is False
    assert justification == 'Deletes critical files'


def test_fallback2_case_insensitive_token_match_preserved():
    """Lowercase [no] in a Fallback-2 position is still detected as NO.

    Proves the fix kept case-insensitive verdict-token matching while fixing casing:
    the token match uses the upper-cased copy, but the justification keeps original case.
    """
    h = _handler()
    text = (
        'some preamble\n'
        '[no] some reason\n'
        'trailing note here'
    )
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_no is True
    assert is_yes is False
    # Original case preserved ('some reason', not 'SOME REASON')
    assert justification == 'some reason'


def test_prefix_stripping_still_works():
    """The 'Reason:' prefix strip must still apply to the Fallback-2 path."""
    h = _handler()
    text = (
        'pre line\n'
        '[NO] Reason: unsafe rm -rf\n'
        'trailing note'
    )
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_no is True
    assert is_yes is False
    # 'Reason:' stripped; remaining text keeps original case.
    assert justification == 'unsafe rm -rf'

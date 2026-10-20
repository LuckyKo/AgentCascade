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


def test_end_token_stripped_from_verdict():
    """[END] terminal token on the verdict line must be stripped from the justification."""
    h = _handler()
    text = '[YES] Reason: Safe operation. [END]'
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_yes is True
    assert is_no is False
    assert justification == 'Safe operation.'
    assert '[END]' not in justification


def test_end_token_case_insensitive():
    """Lowercase [end] is stripped just like [END]; verdict/justification unaffected."""
    h = _handler()
    text = '[no] reason: risky. [end]'
    is_yes, is_no, justification = h._parse_verdict(text)
    assert is_no is True
    assert is_yes is False
    assert justification == 'risky.'


# ── Early-break predicate (collection loop) ─────────────────────────────────

def test_signals_completion_on_committed_verdict():
    """A post-Phase-4 (non-streaming) yield whose text has verdict + [END] breaks the loop."""
    h = _handler()
    turn_output = [{'role': 'assistant', 'content': '[YES] Reason: Safe operation. [END]'}]
    assert h._signals_completion(turn_output, False) is True


def test_signals_completion_ignores_streaming_ticks():
    """Streaming yields carry UNCOMMITTED partial text; breaking there would lose the verdict.

    run() commits the assistant message only in Phase 4 (engine/core.py), which is
    skipped when the generator is abandoned mid-stream — so a streaming tick must
    never trip the break even when the partial text already shows verdict + [END].
    """
    h = _handler()
    partial = [{'role': 'assistant', 'content': '[YES] Reason: Safe. [END]'}]
    assert h._signals_completion(partial, True) is False


def test_signals_completion_not_tripped_by_tool_payload():
    """A tool-result payload containing the literal "[END]" must NOT break the loop.

    Backward compatibility: models that never emit the token still run to max_turns.
    """
    h = _handler()
    turn_output = [{'role': 'function', 'content': 'some file content ... [END] ...'}]
    assert h._signals_completion(turn_output, False) is False


def test_signals_completion_false_without_token():
    """No [END] at all — the loop runs to max_turns exactly as before."""
    h = _handler()
    turn_output = [{'role': 'assistant', 'content': '[YES] Reason: Safe operation.'}]
    assert h._signals_completion(turn_output, False) is False


def test_signals_completion_case_insensitive():
    """Lowercase [end] from the model is detected like [END]."""
    h = _handler()
    turn_output = [{'role': 'assistant', 'content': '[NO] risky. [end]'}]
    assert h._signals_completion(turn_output, False) is True


def test_signals_completion_requires_same_line_pairing():
    """A provisional [YES] in one message must not pair with a literal [END] in another.

    Realistic trigger: the security prompt itself contains the literal "[END]", so a
    grep/read_file tool result can carry it while the model has not decided yet.
    Also covers a [YES] on one line and [END] on the next within one message.
    """
    h = _handler()
    cross_message = [
        {'role': 'assistant', 'content': 'Maybe [YES] this is fine, let me check the file.'},
        {'role': 'function', 'content': 'prompt text: append [END] on the same line'},
    ]
    assert h._signals_completion(cross_message, False) is False

    same_msg_different_lines = [{'role': 'assistant', 'content': '[YES] Reason: ok.\n[END]'}]
    assert h._signals_completion(same_msg_different_lines, False) is False


def test_signals_completion_ignores_tool_result_log_echo():
    """A FUNCTION payload holding a complete "[YES] ... [END]" line must NOT break the loop.

    The advisor is pointed at the supervisor's JSONL log precisely so it can read past
    checks; JSONL escapes newlines, so a previous verdict arrives as one physical line
    that satisfies the same-line shape. Only assistant-authored text may trip the break.
    """
    h = _handler()
    log_echo = [{'role': 'function', 'content': '{"role": "assistant", "content": "[YES] Reason: Safe operation. [END]"}'}]
    assert h._signals_completion(log_echo, False) is False

    # The assistant verdict itself still trips it, in the same payload.
    both = [
        {'role': 'function', 'content': '[YES] Reason: old check approved. [END]'},
        {'role': 'assistant', 'content': '[NO] Reason: deletes a critical file. [END]'},
    ]
    assert h._signals_completion(both, False) is True

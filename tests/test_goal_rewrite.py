"""Tests for the /goal → supervisor-task-prefix rewrite (todo.md:160).

The rewrite lives in ``agent_cascade/content_parse._rewrite_goal_command`` and is
applied at the top of ``_parse_multimodal_content`` — the single choke point all
user messages pass through (WS ``handle_message``, WS ``handle_inject``, and REST
``/api/message``, the latter also serving the Telegram bridge). After the rewrite,
a user's ``/goal <text>`` message starts with ``Context: This is a message from
User:``, which ``is_supervisor_task_message`` in compression/helpers.py recognizes
— so the new goal is preserved verbatim in the ``<last_supervisor_task>`` block of
compression markers with no further logic.

Covers:
- _rewrite_goal_command unit behavior (exact word match, case-insensitivity,
  whitespace handling, non-matching input byte-identical).
- End-to-end through _parse_multimodal_content (plain text and multimodal image
  payloads — the rewrite must survive the parts-splitting path too).
- The compression contract: rewritten messages are classified as supervisor tasks
  and extracted by extract_last_supervisor_task.
- Prefix sync guard: the rewritten prefix is derived from helpers._TASK_PREFIX, so
  the two halves of the feature can never drift apart.
"""

import pytest

from agent_cascade.compression.helpers import (
    _TASK_PREFIX,
    extract_last_supervisor_task,
    is_supervisor_task_message,
)
from agent_cascade.content_parse import _parse_multimodal_content, _rewrite_goal_command
from agent_cascade.llm.schema import USER, Message


def _make_msg(role, content):
    return Message(role=role, content=content)


# ────────────────────────────────────────────────────────────────────────────
# 1. _rewrite_goal_command — unit behavior
# ────────────────────────────────────────────────────────────────────────────


class TestRewriteGoalCommand:
    def test_basic_rewrite(self):
        assert _rewrite_goal_command('/goal fix the parser') == \
            'Context: This is a message from User:\nfix the parser'

    def test_case_insensitive(self):
        assert _rewrite_goal_command('/GOAL fix the parser') == \
            'Context: This is a message from User:\nfix the parser'
        assert _rewrite_goal_command('/Goal fix the parser') == \
            'Context: This is a message from User:\nfix the parser'

    def test_leading_whitespace_tolerated(self):
        # The WS/REST paths .strip() before calling, but be defensive anyway.
        assert _rewrite_goal_command('  /goal fix the parser') == \
            'Context: This is a message from User:\nfix the parser'

    def test_extra_internal_whitespace_collapse_of_rest_is_not_done(self):
        # Only the boundary around the command word is normalized; the task text
        # itself is kept verbatim (single space after /goal, internal spacing intact).
        assert _rewrite_goal_command('/goal  fix   the parser') == \
            'Context: This is a message from User:\nfix   the parser'

    def test_multiline_goal_text_preserved(self):
        text = '/goal fix the parser\nand then run the tests'
        assert _rewrite_goal_command(text) == \
            'Context: This is a message from User:\nfix the parser\nand then run the tests'

    def test_bare_goal_with_no_text(self):
        # Degenerate but must not crash or mangle: empty task text.
        assert _rewrite_goal_command('/goal') == \
            'Context: This is a message from User:\n'

    def test_goal_is_not_a_prefix_match_for_longer_words(self):
        assert _rewrite_goal_command('/goals fix the parser') == '/goals fix the parser'
        assert _rewrite_goal_command('/goalx fix the parser') == '/goalx fix the parser'

    def test_mid_message_goal_not_rewritten(self):
        text = 'please /goal fix the parser'
        assert _rewrite_goal_command(text) == text

    def test_non_matching_text_byte_identical(self):
        for text in ('hello', 'Please refactor the parser module.', '/compress 0.5'):
            assert _rewrite_goal_command(text) == text

    def test_non_string_input_returned_unchanged(self):
        # Defensive: multimodal payloads are lists; never crash on them here.
        parts = [{'text': '/goal fix'}, {'image': '/tmp/x.png'}]
        assert _rewrite_goal_command(parts) is parts


# ────────────────────────────────────────────────────────────────────────────
# 2. End-to-end through _parse_multimodal_content (the real choke point)
# ────────────────────────────────────────────────────────────────────────────


class TestRewriteThroughParseMultimodal:
    def test_plain_goal_message(self):
        result = _parse_multimodal_content('/goal fix the parser')
        assert result == 'Context: This is a message from User:\nfix the parser'

    def test_plain_non_goal_message_unchanged(self):
        result = _parse_multimodal_content('hello there')
        assert result == 'hello there'

    def test_goal_with_image_becomes_parts_and_text_is_rewritten(self):
        # A data-URI image is saved to media storage; the text part must carry
        # the rewritten prefix (rewrite happens BEFORE parts splitting).
        data_uri = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA'
                    'fFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
        result = _parse_multimodal_content(f'/goal fix the parser\n![shot]({data_uri})')
        assert isinstance(result, list)
        text_parts = [p for p in result if 'text' in p]
        assert text_parts, f'expected a text part, got: {result!r}'
        # The first text part is the rewritten goal line (the "[NOTE: Saved as...]"
        # note is a separate text part appended after the image).
        assert text_parts[0]['text'].startswith(
            'Context: This is a message from User:\nfix the parser')

    def test_non_goal_with_image_unchanged_semantics(self):
        data_uri = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA'
                    'fFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
        result = _parse_multimodal_content(f'look at this\n![shot]({data_uri})')
        assert isinstance(result, list)
        text_parts = [p for p in result if 'text' in p]
        assert text_parts[0]['text'].startswith('look at this')


# ────────────────────────────────────────────────────────────────────────────
# 3. Compression contract — rewritten goals are preserved as supervisor tasks
# ────────────────────────────────────────────────────────────────────────────


class TestCompressionContract:
    def test_rewritten_goal_is_a_supervisor_task_message(self):
        msg = _make_msg(USER, _rewrite_goal_command('/goal fix the parser'))
        assert is_supervisor_task_message(msg) is True

    def test_extract_last_supervisor_task_finds_rewritten_goal(self):
        msgs = [
            _make_msg(USER, 'Context: This is a message from User:\nold goal'),
            _make_msg(USER, 'some plain chatter that should not match'),
            _make_msg(USER, _rewrite_goal_command('/goal fix the parser')),
        ]
        assert extract_last_supervisor_task(msgs) == \
            'Context: This is a message from User:\nfix the parser'

    def test_newer_plain_message_does_not_hide_the_goal(self):
        # The goal is preserved only if it is the LAST qualifying task in the
        # discarded window — plain chatter after it does not displace it.
        msgs = [
            _make_msg(USER, _rewrite_goal_command('/goal fix the parser')),
            _make_msg(USER, 'just some follow-up chatter'),
        ]
        assert extract_last_supervisor_task(msgs) == \
            'Context: This is a message from User:\nfix the parser'

    def test_unrewritten_plain_message_is_not_a_task(self):
        # Regression guard: without the rewrite, plain user text is NOT preserved.
        msg = _make_msg(USER, 'fix the parser')
        assert is_supervisor_task_message(msg) is False

    def test_rewritten_prefix_stays_in_sync_with_helpers_task_prefix(self):
        # The rewrite derives its prefix from helpers._TASK_PREFIX (single source
        # of truth). Guard: whatever that constant becomes, a rewritten /goal must
        # still be recognized as a supervisor task message — no silent drift.
        msg = _make_msg(USER, _rewrite_goal_command('/goal fix the parser'))
        assert str(msg.content).startswith(_TASK_PREFIX)
        assert is_supervisor_task_message(msg) is True

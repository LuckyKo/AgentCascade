"""Regression tests: sub-agent turn-end "broken chunks" (duplicate last message).

Root cause (verified, see N:\\work\\WD\\AgentWorkspace\\broken_chunks_rootcause_and_fix.md):
core.py Phase 4 commits the final assistant message to ``conversation`` and then clears
``_streaming_responses`` NON-atomically. In that window a concurrent serialization can see
BOTH the committed FINAL message AND a STALE in-flight partial whose (content, reasoning) are
PREFIXES of the final. The exact-fingerprint dedup misses it (the stale partial is shorter), so
it was appended as a near-duplicate second assistant message -> duplicated/broken chunks on the
frontend.

Fix under test (primary): ``_serialize_instance`` now ALSO skips a streaming partial when an
already-serialized assistant message already CONTAINS it (both content AND reasoning are
prefixes) -- the "subsumed by an already-committed message" case.

The SAFETY requirement is the crux: this guard must NEVER suppress legitimate streaming growth.
During normal active streaming the current turn's in-flight message is NOT yet committed, so no
serialized message contains it -> it must still be appended. ``test_normal_growth_not_suppressed``
proves that directly.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

import agent_cascade.settings as settings
# The commit-race duplication only manifests in delta mode (the tail-cut path). Force it on so
# the test exercises the same code path as production regardless of env config.
try:
    settings.STREAM_DELTA_ENABLED = True
except Exception:
    pass

from agent_cascade.llm.schema import Message, USER, ASSISTANT
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.api_integration_pkg.state_builder import _serialize_instance


class FakePool:
    """Minimal pool stub -- mirrors the repro probe (N:\\work\\WD\\AgentWorkspace\\_probe_delta_contract.py)."""

    def is_instance_halted(self, n):
        return False

    def has_messages(self, n):
        return False

    def get_queue_messages(self, n):
        return []

    def slice_history_for_llm(self, msgs):
        return msgs


def _make_instance(conversation):
    now = time.monotonic()
    return AgentInstance(
        instance_name='stale-sub',
        agent_class='researcher',
        conversation=conversation,
        max_turns=None,
        parent_instance=None,
        created_at=now,
        last_activity=now,
        compression_summary=None,
        latest_marker_index=-1,
    )


def _assistant_msgs(frame):
    return [m for m in frame.get('messages', []) if m.get('role') == ASSISTANT]


# ── 1. The bug: stale prefix partial must NOT be appended ────────────────────
def test_stale_prefix_partial_not_appended():
    """Commit-race window: committed final + a STALE prefix partial in _streaming_responses.

    The frame must contain exactly ONE assistant message (the committed final). The stale
    partial (a shorter prefix of the final) is subsumed by it and must be dropped.
    """
    # Non-trivial history so the delta tail-cut is active (original_history_count > 0).
    conv = [
        Message(role=USER, content='initial prompt ' + ('x' * 60)),
        Message(role=ASSISTANT, content='earlier answer ' + ('y' * 40),
                reasoning_content='earlier reasoning ' + ('z' * 30)),
    ]
    final_reasoning = 'full final reasoning block ' + ('R' * 200)
    final_content = 'full final answer text ' + ('A' * 120)
    conv.append(Message(role=ASSISTANT, content=final_content,
                        reasoning_content=final_reasoning))

    inst = _make_instance(conv)
    # The STALE partial: strict prefixes of the final -> different fingerprint, near-identical look.
    stale = Message(role=ASSISTANT,
                    content=final_content[:60],
                    reasoning_content=final_reasoning[:80])
    inst._streaming_responses = [stale]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))

    assert frame.get('is_partial') is True, 'expected a partial (streaming) frame'
    assist = _assistant_msgs(frame)
    # The stale prefix must NOT be appended as a second assistant message.
    assert len(assist) == 1, (
        f"BUG: expected exactly 1 assistant message, got {len(assist)}: "
        + ', '.join(f"content_len={len(m.get('content') or '')}" for m in assist)
    )
    # The one assistant message is the committed FINAL (full content), not the stale prefix.
    assert len(assist[0].get('content') or '') == len(final_content)


# ── 2. SAFETY: legitimate streaming growth must NOT be suppressed ─────────────
def test_normal_growth_not_suppressed():
    """During normal active streaming the current turn's partial is NOT yet committed, so no
    serialized message contains it -> it MUST still be appended (growth works).

    This is the critical safety assertion: a genuinely growing in-flight partial must never be
    "subsumed" by an earlier serialized message of the same turn, because that committed version
    does not exist yet.
    """
    conv = [
        Message(role=USER, content='initial prompt ' + ('x' * 60)),
        Message(role=ASSISTANT, content='earlier answer ' + ('y' * 40),
                reasoning_content='earlier reasoning ' + ('z' * 30)),
    ]
    inst = _make_instance(conv)

    # A FRESH in-flight partial for the CURRENT turn. Its content is NOT a prefix of (and is not
    # contained by) any committed message -- it's brand new text still being generated.
    fresh = Message(role=ASSISTANT,
                    content='brand new answer being streamed ' + ('B' * 90),
                    reasoning_content='fresh reasoning being streamed ' + ('Q' * 70))
    inst._streaming_responses = [fresh]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))

    assert frame.get('is_partial') is True, 'expected a partial (streaming) frame'
    assist = _assistant_msgs(frame)
    # The fresh in-flight partial MUST be appended alongside the committed earlier answer.
    assert len(assist) == 2, (
        f"SAFETY VIOLATION: expected the growing partial to be appended (2 assistant msgs), "
        f"got {len(assist)}: "
        + ', '.join(f"content_len={len(m.get('content') or '')}" for m in assist)
    )
    # The growing partial's full content is present verbatim.
    assert any((m.get('content') or '') == fresh.content for m in assist), (
        "the fresh in-flight partial's content was not appended -- growth was suppressed"
    )


# ── 3. Guard precision: prefix on only ONE field must NOT be treated as stale ──
def test_content_prefix_alone_not_suppressed():
    """The guard requires BOTH content-prefix AND reasoning-prefix (or both empty).

    A partial whose content is a prefix of the committed message but whose reasoning is NOT a
    prefix is a legitimately-different in-flight message and must still be appended. This guards
    against a false-positive that would suppress unrelated short strings.
    """
    conv = [
        Message(role=USER, content='initial prompt ' + ('x' * 60)),
        Message(role=ASSISTANT, content='full final answer text ' + ('A' * 120),
                reasoning_content='committed reasoning block ' + ('C' * 100)),
    ]
    inst = _make_instance(conv)

    # Content IS a prefix of the committed content, but reasoning is NOT (different text).
    partial = Message(role=ASSISTANT,
                      content='full final answer text ' + ('A' * 40),   # prefix of committed content
                      reasoning_content='completely different reasoning')  # NOT a prefix
    inst._streaming_responses = [partial]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))

    assist = _assistant_msgs(frame)
    assert len(assist) == 2, (
        f"false-positive suppression: expected 2 assistant msgs (reasoning differs), got {len(assist)}"
    )


# ── 4. Direct unit test of the guard helper ───────────────────────────────────
def test_is_stale_prefix_helper_unit():
    """Unit-level checks on _is_stale_prefix_of_serialized semantics."""
    from agent_cascade.api_integration_pkg.state_builder import _is_stale_prefix_of_serialized

    committed = [{'role': ASSISTANT, 'content': 'abcdef', 'reasoning_content': '123456'}]
    other_role = [{'role': USER, 'content': 'abcdef', 'reasoning_content': '123456'}]

    # Exact prefix on both -> stale.
    assert _is_stale_prefix_of_serialized('abc', '123', committed) is True
    # Equal on both -> stale (a partial equal to the committed msg).
    assert _is_stale_prefix_of_serialized('abcdef', '123456', committed) is True
    # Content prefix but reasoning NOT a prefix -> not stale.
    assert _is_stale_prefix_of_serialized('abc', '999', committed) is False
    # Reasoning prefix but content NOT a prefix -> not stale.
    assert _is_stale_prefix_of_serialized('zzz', '123', committed) is False
    # LAST-ONLY: an OLDER assistant that contains the partial is IGNORED because a NEWER one is
    # the last serialized assistant. The newer (last) message must itself contain the partial for
    # it to be stale. This is what prevents a fresh turn's partial from being suppressed by an
    # older committed answer.
    older_contains = [{'role': ASSISTANT, 'content': 'abcdef', 'reasoning_content': '123456'}]
    newer_differs = [{'role': ASSISTANT, 'content': 'totally different', 'reasoning_content': '999999'}]
    assert _is_stale_prefix_of_serialized('abc', '123', older_contains + newer_differs) is False

    # NO universal-prefix matching: an empty field on the partial side requires the committed
    # message to be EMPTY in that same field. A both-empty partial does NOT match a committed
    # message that has content/reasoning (the old "both-empty rule" was a false-positive hole).
    assert _is_stale_prefix_of_serialized('', '', committed) is False
    # Empty reasoning + content prefix of the last assistant, but the last assistant HAS
    # reasoning -> not stale (empty-field fix; the old code treated empty sr as a universal
    # prefix and wrongly matched here).
    assert _is_stale_prefix_of_serialized('abc', '', committed) is False
    # Empty content + reasoning prefix of the last assistant, but the last assistant HAS
    # content -> not stale (symmetric empty-field fix).
    assert _is_stale_prefix_of_serialized('', '123', committed) is False
    # Both fields empty AND both committed fields empty -> stale (the only legitimate
    # both-empty match under the no-universal-prefix rule).
    committed_empty = [{'role': ASSISTANT, 'content': '', 'reasoning_content': ''}]
    assert _is_stale_prefix_of_serialized('', '', committed_empty) is True
    # Only assistant messages are considered; a USER msg with the same text does not subsume.
    assert _is_stale_prefix_of_serialized('abc', '123', other_role) is False


# ── 5. SAFETY: a NEW turn's partial prefixing an OLDER answer must NOT be suppressed ────
def test_new_turn_partial_prefixing_older_answer_not_suppressed():
    """Guard against the reviewer-flagged over-broadening: Guard 2 compares against ALL
    serialized assistant messages, so in principle a new turn's partial that happens to be a
    prefix of an OLDER committed answer could be wrongly dropped.

    In practice this cannot happen for two independent reasons, both asserted here:
      (a) The guard requires BOTH content AND reasoning to be prefixes; a genuinely-new turn's
          in-flight message has different reasoning than the older answer, so it is never
          "subsumed" by it.
      (b) In delta mode the serialized tail only contains the LAST committed message, so an
          older answer is not even present to compare against.

    Either way the new partial must still be appended. This test locks in that safety property.
    """
    old_answer = 'The quick brown fox jumps over the lazy dog ' + ('OLD' * 50)
    conv = [
        Message(role=USER, content='prompt ' + ('x' * 60)),
        Message(role=ASSISTANT, content=old_answer,
                reasoning_content='turn1 reasoning ' + ('R' * 80)),
        Message(role=USER, content='followup question ' + ('q' * 40)),
    ]
    inst = _make_instance(conv)
    # New turn's in-flight partial: content is a prefix of the OLD answer, but its reasoning is
    # brand new (NOT a prefix of the old answer's reasoning).
    new_partial = Message(role=ASSISTANT,
                          content=old_answer[:50],
                          reasoning_content='brand new turn2 reasoning')
    inst._streaming_responses = [new_partial]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))
    assist = _assistant_msgs(frame)
    # The new partial MUST be appended (not suppressed by the older answer).
    assert any((m.get('content') or '') == new_partial.content for m in assist), (
        "SAFETY VIOLATION: a new turn's partial that prefixes an OLDER answer was wrongly "
        f"suppressed. assistant msgs = {[(m.get('index'), len(m.get('content') or '')) for m in assist]}"
    )


# ── 6. HARDENED: BOTH fields prefix an OLDER answer that is NOT even serialized -> NOT suppressed ──
def test_new_turn_both_fields_prefix_unserialized_older_answer_not_suppressed():
    """SAFETY under the hardened guard for the "older answer is the last committed" case.

    A new turn's in-flight partial whose BOTH content AND reasoning are strict prefixes of an
    OLDER committed answer, with a follow-up USER prompt after it (the current turn's input).
    This is exactly the shape of a real mid-conversation turn: [user, assistant(older), user].

    Two independent reasons the fresh partial must NOT be suppressed here:
      (a) TAIL_COMMITTED=1 delta tail-cut: during active streaming the last committed message is
          the current turn's USER prompt, so the serialized tail is just that user message -- the
          OLDER answer is not even in `serialized_msgs` to compare against. The helper therefore
          has no assistant candidate and returns False (no suppression).
      (b) Last-only guard: even if the older answer WERE the last serialized assistant, it is an
          OLDER turn's message, not a stale version of this fresh partial -- so comparing against
          it would be a false positive. (See test 8 for the non-last variant.)

    Net effect: legitimate in-turn growth is never suppressed by an older answer. This closes the
    reviewer's concern that "the last serialized assistant is an OLDER turn" could prefix-match a
    fresh partial -- with TAIL_COMMITTED=1 the older answer is simply not serialized during a
    streaming turn, and the core.py atomicity fix closes the commit-race on the main path.
    """
    old_answer = 'The quick brown fox jumps over the lazy dog ' + ('OLD' * 50)
    old_reasoning = 'turn1 reasoning block ' + ('R' * 80)
    conv = [
        Message(role=USER, content='prompt ' + ('x' * 60)),
        Message(role=ASSISTANT, content=old_answer, reasoning_content=old_reasoning),
        Message(role=USER, content='followup question ' + ('q' * 40)),
    ]
    inst = _make_instance(conv)
    # New turn's partial: BOTH fields are strict prefixes of the OLDER answer.
    new_partial = Message(role=ASSISTANT,
                          content=old_answer[:50],
                          reasoning_content=old_reasoning[:40])
    inst._streaming_responses = [new_partial]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))
    msgs = frame.get('messages', [])
    assist = _assistant_msgs(frame)
    # The older answer is NOT in the serialized tail (TAIL_COMMITTED=1 cut it off). This is why
    # the helper has no assistant candidate and cannot suppress the fresh partial.
    assert not any((m.get('content') or '') == old_answer for m in msgs), (
        f"expected the older answer to be cut from the delta tail, but it was serialized: "
        f"{[(m.get('index'), len(m.get('content') or '')) for m in msgs]}"
    )
    # The fresh partial MUST be appended (not suppressed by the unserialized older answer).
    assert any((m.get('content') or '') == new_partial.content for m in assist), (
        "SAFETY VIOLATION: a new turn's partial whose BOTH fields prefix an OLDER (unserialized) "
        f"answer was wrongly suppressed. assistant msgs = {[(m.get('index'), len(m.get('content') or '')) for m in assist]}"
    )


# ── 7. HARDENED (empty-field fix): empty reasoning + content prefix, last answer HAS reasoning ──
def test_empty_reasoning_prefix_of_answer_with_reasoning_not_suppressed():
    """SAFETY under the no-universal-prefix fix.

    A new turn with EMPTY reasoning whose content is a prefix of the LAST committed answer's
    content -- but that committed answer HAS non-empty reasoning. The old guard treated an empty
    stream_reasoning as a universal prefix (reasoning_ok = True unconditionally), so it wrongly
    "subsumed" this fresh partial and dropped legitimate growth. With the fix, the empty
    stream_reasoning requires the committed reasoning to ALSO be empty; since it is not, the guard
    must NOT suppress -> the partial is appended.
    """
    old_answer = 'The quick brown fox jumps over the lazy dog ' + ('OLD' * 50)
    conv = [
        Message(role=USER, content='prompt ' + ('x' * 60)),
        # Last committed assistant: HAS reasoning (non-empty).
        Message(role=ASSISTANT, content=old_answer,
                reasoning_content='committed reasoning block ' + ('C' * 80)),
        Message(role=USER, content='followup question ' + ('q' * 40)),
    ]
    inst = _make_instance(conv)
    # New turn's partial: EMPTY reasoning, content is a prefix of the last answer's content.
    new_partial = Message(role=ASSISTANT,
                          content=old_answer[:50],
                          reasoning_content='')  # empty reasoning
    inst._streaming_responses = [new_partial]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))
    assist = _assistant_msgs(frame)
    # The fresh partial MUST be appended (empty-field fix prevents false-positive suppression).
    assert any((m.get('content') or '') == new_partial.content for m in assist), (
        'SAFETY VIOLATION: a new turn with EMPTY reasoning whose content prefixes the last '
        "answer's content was wrongly suppressed by an answer that HAS reasoning. "
        f"assistant msgs = {[(m.get('index'), len(m.get('content') or '')) for m in assist]}"
    )


# ── 8. HARDENED (last-only): BOTH fields prefix an OLDER (non-last) assistant -> NOT suppressed ──
def test_new_turn_prefixing_older_non_last_assistant_not_suppressed():
    """SAFETY under the last-only fix (the direct regression the reviewer flagged).

    A new turn's partial whose BOTH content AND reasoning are prefixes of an OLDER committed
    answer, but a NEWER assistant message is serialized after it. The old all-messages guard
    would scan every assistant message and match the OLDER one -> false-positive suppression.
    With last-only, only the NEWER (last) assistant is compared; since it does not contain the
    partial, the fresh partial MUST be appended. This is the clean proof that comparing against
    an older non-last answer can no longer suppress legitimate growth.
    """
    old_answer = 'The quick brown fox jumps over the lazy dog ' + ('OLD' * 50)
    old_reasoning = 'turn1 reasoning block ' + ('R' * 80)
    conv = [
        Message(role=USER, content='prompt ' + ('x' * 60)),
        # OLDER answer: the partial will prefix THIS one.
        Message(role=ASSISTANT, content=old_answer, reasoning_content=old_reasoning),
        Message(role=USER, content='followup question ' + ('q' * 40)),
        # NEWER (last) assistant: does NOT contain the new partial's text.
        Message(role=ASSISTANT, content='brand new committed answer ' + ('N' * 60),
                reasoning_content='brand new committed reasoning ' + ('M' * 40)),
        Message(role=USER, content='another followup ' + ('w' * 30)),
    ]
    inst = _make_instance(conv)
    # New turn's partial: BOTH fields are prefixes of the OLDER answer (not the newer one).
    new_partial = Message(role=ASSISTANT,
                          content=old_answer[:50],
                          reasoning_content=old_reasoning[:40])
    inst._streaming_responses = [new_partial]

    frame = _serialize_instance(inst, FakePool(), include_messages=True, streaming=True,
                                streaming_responses=list(inst._streaming_responses))
    assist = _assistant_msgs(frame)
    # The fresh partial MUST be appended: last-only means the older non-last answer is ignored.
    assert any((m.get('content') or '') == new_partial.content for m in assist), (
        "SAFETY VIOLATION: a new turn's partial that prefixes an OLDER (non-last) committed "
        f"answer was wrongly suppressed by all-messages scanning. "
        f"assistant msgs = {[(m.get('index'), len(m.get('content') or '')) for m in assist]}"
    )

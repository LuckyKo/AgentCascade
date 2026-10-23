"""In-loop auto-skill trigger for the execution engine (Phase 6 split).

``AutoSkillMixin`` owns the reflection-extension trigger: the cheap pre-LLM
gates, the snapshot/qualification/injection path, the live extra-turn budget
read, and the budget-reset side effects. Method bodies are moved verbatim from
:mod:`agent_cascade.engine.core`; the module-level constants live here so tests
patch the true runtime consumers rather than the composition module.
"""

from __future__ import annotations

from agent_cascade.llm.schema import ASSISTANT
from agent_cascade.log import logger
from agent_cascade.settings import (AUTO_SKILL_ENABLED, AUTO_SKILL_EXTRA_TURNS, AUTO_SKILL_MIN_TURNS,
                                    DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE)
from agent_cascade.utils.utils import extract_text_from_message


def _extract_instance_output(*args, **kwargs):
    """Lazy wrapper around compression.helpers.extract_instance_output.

    Used only by the auto-skill in-loop trigger's snapshot fallback (§2.3 step 2).
    Imported inside the call so module import time is unchanged.
    """
    from agent_cascade.compression.helpers import extract_instance_output
    return extract_instance_output(*args, **kwargs)


class AutoSkillMixin:
    """In-loop auto-skill reflection trigger and its budget side effects."""

    def _auto_skill_gates_met(self, instance) -> bool:
        """Cheap, pre-LLM auto-skill trigger gates (settings + one-shot flag only).

        Returns True iff the in-loop reflection extension COULD fire this run. Used both to
        decide whether to skip last-turn tool-disable (prefix stability) and as the fast-path
        gate at the top of _try_auto_skill_extension, so the two can never drift.
        Deliberately excludes auto_skill_qualifies (skill-creator/prompt build) — that is a
        deeper qualification checked later and must not be re-run on every final turn.
        """
        skill_manager = getattr(self.pool, 'skill_manager', None)
        if skill_manager is None:
            return False
        settings = getattr(self.pool, 'settings', None)
        if not getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED):
            return False
        if getattr(settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) == LOAD_SKILL_NONE:
            return False
        min_turns = getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)
        if instance._current_turn <= min_turns:
            return False
        with instance._compression_lock:
            if getattr(instance, '_auto_skill_proposed', False):
                return False
        return True

    def _try_auto_skill_extension(self, instance, messages, llm_messages, loaded_skill_names=None) -> bool:
        """In-loop auto-skill trigger (replaces the post-run two-run helper).

        Called from run() at the NATURAL-COMPLETION point (Phase 5), i.e. right before
        the loop breaks when _post_turn_checks reports a natural end — an assistant turn
        with no tool call, no pending async work, and non-empty content.

        IMPORTANT: _post_turn_checks returns False via FOUR paths (terminal stop, terminal
        stop during the compression wait, pure-thinking stall, genuine completion) that all
        collapse into the same ``if not completed:`` in run(). The caller therefore gates this
        call on ``_is_genuine_completion(instance, response)`` so it is reached ONLY on genuine
        completion — a stopped or stalled agent never triggers skill reflection. This method
        itself does NOT re-check stop/stall; the gating is the caller's responsibility.

        When all gates pass it:
          1. snapshots the pre-reflection task output onto the instance — at natural
             end the final answer is already committed (per-iteration order: LLM →
             _process_response commits → _post_turn_checks), so this captures the
             just-completed final answer, not a previous turn's reply;
          2. injects the reflection prompt into BOTH instance.conversation
             (via _append_and_log) and the loop-local working sets (R3),
          3. sets the one-shot _auto_skill_proposed flag.

        Returns True on a successful trigger (caller then resets the loop budget);
        False otherwise. Best-effort: ANY exception is logged and returns False —
        a gate failure must never break the run.
        """
        try:
            # ── Gates (fast path, no locks) ────────────────────────────────
            # Same cheap gates as the final-turn tool-disable decision in run() — one
            # shared helper so the two can never drift.
            if not self._auto_skill_gates_met(instance):
                return False
            settings = getattr(self.pool, 'settings', None)
            skill_manager = self.pool.skill_manager
            # Live-read turn threshold (UI-editable, no restart). Shared by the gate above and
            # passed to auto_skill_qualifies so BOTH gates agree on the same live value.
            min_turns = getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)

            # ── Snapshot capture (under the compression lock) ────────────────
            # The one-shot _auto_skill_proposed flag was already checked in the
            # _auto_skill_gates_met fast-path above; it cannot change within this
            # synchronous call, so no re-check is needed here. The lock below is
            # still required for the instance state mutation (task_output).
            with instance._compression_lock:
                # Find the last ASSISTANT message with text content — same
                # dict/Message branching as the turn-limit notice in run() — and
                # extract its text. If none exists, fall back to the current
                # result on the pre-reflection conversation (may be a FUNCTION-role
                # warning or "no text output" string; that is the honest state of
                # this run). Never leave the attribute unset.
                snapshot = None
                for msg in reversed(instance.conversation):
                    msg_role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                    if msg_role != ASSISTANT:
                        continue
                    has_text = False
                    if isinstance(msg, dict):
                        content = msg.get('content', '')
                        if isinstance(content, list):
                            has_text = any(isinstance(item, dict) and item.get('type') == 'text' for item in content)
                        elif isinstance(content, str):
                            has_text = bool(content)
                    else:
                        content = getattr(msg, 'content', '')
                        if isinstance(content, list):
                            has_text = any(isinstance(item, dict) and item.get('type') == 'text' for item in content)
                        elif isinstance(content, str):
                            has_text = bool(content)
                    if has_text:
                        snapshot = extract_text_from_message(msg, add_upload_info=False)
                        break
                if snapshot is None:
                    snapshot = _extract_instance_output(list(instance.conversation),
                                                        instance.instance_name,
                                                        pool=self.pool)

                instance._auto_skill_task_output = snapshot

            # ── Qualification + prompt build (pure; no conversation/flag mutation) ─
            prompt = skill_manager.auto_skill_qualifies(instance,
                                                        instance._current_turn,
                                                        loaded_skill_names=loaded_skill_names,
                                                        min_turns=min_turns)
            if not prompt:
                return False

            # ── Inject via the existing path (R3: all targets, same object) ────
            # _append_and_log_to_llm appends to conversation + JSONL log and mirrors
            # into llm_messages exactly once (identity-checked against
            # _cached_llm_messages). `messages` (full working set) is a separate list
            # object here — run() builds it from a fresh copy in _setup_turn — so it
            # still needs its own append for the reflection prompt to reach both views.
            user_msg = self._make_user_message(prompt)
            with instance._compression_lock:
                self._append_and_log_to_llm(instance, user_msg, llm_messages, lock_held=True)
                messages.append(user_msg)

            with instance._compression_lock:
                instance._auto_skill_proposed = True

            logger.info('[AUTO-SKILL] In-loop trigger fired for %s (turns=%d)', instance.instance_name,
                        instance._current_turn)
            return True
        except Exception as e:
            logger.warning('[AUTO-SKILL] In-loop trigger failed for %s: %s', getattr(instance, 'instance_name', '?'), e)
            return False

    def _get_auto_skill_extra_turns(self) -> int:
        """Read the live auto-skill extra-turn budget with the module default."""
        return self._get_pool_setting('auto_skill_extra_turns', AUTO_SKILL_EXTRA_TURNS)

    def _grant_auto_skill_extension(self, instance) -> int:
        """Shared budget-reset side-effects for a FIRED auto-skill reflection extension.

        Called by BOTH trigger sites (the Phase-5 natural-completion path and the new
        Phase-4 last-turn-tool-call path) after _try_auto_skill_extension returns True.
        Snapshots the pre-trigger max_turns (R6 — restored in run()'s exit finally block)
        and extends instance.max_turns by AUTO_SKILL_EXTRA_TURNS from the
        current turn. Returns the new max_turns so the caller mirrors it into its loop-local
        `max_turns` and sets ``turns_available = AUTO_SKILL_EXTRA_TURNS`` +
        ``_suppress_budget_warnings = True`` (run() frame locals, as is the trailing
        ``yield response; continue``). Does NOT re-run trigger/qualification logic.
        """
        instance._auto_skill_orig_max_turns = instance.max_turns   # R6 snapshot
        extra_turns = self._get_auto_skill_extra_turns()
        new_max = instance._current_turn + extra_turns
        instance.max_turns = new_max
        return new_max

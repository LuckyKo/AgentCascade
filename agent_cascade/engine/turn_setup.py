"""Turn setup helpers for the execution engine (Phase 4 split).

``TurnSetupMixin`` owns turn-state restore, system-prompt refresh, working-set
cache management, and the setup-turn entry point. Method bodies are moved from
:mod:`agent_cascade.engine.core`; ``_setup_turn`` was split into the four
internal helpers below before the move.
"""

from __future__ import annotations

import re
from typing import List, Optional

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.helpers import (_build_resources_block, _build_session_metadata,
                                          _replace_resources_block, _replace_section)
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.log import logger
from agent_cascade.utils.utils import extract_text_from_message


class TurnSetupMixin:
    """Restore, refresh, and cache the per-turn working set."""

    def _restore_turn_state(self, instance: AgentInstance, inst_name: str) -> Optional[List[Message]]:
        """Restore held KV state when due, then snapshot the conversation.

        Returns the conversation copy, or None when the conversation is empty
        (the caller's documented early-exit path).
        """
        # Restore KV state ONLY when BOTH conditions hold:
        #   1. This instance currently HOLDS a concurrency slot (eviction safety - the
        #      FIFO guarantees single-holder for conc=0, so restoring while holding the
        #      slot can never evict another agent). We target the endpoint it actually
        #      holds, never the stale _last_endpoint_config (which may point at a shared
        #      conc=0 autoloader we no longer own; loading there would auto-evict a live
        #      sibling's resident model).
        #   2. A saved state label is pending (_state_label set) - i.e. an actual agent
        #      dispatch happened (sync or async) and a save ran, leaving a save file to
        #      consume. restore_instance_state() clears _state_label on success/failure,
        #      so it is set only between "save occurred" and "restore consumed it".
        #
        # The label gate is what stops us reloading on EVERY turn: without it, the slot
        # gate alone fires on every fresh user-message turn (run() re-acquires the slot
        # before _setup_turn), doing a 3.1GB disk read and - worse - a state/load that
        # destroys the warm KV cache, forcing an 85K-token full reprocess (~74s). On a
        # natural turn with no dispatch, _state_label is None -> we skip the restore and
        # the slot's KV cache stays warm. Any resolution failure also skips the restore
        # (a missed restore is far better than a wrongful eviction).
        with instance._state_lock:
            has_saved_state = instance._state_label is not None
        if instance._slot_release is None:
            logger.debug('[STATE_RESTORE_SKIP] %s holds no slot - skipping state restore', inst_name)
            # Defense-in-depth: a label set without a held slot cannot be restored this turn.
            # Clear it so it does not leak and re-trigger an expensive state/load next turn.
            self._clear_orphaned_state_label(instance, inst_name)
        elif not has_saved_state:
            logger.debug('[STATE_RESTORE_SKIP] %s has no saved state to restore (no dispatch this turn) - '
                         'keeping warm KV cache', inst_name)
        else:
            self._restore_held_slot_state(instance, inst_name)

        with instance._compression_lock:
            conv = list(instance.conversation)

        if not conv:
            logger.warning('empty conversation for %s - early exit', inst_name)
            return None
        return conv

    def _get_or_extend_working_set(self, instance: AgentInstance, inst_name: str) -> Optional[tuple]:
        """Return the cached working set when valid, extending it for new messages."""
        can_use_cache = (instance._last_config_version == self.pool._config_version and instance._cached_messages and
                         instance._cached_llm_messages)

        if can_use_cache:
            # Extend cached lists with any new messages appended since last
            # turn
            with instance._compression_lock:
                cached_len = len(instance._cached_messages)
                current_len = len(instance.conversation)

                # Fix 2: Cache sanity check - detect mismatches that would
                # cause silent rebuilds
                if cached_len != current_len:
                    if current_len > cached_len:
                        # Normal case: new messages were appended - extend the
                        # cache
                        logger.debug(f"[CACHE_EXTEND] Extending cached working set for {inst_name} "
                                     f"by {current_len - cached_len} message(s)")
                        new_messages = list(instance.conversation[cached_len:])
                        instance._cached_messages.extend(new_messages)
                        # Re-slice to ensure marker correctness after extension
                        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
                        instance._cached_llm_messages = list(sliced) if sliced else list(instance._cached_messages)
                    else:
                        # cached_len > current_len indicates a regression in
                        # Fix 1 - atomic updates should prevent this.
                        # Force rebuild to resync, log at INFO level for
                        # visibility (Fix 2 + Fix 3).
                        logger.info(f"[CACHE_MISMATCH] {inst_name}: conv={current_len}, cached={cached_len} "
                                    f"- forcing rebuild to resync")
                        can_use_cache = False

                if can_use_cache:
                    logger.debug(
                        f"[CACHE_HIT] Reusing cached messages={len(instance._cached_messages)}, llm_messages={len(instance._cached_llm_messages)}"
                    )
                    return instance._cached_messages, instance._cached_llm_messages, []
        return None

    def _refresh_system_prompt(self, instance: AgentInstance, inst_name: str,
                               conv: List[Message], template) -> None:
        """Refresh identity, session metadata, resources, and skills in conv[0]."""
        # P7: System prompt injection for ALL agents (not just root)
        # Inject identity, session metadata, available resources, and argument
        # reuse instructions
        if len(conv) > 0:
            m0 = conv[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')

            # If no system message at start, inject it from template
            if m0_role != SYSTEM and template and getattr(template, 'system_message', None):
                sys_msg = Message(role=SYSTEM, content=template.system_message)

                # Preserve old first message's timestamp so update_history()
                # can match it as an update
                # rather than appending a duplicate. The logger uses timestamps
                # as identity markers.
                if len(conv) > 0 and hasattr(conv[0], 'timestamp') and conv[0].timestamp:
                    try:
                        sys_msg.timestamp = conv[0].timestamp
                    except AttributeError:
                        pass

                instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync and clear
                m0 = sys_msg
                m0_role = SYSTEM

                # Note: No update_history() call here -
                # _log_messages_to_jsonl() already handles first-time logging.
                # Calling update_history() with fresh timestamp causes dedup to
                # fail and create duplicates.

            # Frozen-prompt guard (todo 134): read under _compression_lock at the
            # point of use. RLock reentrancy is safe - freeze_system_prompt() acquires
            # the same lock on this thread later in this turn, and no other thread can
            # flip the flag between this read and the write below (only this thread's
            # own freeze call at the end of _setup_turn sets it).
            with instance._compression_lock:
                sys_prompt_frozen = instance._system_prompt_frozen
            if m0_role == SYSTEM and not sys_prompt_frozen:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str):
                    original_content = m0_content
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are\s+\w+\."
                    if re.search(pattern, m0_content):
                        m0_content = re.sub(pattern, f"You are {inst_name}.", m0_content, count=1)

                    # 2. Refresh Session Metadata section.
                    # Always a refresh (never first-injection) because the pre-log writers handle
                    # persistence for every instance class. _replace_section's byte-identical early-out
                    # makes this a no-op when unchanged, preserving the KV-cache prefix. Do NOT re-add
                    # a first-injection branch here: todo.md:155.
                    meta_block = _build_session_metadata(self.pool, instance)
                    if meta_block:
                        m0_content = _replace_section(m0_content, '## Session Metadata', meta_block)

                    # 3. Inject/update available resources (agent types and
                    # cache pool note based on feature flags)
                    # Note: Using already-resolved template, no need for
                    # re-lookup
                    new_block = _build_resources_block(self.pool, template, instance)
                    if new_block:
                        if '## AVAILABLE AGENTS' in m0_content:
                            # Replace the existing block with fresh data
                            # (handles dynamic tool changes)
                            m0_content = _replace_resources_block(m0_content, new_block)
                        else:
                            # Legacy migration support only: strip the old "--- CURRENT AVAILABLE RESOURCES"
                            # block from restored sessions that predate the "## AVAILABLE AGENTS" format.
                            if '--- CURRENT AVAILABLE RESOURCES' in m0_content:
                                escaped_old = re.escape('--- CURRENT AVAILABLE RESOURCES')
                                old_pattern = escaped_old + r'.*?(?=\n\n(?:#{1,6}|---)|\Z)'
                                m0_content = re.sub(old_pattern, '', m0_content, count=1, flags=re.DOTALL)

                            # First injection - insert before ## Active Skills if present,
                            # otherwise append at end. This ensures consistent order:
                            # identity/metadata -> available agents -> active skills
                            if '## Active Skills' in m0_content:
                                # Insert before the Active Skills section
                                m0_content = m0_content.replace('## Active Skills', new_block + '\n\n## Active Skills',
                                                                1)
                            else:
                                # No skills section - append at end as fallback
                                m0_content += new_block

                    # Update the message ONLY if content actually changed
                    # (preserves LLM prefix caching)
                    if m0_content != original_content:
                        # DEBUG: Show exactly what changed (for KV cache troubleshooting)
                        diff_summary = []
                        if len(m0_content) != len(original_content):
                            diff_summary.append(f"len {len(original_content)}->{len(m0_content)}")
                        if m0_content[-5:] != original_content[-5:]:
                            diff_summary.append(f"tail_diff")
                        # Find first diff position
                        first_diff = None
                        for i, (a, b) in enumerate(zip(original_content, m0_content)):
                            if a != b:
                                first_diff = i
                                break
                        if first_diff is not None:
                            ctx_start = max(0, first_diff - 30)
                            ctx_end = min(first_diff + 30, len(original_content))
                            diff_summary.append(
                                f"first_diff@{first_diff}: orig='{original_content[ctx_start:ctx_end]}' new='{m0_content[ctx_start:ctx_end]}'"
                            )

                        if isinstance(m0, dict):
                            m0['content'] = m0_content
                        else:
                            m0.content = m0_content
                        logger.debug(
                            f"[CACHE_REBUILD] System prompt content CHANGED for {inst_name} ({', '.join(diff_summary)})"
                        )

                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            if log_inst.data['history']:
                                # Update in-memory system message only - do NOT rewrite the JSONL file here.
                                # data["history"] is the trimmed working set; rewriting the full-history file
                                # from it would destroy retained history. New text persists on next append.
                                log_inst.data['history'][0] = log_inst._format_message(m0)
                        except Exception as e:
                            logger.warning(f"Failed to update in-memory system message for {inst_name}: {e}")
                    else:
                        logger.debug(
                            f"[CACHE_REBUILD] System prompt for {inst_name} textually identical - skipping pool update")

    def _sync_turn_cache(self, instance: AgentInstance, conv: List[Message],
                         llm_messages: List[Message]) -> None:
        """Store the working set and config version, then freeze the prompt."""
        # Sync caches - simple extend-or-rebuild model
        instance._cached_messages = conv
        instance._cached_llm_messages = llm_messages
        instance._last_config_version = self.pool._config_version

        # Freeze the system prompt at the end of the first successful rebuild.
        # The M1 block above produced the final prompt; from now on conversation[0]
        # is immutable for this instance's lifetime (reset_conversation unfreezes).
        # freeze_system_prompt() is idempotent (no-op if already frozen or conv[0]
        # is not SYSTEM), so calling it unconditionally is safe and avoids a
        # lock-free flag read.
        instance.freeze_system_prompt()

    def _setup_turn(self, instance: AgentInstance) -> tuple:
        """Phase 1: Prepare messages and LLM input for the turn loop.

        Builds the system message from template (for main agent), loads conversation history,
        applies slice_history_for_llm to get working set, and sets up the response accumulator.

        Simple caching model: if config unchanged and cache exists, extend with new messages;
        otherwise rebuild from pool. The LLM API handles prefix caching automatically.

        Returns:
            Tuple of (messages, llm_messages, response), or (None, None, None)
            on error or when the conversation is empty (the early-exit path).
        """
        inst_name = instance.instance_name

        conv = self._restore_turn_state(instance, inst_name)
        if conv is None:
            return None, None, None

        cached = self._get_or_extend_working_set(instance, inst_name)
        if cached is not None:
            return cached

        # Cache miss or config change - rebuild from pool (Fix 3: promoted to
        # INFO for visibility)
        logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name} (conv_len={len(conv)})")

        # Load template to get system message if needed
        template = self.pool.get_template(instance.agent_class)

        self._refresh_system_prompt(instance, inst_name, conv, template)

        # messages = full working set; llm_messages = what actually goes to LLM
        # Apply slice to extract system + post-marker tail if markers exist
        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages = list(sliced) if sliced else list(conv)

        self._sync_turn_cache(instance, conv, llm_messages)

        response: List[Message] = []
        # logger.info(f"[SETUP_TURN] messages={len(conv)},
        # llm_messages={len(llm_messages)}, roles={[m.get('role') if
        # isinstance(m, dict) else getattr(m, 'role', '?') for m in
        # llm_messages]}")
        return conv, llm_messages, response

    def _clear_llm_preprocess_cache(self, instance: 'AgentInstance', inst_name: str) -> None:
        """Clear the LLM preprocessing cache for an instance's template.

        Extracted to eliminate duplicate cache-clearing blocks (M11 fix).
        Silently swallows errors — cache clearing is best-effort.
        """
        template = self.pool.get_template(instance.agent_class)
        if template and hasattr(template, 'llm') and template.llm:
            try:
                template.llm._clear_preprocess_cache()
            except Exception as e:
                logger.debug(f"Failed to clear LLM preprocess cache for {inst_name}: {e}")

    @staticmethod
    def _last_user_task_text(instance) -> Optional[str]:
        """Return the last USER-role message text in the instance conversation, or None.

        At run start this is the task the run is working on. It is the authoritative input
        for ``propose_skill``'s Tier-2 self-match gate (BUG_0123), because the proposal's own
        ``generated_from_task`` field is controlled by the model being screened.
        """
        conversation = getattr(instance, 'conversation', None)
        if not isinstance(conversation, (list, tuple)):
            return None
        # Engine-injected USER-role messages are not tasks: budget warnings, background/agent
        # results, shell result wrappers, compression markers and the reflection prompt.
        engine_prefixes = ('[SYSTEM WARNING:', '[SYSTEM]', '[BACKGROUND TOOL RESULT', '[Agent ',
                           '⟨shell_cmd', '--- CONTEXT COMPRESSED', '## Skill Reflection')
        for msg in reversed(conversation):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role != USER:
                continue
            try:
                text = extract_text_from_message(msg, add_upload_info=False)
            except Exception:  # pragma: no cover - defensive
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            if text.lstrip().startswith(engine_prefixes):
                continue
            return text
        return None

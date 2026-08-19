"""
SessionIOMixin — session loading from logs and instance state save/restore. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.settings import DEFAULT_WORKSPACE
from ..agent_instance import AgentInstance, PoolSettings, AgentState, ACTIVE_STATES
class SessionIOMixin:
    def _save_instance_state(self, names: set) -> dict:
        """Save state for a set of instance names before bulk dismissal.

        Returns a dict with keys: state, terminated, children, summaries,
        halted, compression_halted, conversations.
        """
        saved = {
            'state': {n: self.instance_state.get(n) for n in names},
            'terminated': {n for n in names if n in self.terminated_instances},
            'children': {n: self.children.get(n) for n in names},
            'summaries': {n: self.instance_summaries.get(n) for n in names},
            'halted': {n for n in names if n in self._halted_instances},
            'compression_halted': {n for n in names if n in self._compression_halted},
            'conversations': {},
        }
        for n in names:
            inst = self.instances.get(n)
            if inst:
                saved['conversations'][n] = list(inst.conversation)
        return saved

    def _restore_instance_state(self, saved: dict) -> None:
        """Restore previously saved instance state after _clear_all_state_dicts()."""
        # Restore pool-level state dicts
        for n, s in saved['state'].items():
            if s is not None:
                self.instance_state[n] = s
        self.terminated_instances.update(saved['terminated'])
        for n, c in saved['children'].items():
            if c is not None:
                self.children[n] = c
        for n, s in saved['summaries'].items():
            if s is not None:
                self.instance_summaries[n] = s
        self._halted_instances.update(saved['halted'])
        self._compression_halted.update(saved['compression_halted'])

        # Restore conversations using proper API for cache invalidation
        for n, conv in saved['conversations'].items():
            inst = self.instances.get(n)
            if inst is not None:
                if hasattr(self, '_instance_conversations'):
                    self._instance_conversations[n] = conv
                else:
                    inst.rebuild_conversation(conv)

        # Sync per-instance _child_instances with restored pool.children (atomic snapshot under lock)
        with self._children_lock:
            child_lists = {k: list(v) for k, v in self.children.items()}
        for n in saved['conversations']:
            inst = self.instances.get(n)
            if inst is not None and n in child_lists:
                try:
                    with inst._state_lock:
                        inst._child_instances = child_lists[n]
                except Exception as e:
                    logger.debug(f"Syncing _child_instances for {n} failed during session load (non-critical): {e}")

    def _dismiss_all_instances(self, exclude: Optional[set] = None):
        """Dismiss ALL instances from the pool, including root orchestrator(s).

        Unlike clear_sub_agents() which preserves roots, this wipes everything clean.
        Used internally by load_session_from_log() to prevent duplicate root tabs when
        loading a non-orchestrator session (the loaded agent is created as a root too).

        Mirrors the bulk dismissal pattern used in clear_sub_agents() and reset(),
        dismissing every instance via dismiss_instance() followed by full state dict clearing.
          1. Suppress dismissal callbacks (prevents premature broadcasts)
          2. Dismiss every instance via dismiss_instance() (handles cascade + logger close)
          3. Clear per-instance state dicts (instance_state, terminated_instances, children,
             halted_instances, compression_halted, instance_conversations)
          4. Increment _instances_version to signal the change
          5. Restore dismissal callbacks

        Thread safety: Suppresses dismissal callbacks; safe to call from any thread.

        Does NOT reset async infrastructure or performance caches — those are left alone
        so that the loaded session can reuse existing executors and idle checkers.

        Args:
            exclude: Optional set of instance names to skip during dismissal.
                     Excluded instances have their state saved, cleared, and restored.
        """
        if exclude is None:
            exclude = set()

        # Suppress callbacks during bulk cleanup (same pattern as clear_sub_agents)
        _callbacks = self._on_dismissed_callbacks.copy()
        self._on_dismissed_callbacks = []

        try:
            # Save state of excluded instances BEFORE the dismissal loop
            if exclude:
                saved = self._save_instance_state(exclude)

            for name in list(self.instances.keys()):
                if name in exclude:
                    continue
                self.dismiss_instance(name)

            # Clean up per-instance state dicts to prevent stale entries.
            if exclude:
                self._clear_all_state_dicts()
                self._restore_instance_state(saved)
            else:
                self._clear_all_state_dicts()

            # Clear pending approvals to unblock threads waiting for user input.
            # Mirrors the pattern in reset() / stop_session().
            if self.operation_manager:
                try:
                    with self.operation_manager._lock:
                        for approval in self.operation_manager.pending.values():
                            if not approval.event.is_set():
                                approval.approved = False
                                approval.outcome_reason = "All instances dismissed"
                                approval.event.set()
                        self.operation_manager.pending.clear()
                except Exception as e:
                    logger.warning(
                        f"clear_pending failed during _dismiss_all_instances (threads may hang): {e}"
                    )

            self._instances_version += 1
        finally:
            self._on_dismissed_callbacks = _callbacks

    def stop_session(self, release_slots: bool = True):
        """Minimal interrupt for "Stop" action — halts execution but preserves sessions.
        
        This method is used when the user clicks "Stop" to halt all streaming and put
        agents in IDLE state WITHOUT dismissing them (unlike reset() which dismisses
        sub-agents, setting them to TERMINATED).
        
        The key design principle: Stop is NON-DESTRUCTIVE. It should only interrupt
        execution — NOT clear conversations, summaries, or any user-visible session data.
        The user expects to be able to Resume exactly where they left off.
        
        Order of operations (MINOR-1 FIX - updated docstring):
          1. Set _stopped_event (to halt threads)
          2. Release concurrency slots for all active instances (NEW — prevents stuck API slots)
          3. Clear pending approvals (unblocks any threads waiting for user approval)
        
        Does NOT:
          - Dismiss sub-agents (they remain in pool with their current state)
          - Clear conversations (user expects to Resume from the same point)
          - Clear instance_summaries, terminated_instances, or any session data
          - Create a new logger session
          - Shutdown/recreate async infrastructure
        
        Args:
            release_slots: If True, immediately release concurrency slots for all instances.
                         This ensures API endpoints are freed even if execution threads
                         haven't noticed the stop signal yet. Default is True.
        
        See reset() for full session reset that dismisses sub-agents and clears everything.
        """
        # Step 1: Set stopped event to signal threads to halt (use property setter for side effects)
        self.stopped = True

        # Also clear pause flag so agents don't hang in pause wait loops
        self._paused.set()

        # ── Step 2: Release concurrency slots for all active instances ──────────────
        # This ensures API endpoints are freed immediately, even if execution threads
        # haven't noticed the stop signal yet. Prevents "stuck slot" issues where
        # agents transitioned to IDLE still hold their semaphores.
        # Uses instance._state_lock for thread safety against concurrent slot acquisition/release.
        released_count = 0
        held_count = 0
        if release_slots:
            try:
                for inst_name, instance in list(self.instances.items()):
                    with instance._state_lock:
                        # Atomic check-and-clear to prevent double-release with execution threads
                        if instance._slot_release is not None:
                            release_cb = instance._slot_release
                            instance._slot_release = None
                            instance._slot_key = None
                            try:
                                release_cb()
                                released_count += 1
                            except Exception as e:
                                logger.warning(f"[STOP_SLOT] Failed to release slot for '{inst_name}': {e}")
                        elif instance.state.name not in ('IDLE', 'TERMINATED'):
                            held_count += 1
            except Exception as e:
                logger.warning(f"slot_release failed during stop_session (non-critical): {e}")

        # ── Step 2.5: Cancel all queue tickets (FIFO scheduler cleanup) ─────────
        # Clean up any pending waiters to prevent blocked grants on resume.
        if hasattr(self, 'api_router') and self.api_router:
            try:
                cancelled = self.api_router.scheduler.cancel_all()
                if cancelled > 0:
                    logger.info(f"[STOP_SESSION] Cancelled {cancelled} pending queue ticket(s)")
            except Exception as e:
                logger.debug(f"Queue cancellation during stop_session (non-critical): {e}")

        # ── Step 3: Clear cached message sets, message queues, and async results ──
        # After stop, the cached working sets may be stale (from interrupted turns).
        # Clear them so the next turn rebuilds from current conversation state.
        # Also drain message queues and async results buffers to prevent stale data.
        cache_cleared = 0
        queue_cleared = 0
        async_cleared = 0
        try:
            for inst_name, instance in list(self.instances.items()):
                # Use compression lock for thread-safe cache clearing
                with instance._compression_lock:
                    if instance._cached_messages or instance._cached_llm_messages:
                        instance._cached_messages = []
                        instance._cached_llm_messages = []
                        instance._last_config_version = 0  # Force rebuild
                        cache_cleared += 1
                # Clear message queue for this instance
                with self._queue_lock:
                    if inst_name in self.message_queues:
                        try:
                            self.message_queues[inst_name].clear()
                            queue_cleared += 1
                        except Exception as e:
                            logger.debug(f"Queue clear failed for {inst_name}: {e}")
        except Exception as e:
            logger.debug(f"Cache clear during stop_session (non-critical): {e}")

        # ── Step 4: Clear pending approvals ────────────────────────────────────────
        # Prevent dangling threads waiting for user approval.
        approval_count = 0
        if self.operation_manager:
            try:
                with self.operation_manager._lock:
                    for approval in self.operation_manager.pending.values():
                        if not approval.event.is_set():
                            approval.approved = False
                            approval.outcome_reason = "Session stopped"
                            approval.event.set()
                            approval_count += 1
                    self.operation_manager.pending.clear()
            except Exception as e:
                logger.warning(f"clear_pending failed during stop_session (threads may hang): {e}")

        # ── Instrumentation: Report stop state ──────────────────────────────────────
        with self._execution._state_lock:
            stack_len = len(self._execution.active_stack)
        slot_info = ""
        if hasattr(self, 'api_router') and self.api_router:
            sched = self.api_router.scheduler
            status = sched.get_status()
            totals = {k: v['active_count'] for k, v in status.items() if v['active_count'] > 0}
            if totals:
                slot_info = f" active_slots={totals}"
        
        logger.info(
            f"Stop session done: released={released_count} slots, "
            f"cache_cleared={cache_cleared}, "
            f"queue_cleared={queue_cleared}, "
            f"async_cleared={async_cleared}, "
            f"active_instances={len(self.instances)}, "
            f"active_stack={stack_len}{slot_info}, "
            f"approvals_cleared={approval_count}"
        )
        if held_count > 0:
            logger.warning(f"[STOP_SESSION] {held_count} instance(s) were active but held no slot — possible slot leak")
    @staticmethod
    def _extract_last_session(messages: List[dict]) -> List[dict]:
        """Extract only the messages from the last session in a merged log.

        When server restarts and loads the same orchestrator, multiple sessions'
        messages can accumulate in a single log file. Each session starts with
        a system message. This method finds all system messages and returns
        only messages from the last session (after the last system message).

        If zero or one system message exists, returns all messages unchanged.

        Args:
            messages: List of message dicts (may contain messages from multiple sessions)

        Returns:
            List of message dicts from the last session only
        """
        # Find indices of all system messages (ROLE/SYSTEM imported at module level)
        sys_indices = [i for i, msg in enumerate(messages)
                       if msg.get(ROLE) == SYSTEM]

        if len(sys_indices) <= 1:
            # Normal case: single session, return all messages
            return messages

        # Multiple sessions detected: keep only messages from the last session
        last_sys_idx = sys_indices[-1]
        logger.debug(f"Session boundary detected: {len(sys_indices)} system messages found, "
                     f"keeping only last session (discarding {last_sys_idx} messages before index {last_sys_idx})")
        return messages[last_sys_idx:]

    def _parse_json_input(self, log_input: str) -> Tuple[List[dict], dict]:
        """Parse log input as file path, multi-line JSONL, or single JSON block.

        Tries each strategy in order and returns the first successful parse.
        Filters to only dict items (BOOL_LEAK guard). Extracts metadata entries.

        Session boundary detection: If multiple system messages are found
        (indicating merged sessions from server restarts), only messages from
        the LAST session (after the last system message) are returned.

        Returns:
            Tuple of (messages_list, metadata_dict)
        """
        messages = []
        metadata = {}

        # Resolve potential file path
        potential_path = Path(log_input)
        if not potential_path.is_absolute():
            ws = self._logger.workspace_dir if self._logger.workspace_dir else Path(DEFAULT_WORKSPACE)
            potential_path = ws / potential_path

        # --- Strategy 1: File path ---
        if potential_path.exists() and potential_path.is_file():
            try:
                with open(potential_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        item = self._parse_json_line(line.strip())
                        messages.extend(item["messages"])
                        metadata.update(item["metadata"])
                # Session boundary detection: keep only last session's messages
                messages = self._extract_last_session(messages)
                return messages, metadata
            except Exception as e:
                logger.debug(f"Error reading log file '{potential_path.name}': {e}")

        # --- Strategy 2: Multi-line JSONL (one JSON object per line) ---
        lines = [l.strip() for l in log_input.split('\n') if l.strip()]
        parse_messages, parse_metadata = [], {}
        single_line = len(lines) == 1
        for line in lines:
            item = self._parse_json_line(line)
            parse_messages.extend(item["messages"])
            parse_metadata.update(item["metadata"])
        # Session boundary detection: keep only last session's messages
        parse_messages = self._extract_last_session(parse_messages)
        if parse_messages or parse_metadata:
            return parse_messages, parse_metadata

        # --- Strategy 3: Single JSON block (array or object) ---
        if not single_line:
            return [], {}  # Already tried line-by-line above; don't double-try
        try:
            item = json.loads(log_input)
            if isinstance(item, list):
                filtered = [msg for msg in item if isinstance(msg, dict)]
                if len(filtered) != len(item):
                    logger.debug(f"_parse_json_input: filtered {len(item)-len(filtered)} non-dict items from JSON block")
                # Session boundary detection: keep only last session's messages
                return self._extract_last_session(filtered), {}
            elif isinstance(item, dict):
                if "history" in item:
                    history = item["history"]
                    if isinstance(history, list):
                        filtered = [msg for msg in history if isinstance(msg, dict)]
                        meta = {}
                        if "metadata" in item:
                            meta.update(item["metadata"])
                        # Session boundary detection: keep only last session's messages
                        return self._extract_last_session(filtered), meta
                    elif isinstance(history, dict):
                        return [history], {}
                else:
                    meta = {}
                    if "metadata" in item:
                        meta.update(item["metadata"])
                    return [item], meta
        except json.JSONDecodeError:
            pass

        return [], {}

    @staticmethod
    def _parse_json_line(line: str) -> dict:
        """Parse a single JSON line and return {'messages': [...], 'metadata': {...}}.

        Handles plain dicts, metadata wrappers, and inline lists.
        Filters to only dict items (BOOL_LEAK guard).
        """
        result = {"messages": [], "metadata": {}}
        if not line:
            return result
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return result

        if isinstance(item, dict):
            if "metadata" in item:
                # Metadata wrapper: extract the metadata payload and any messages
                if isinstance(item["metadata"], dict):
                    result["metadata"].update(item["metadata"])
                elif not item.get("event"):  # Skip event markers
                    result["messages"].append(item)
            elif "event" in item:
                pass  # Skip COMPRESSION/ROLLBACK event markers
            else:
                result["messages"].append(item)
        elif isinstance(item, list):
            filtered = [msg for msg in item if isinstance(msg, dict)]
            if len(filtered) != len(item):
                logger.debug(f"_parse_json_line: filtered {len(item)-len(filtered)} non-dict items from inline list")
            result["messages"].extend(filtered)

        return result

    def load_session_from_log(
        self,
        log_input: str,
        target_instance: Optional[str] = None,
        clear_sub_agents_before_load: bool = True,
        caller_name: Optional[str] = None,
    ) -> str:
        """Load session history from a JSONL log file.

        Simplified flow: clear sub-agents → read JSONL → extract last session boundary
        → filter valid messages → delete old instance → create fresh one → set up logger.

        Returns a status message string.

        Args:
            log_input: Path to the JSONL log file (or JSON string).
            target_instance: Name for the instance (default: from metadata or 'RecoveredSession').
            clear_sub_agents_before_load: If True, dismiss stale sub-agents first.
            caller_name: Optional instance name to preserve during dismissal (prevents
                the caller from being dismissed, which would cause UI tab loss).
        """
        # --- 1. Parse log early to determine target_instance name -------------
        log_input = log_input.strip()
        if not log_input:
            return "Error: Empty log input."

        messages, metadata = self._parse_json_input(log_input)
        if not messages:
            return "Error: No valid messages found in log input."

        # --- 2b. Determine instance name early for exclusion check ----------
        instance_name = self._resolve_instance_name(
            target_instance if target_instance is not None else metadata.get("instance_name") or "RecoveredSession"
        )

        # --- 3. Dismiss ALL instances (sub-agents + roots) -------------------
        if clear_sub_agents_before_load:
            # Exclude caller only if it exists and isn't the target being loaded
            if caller_name is not None and caller_name != instance_name and caller_name in self.instances:
                self._dismiss_all_instances(exclude={caller_name})
            else:
                self._dismiss_all_instances()

        # --- 4. Filter to valid conversation messages (skip events/metadata) --
        from agent_cascade.llm.schema import CONTENT as MSG_CONTENT
        cleaned = [
            msg for msg in messages
            if isinstance(msg, dict) and "event" not in msg
               and ROLE in msg and MSG_CONTENT in msg
        ]
        if not cleaned:
            return "Error: No valid conversation messages found."

        # --- 4b. Determine agent class (instance_name already determined above) -
        agent_class = (metadata.get("agent_class") or "Orchestrator").strip().lower()

        # --- 5. Build working set per design spec §5.2: [SYS][U0][COMP...][tail] -
        # Forward pass — find compression markers and extract summaries
        def _is_marker(msg):
            content = msg.get(MSG_CONTENT, '')
            return (msg.get(ROLE) == USER and isinstance(content, str)
                    and content.startswith(COMPRESSION_MARKER))

        markers = []
        last_marker_index = -1
        for i, msg in enumerate(cleaned):
            if _is_marker(msg):
                markers.append(msg)
                last_marker_index = i

        # Store latest summary in instance_summaries (for UI display) — only need last marker
        if markers:
            text = markers[-1].get(MSG_CONTENT, '')
            start_tag = '<context_summary>'
            end_tag   = '</context_summary>'
            s = text.find(start_tag) + len(start_tag)
            e = text.find(end_tag, s)
            if s > len(start_tag) - 1 and e > s:
                self.instance_summaries[instance_name] = text[s:e].strip()

        # Construct working set: [SYS][U0 first user msg][all markers][tail after last marker]
        system_msg = cleaned[0] if cleaned and cleaned[0].get(ROLE) == SYSTEM else None
        first_user = next((m for m in cleaned if m.get(ROLE) == USER and not _is_marker(m)), None)

        if markers:
            # Tail = messages after the last marker; markers are stacked in full
            tail = cleaned[last_marker_index + 1:]
            working_set = (
                ([system_msg] if system_msg else [])
                + ([first_user] if first_user else [])
                + markers          # all compression markers, including the last one
                + tail             # recent messages after the last marker
            )
        else:
            # No compression — full history is the working set
            working_set = cleaned

        # --- 6. Convert dicts -> Message objects (skip bad entries gracefully) -
        msg_objects = []
        for msg_dict in working_set:
            try:
                msg_objects.append(Message(**msg_dict))
            except Exception as e:
                logger.warning(f"Skipping malformed message on load: {e}")

        # --- 7. Delete old instance, create a fresh one --------------------
        normalized_agent_class = agent_class.strip().lower()
        key = (instance_name, normalized_agent_class)

        # Remove stale logger so the new one doesn't conflict
        with self._logger._lock:
            if key in self._logger._loggers:
                try:
                    self._logger._loggers[key].close()
                except Exception as e:
                    logger.warning(f"Logger close during load (non-critical): {e}")
            self._logger._loggers.pop(key, None)

        # Swap instance under state lock to prevent races with concurrent callers
        # (lifecycle_manager.py and ws_handlers.py can call this at runtime)
        with self._execution._state_lock:
            self.instances.pop(instance_name, None)

            now = time.monotonic()
            new_inst = AgentInstance(
                instance_name=instance_name,
                agent_class=agent_class,
                conversation=msg_objects,
                max_turns=None,
                parent_instance=None,
                created_at=now,
                last_activity=now,
                compression_summary=None,
                latest_marker_index=-1,
            )
            self.instances[instance_name] = new_inst
            self._instances_version += 1

        # --- 8. Set up logger pointing to the log file ---------------------
        try:
            from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger

            original_log_path = metadata.get("current_log_path")
            if original_log_path and Path(original_log_path).exists():
                new_log_path = AgentInstanceLogger.copy_session_file(
                    source_path=original_log_path,
                    log_dir=str(self._logger.log_dir),
                    agent_class=normalized_agent_class,
                    instance_name=instance_name,
                )
            else:
                new_log_path = None

            # Update metadata for the new session context
            updated_metadata = dict(metadata) if metadata else {}
            updated_metadata["current_log_path"] = new_log_path or original_log_path
            if new_log_path:
                updated_metadata["original_log_path"] = str(Path(original_log_path or "").name)

            log_inst = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(self._logger.log_dir),
                base_metadata=updated_metadata if updated_metadata else None,
                log_path=new_log_path,
            )
            # Rewrite log with cleaned (full history), not working_set.
            # Design §5.2: "Agent memory and JSONL are NOT in full sync — the logs retain
            # the full conversation history at all times." Only in-memory gets [SYS][U0][COMP][tail].
            log_inst.rewrite_log_with_history(cleaned)

            with self._logger._lock:
                self._logger._loggers[key] = log_inst
        except Exception as e:
            logger.warning(f"Logger setup after load (non-critical): {e}")

        # ── Skills System: Inject self-augmentation skill into loaded instance's system message ──
        # On restart, only inject self-augmentation (the default root skill).
        # Do NOT run AUTO matching against old conversation history — that would
        # match stale tasks from the previous session. AUTO matching is handled
        # by _create_and_run_agent when a new agent is spawned with a fresh task.
        try:
            from agent_cascade.execution_engine import _inject_self_augmentation_skill
            _inject_self_augmentation_skill(self, new_inst)
        except Exception as e:
            logger.warning(f"[SKILLS] Skill injection on load failed for {instance_name}: {e}")

        log_source = "file" if Path(log_input).exists() else "JSON input"
        return f"Loaded {len(msg_objects)} messages for '{instance_name}' ({agent_class}) from {log_source}."

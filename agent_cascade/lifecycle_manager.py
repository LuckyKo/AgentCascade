"""
Agent Lifecycle Manager — Phase 4.1 of the AgentCascade Architecture Rewrite.

Manages agent instance lifecycle: creation, reuse logic, settings propagation.
Extracted from ExecutionEngine to reduce God Object complexity.

See DESIGN_REWRITE.md §4.1 for design rationale.
"""

import copy
import datetime
import re
import time
from typing import TYPE_CHECKING, Optional, Sequence, Tuple

from agent_cascade.agent_instance import AgentInstance, AgentState, SystemPromptFrozenError
from agent_cascade.constants import NON_LLM_KEYS
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.exceptions import LogFileNotFoundError
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_MAX_TURNS, DEFAULT_TOKEN_BUDGET
from agent_cascade.budget_words import budget_allocation_notice
from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES

if TYPE_CHECKING:
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.execution_engine import ExecutionEngine


# ── L1 prefix-stability lint (BUG_0278) ──────────────────────────────────────
# llama.cpp's KV prefix cache only hits while the leading prompt prefix —
# conversation[0] — is byte-identical between runs. For "stable" system-agent
# classes (STABLE_SYSTEM_PROMPT_CLASSES) the RUNTIME gate in
# engine/helpers._build_session_metadata keeps the per-run identity fields
# (Supervisor name + log filename, own log path) out of conversation[0]. Nothing,
# however, asserted that a future edit cannot smuggle one back in.
#
# This is that guard. The patterns below are where the lint DEFINES the volatile
# fields; the producer that emits them (helpers._build_session_metadata) necessarily
# holds its own copies, so the two must stay in sync — the regression test asserts the
# literal labels, so a producer rename surfaces there rather than silently.
_VOLATILE_PREFIX_PATTERNS = (
    re.compile(r'Supervisor:'),           # caller identity + its timestamped log filename
    re.compile(r'Your log Path:'),        # per-run timestamped log path (…_%Y%m%d_%H%M%S.jsonl)
    re.compile(r'\d{4}-\d{2}-\d{2}'),     # wall-clock date (see _STABLE_PREFIX_CONSTANT_LINE)
)
# Line the runtime intentionally keeps in the stable block whose value is a PER-INSTANCE
# CONSTANT (captured once in AgentInstance.__post_init__, frozen with the prompt) — not
# per-run volatile: `- System: AgentCascade v… — YYYY-MM-DD HH:MM`. It matches the
# wall-clock pattern above but must not trip the guard. Stability is decided at the VALUE
# level (where the field is assigned), not the LINE level (it only looks like a timestamp).
_STABLE_PREFIX_CONSTANT_LINE = re.compile(r'^\s*-\s*System:\s*AgentCascade\b')


class PrefixStabilityError(RuntimeError):
    """Raised by _lint_stable_prefix when a volatile field renders into a stable-class
    conversation[0] Session Metadata section.

    Such a field busts the llama.cpp KV prefix cache on every run (the prefix stops
    matching from the first volatile token onward). The check itself raises hard so the
    build/CI regression test turns drift into a build failure; the runtime caller logs it
    non-fatally, because a cache miss is a perf regression that must not crash a live agent.
    """


def _extract_stable_metadata_lines(prefix_block: str) -> list:
    """Return the contiguous `- ` bullet lines of the `## Session Metadata` section.

    The volatile per-run fields are emitted by _build_session_metadata ONLY as
    `- Label: value` bullets, so the guard scans exactly those lines and nothing else.
    It stops at the first non-bullet line (the block trailer / the soul body), so static
    soul or skill prose is never scanned — no matter what a future soul edit contains.
    (An earlier heading-to-heading extraction did absorb soul prose and could false-positive
    on a date there, which would have hard-raised on a live stable-agent turn.)
    """
    lines = prefix_block.split('\n')
    start = next((i for i, line in enumerate(lines) if line.startswith('## Session Metadata')), None)
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if not line.startswith('- '):
            break
        out.append(line)
    return out


def _lint_stable_prefix(prefix_block: str, *, tool_names: Optional[Sequence[str]] = None) -> None:
    """Guard: assert a stable-class conversation[0] Session Metadata carries no volatile field.

    Callers gate this to STABLE_SYSTEM_PROMPT_CLASSES. Two independent checks:
      1. The `## Session Metadata` bullet run must contain no pattern from
         _VOLATILE_PREFIX_PATTERNS, except the per-instance-constant System line.
      2. If `tool_names` is supplied, they must be in canonical (sorted) order — a
         reordering of the tool schema is itself a KV prefix bust (the same bust class as
         a changed token; cf. the OpenAI prompt-cache bust table). The schema is assembled
         outside lifecycle_manager, so this check is a SPEC-GUARD for a future production
         wiring (canonical sorted-order check): no production caller supplies `tool_names`
         yet, and it is exercised only by synthetic lists in the regression test. Not wired
         to a caller now (YAGNI).

    Raises PrefixStabilityError on the first violation; otherwise returns None.
    """
    for line in _extract_stable_metadata_lines(prefix_block):
        if _STABLE_PREFIX_CONSTANT_LINE.match(line):
            continue
        for pattern in _VOLATILE_PREFIX_PATTERNS:
            if pattern.search(line):
                raise PrefixStabilityError(
                    f'stable-class conversation[0] Session Metadata contains a volatile '
                    f'field matching {pattern.pattern!r}: {line!r} — this busts the KV prefix cache')
    if tool_names is not None and list(tool_names) != sorted(tool_names):
        raise PrefixStabilityError(
            f'stable-class tool schema is not in canonical (sorted) order — a reordering '
            f'busts the KV prefix cache: {list(tool_names)!r}')


def _inject_metadata_into_message(sys_msg: Message, pool: 'AgentPool', instance: AgentInstance) -> None:
    """Inject Session Metadata block into system message if not already present.

    This is the PERSISTING pre-log writer (todo.md:155): it runs before logging so the
    metadata block reaches the JSONL on disk for every instance class. The refresh in
    execution_engine._setup_turn() only updates an already-present block and never
    first-injects — do NOT remove this call assuming _setup_turn covers persistence.

    Args:
        sys_msg: System Message object to modify in-place
        pool: AgentPool instance (needed for _build_session_metadata)
        instance: AgentInstance whose metadata should be injected
    """
    from agent_cascade.execution_engine import _build_session_metadata

    # Defensive guard for empty content
    if not sys_msg.content or not sys_msg.content.strip():
        return

    # Frozen prompt: skip injection (a missing heading on a frozen prompt is
    # pre-existing degradation, not a bug worth killing a live turn for).
    # Read under _compression_lock so the check and the write below are one
    # atomic unit against a concurrent freeze_system_prompt().
    with instance._compression_lock:
        if getattr(instance, '_system_prompt_frozen', False):
            logger.warning(
                '[SYS_PROMPT_FROZEN] metadata injection skipped on live %s', instance.instance_name)
            return

        if '## Session Metadata' not in sys_msg.content:
            meta_block = _build_session_metadata(pool, instance)
            if meta_block:
                content_lines = sys_msg.content.split('\n')
                # Insert after identity line; skip extra blank/comment lines
                # (matches execution_engine.py line 943)
                insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith('#') else 1
                for i, ml in enumerate(meta_block.split('\n')):
                    content_lines.insert(insert_pos + i, ml)
                sys_msg.content = '\n'.join(content_lines)


class AgentLifecycleManager:
    """Manages agent instance lifecycle: creation, reuse, and configuration.

    This class handles:
    - Finding or creating instances (with reuse logic)
    - Building system and task messages
    - Propagating settings from parent to child
    - Initializing conversations and logger state

    Usage:
        manager = AgentLifecycleManager(pool)
        inst, is_reuse, session_was_loaded = manager.find_or_create_instance(...)
        sys_msg = manager.build_system_message(...)
        manager.propagate_settings(inst, caller_name, agent_class)
    """

    def __init__(self, pool):
        """Initialize with reference to AgentPool.

        Args:
            pool: AgentPool instance for template lookup and state management
        """
        self.pool = pool
        self._engine = None  # Lazy initialization

    @property
    def engine(self) -> 'ExecutionEngine':
        """Get engine reference (raises if not set)."""
        if self._engine is None:
            raise RuntimeError('AgentLifecycleManager._engine not set. Call ExecutionEngine.initialize().')
        return self._engine

    def set_engine(self, engine: 'ExecutionEngine') -> None:
        """Set engine reference after ExecutionEngine construction completes.

        This breaks the circular dependency during __init__.

        Args:
            engine: ExecutionEngine instance for cross-reference
        """
        self._engine = engine

    def _prepare_instance_for_reuse(self, inst, caller, nest_depth: int) -> None:
        """Re-point a reused instance's ownership + clear its per-run child/skill/stream state.

        H3 (Security/Compressor instance reuse v2): this block was duplicated verbatim between the
        sub-agent reuse branch of :meth:`find_or_create_instance` and
        ``ExecutionEngine._acquire_reusable_system_agent`` — 9 fields including the same
        old-parent-capture-BEFORE-overwrite subtlety (flagged CRITICAL). Extracted here so both
        paths stay in lockstep. Thread-safety mirrors the original: _children_lock for reading the
        old parent, _state_lock for mutating instance state, and _update_child_relationship to drop
        the stale link from the previous caller's children set.
        """
        # Update _nest_depth to reflect current call chain depth (Fix)
        inst._nest_depth = nest_depth

        # MAJOR FIX: Reset last_activity when reusing instance so idle timer starts from reuse event
        inst.last_activity = time.monotonic()

        # Clear old child tracking — reused instances start fresh with no children.
        # Track old parent for cleanup, then update to new caller (thread-safe).
        # Use _children_lock for pool.children and _state_lock for instance state.
        with self.pool._children_lock:
            old_parent = inst.parent_instance
        with inst._state_lock:
            inst.parent_instance = caller
            inst._child_instances.clear()

        # Clear stale auto-skill state from the previous run. The snapshot
        # (_auto_skill_task_output) shadows the new task's answer in
        # extract_instance_output(), and the one-shot flag would permanently
        # disable skill reflection for this instance on all future recalls.
        inst._auto_skill_task_output = None
        inst._auto_skill_source_task = None
        inst._auto_skill_proposed = False
        inst._auto_skill_dirty_stop = False

        # TG-STREAM F4: clear the stream-time push flags too — a reused
        # instance must not carry its previous run's "already pushed" state forward,
        # or the first text output / final answer of the new run would be suppressed.
        inst._tg_first_pushed = False
        inst._tg_final_pushed_phase = None
        inst._tg_first_pushed_text = None

        # Streaming partials (agent_instance.py:323) — reset_conversation() does NOT clear
        # _streaming_responses, so a reused instance would otherwise serve its previous run's stale
        # tail on the first frame. It is repopulated from the first LLM yield of the new run. Mirrored
        # in ExecutionEngine._acquire_reusable_system_agent so both reuse paths stay in lockstep.
        inst._streaming_responses = []

        # Remove from old parent's tracking even if new caller is None
        if old_parent is not None:
            self.pool._update_child_relationship(old_parent, inst.instance_name, add=False)

    def find_or_create_instance(self,
                                agent_class: str,
                                instance_name: str,
                                caller: str,
                                nest_depth: int,
                                force_fresh: bool = False,
                                log_file: Optional[str] = None) -> Tuple[AgentInstance, bool, bool]:
        """Find existing inactive instance or create new one.

        Checks for an existing inactive (IDLE/TERMINATED) instance that can be reused.
        If no reusable instance exists, creates a new AgentInstance.

        Args:
            agent_class: Template class name
            instance_name: Unique instance identifier
            caller: Parent instance name
            nest_depth: Depth in call chain
            force_fresh: If True, always create new instance (for Security/Compressor)
            log_file: Optional path to a JSONL log file to load session history from

        Returns:
            Tuple of (instance, is_reuse, session_was_loaded) where is_reuse indicates
            if existing was reused and session_was_loaded indicates that conversation
            history was loaded from a log file
        """
        instance_name = self.pool._resolve_instance_name(instance_name)
        now = time.monotonic()
        existing = self.pool.instances.get(instance_name)
        is_reuse = False
        inst = None  # Initialize to ensure it's always defined

        # Skip reuse logic if force_fresh=True (for Security/Compressor agents)
        if not force_fresh and existing is not None:
            # Reuse existing instance if it's IDLE or TERMINATED (not currently
            # executing)
            existing_state = getattr(existing, 'state', None)
            if existing_state in (AgentState.IDLE, AgentState.TERMINATED):
                # Reuse existing inactive instance instead of creating new one
                inst = existing
                is_reuse = True

                # Update _nest_depth to reflect current call chain depth (Fix
                inst._nest_depth = nest_depth

                # MAJOR FIX: Reset last_activity when reusing instance so idle
                # timer starts from reuse event
                inst.last_activity = now

                # H3: re-point ownership + clear per-run child/skill/stream state via the shared
                # helper (also used by ExecutionEngine._acquire_reusable_system_agent) so the two
                # reuse paths cannot drift.
                self._prepare_instance_for_reuse(inst, caller, nest_depth=nest_depth)

                logger.debug(f"[INSTANCE REUSE] '{instance_name}' ({agent_class}) reusing existing inactive instance. "
                             f"Conversation history will be preserved and extended.")
            else:
                # Existing instance is still active
                # (RUNNING/SLEEPING/COMPLETING), fall through to create new one
                existing = None  # Clear so we don't incorrectly log about reusing an active instance

        if inst is None or not is_reuse:
            # Create new instance (existing is None or still active)
            inst = AgentInstance(
                instance_name=instance_name,
                agent_class=agent_class,
                conversation=[],
                max_turns=None,  # Will be set below via settings propagation (P6)
                parent_instance=caller,
                created_at=now,
                last_activity=now,
                compression_summary=None,
                latest_marker_index=-1,
                _nest_depth=nest_depth,
            )

            if existing is not None:
                # Warn about overwriting an active instance
                logger.warning(f"[NEW INSTANCE] '{instance_name}' ({agent_class}) replacing active instance. "
                               f"Previous instance conversation will be replaced.")

        # FIX
        if not is_reuse:
            self.pool.instances[instance_name] = inst
            logger.debug('[CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for %s',
                         instance_name)

            # Per-instance child tracking + pool.children sync via helper (thread-safe, deduped)
            if caller is not None and caller != instance_name:
                self.pool._update_child_relationship(caller, instance_name, add=True)
        else:
            # Reuse path: update per-instance child tracking for the new caller.
            # NOTE: the "remove from old parent" step is intentionally NOT done here — it is
            # already handled inside _prepare_instance_for_reuse() (which captures old_parent
            # under _children_lock before re-pointing ownership and drops the stale link). A
            # duplicate removal was left behind after that H3 extraction and referenced an
            # out-of-scope `old_parent` (NameError); it has been removed. Only the add-to-new
            # parent remains, which the helper dedupes.
            if caller is not None and caller != instance_name:
                self.pool._update_child_relationship(caller, instance_name, add=True)

        # BUG FIX (Bug 2): Load session from log_file if provided
        session_was_loaded = False
        if log_file:
            # Don't dismiss all instances — we're just loading history into an existing instance
            status = self.pool.load_session_from_log(log_file,
                                                     target_instance=instance_name,
                                                     clear_sub_agents_before_load=False,
                                                     caller_name=caller)
            if status.startswith('Error'):
                logger.warning(f"[LOG_FILE_LOAD] Failed to load session for '{instance_name}': {status}")
                if status.startswith('Error: Log file not found'):
                    # BUG_0207: a missing log file is a hard error, not a warn-and-continue.
                    # Other load errors (corrupt file, no messages) keep the existing
                    # warn-and-continue behavior.
                    raise LogFileNotFoundError(log_file)
            else:
                # load_session_from_log creates a new instance in the pool,
                # update our reference
                inst = self.pool.instances.get(instance_name) or inst
                logger.info(f"[LOG_FILE_LOAD] Loaded session for '{instance_name}': {status}")
                session_was_loaded = True

                # BUG_0032: A resurrected instance must not be pre-terminated. The name-scoped
                # pool.terminated_instances set is NOT cleared by load_session_from_log (it only
                # swaps the instance object), so a stale entry left behind by a prior
                # terminate_instance() with no clean thread-join (sub-agent names never register a
                # thread in pool._instance_threads, so their entry is immortal) would make
                # is_instance_terminated(name) return True and abort the run at core.py before the
                # first turn. Clear it here — the orchestration decision point ("this call is
                # resurrecting an instance"). Mirrors the reuse-path flag reset (FIX #4 below).
                # Only runs on the log_file branch; discard on an absent name is a no-op. Under
                # _pool_lock (the RLock guarding instances + terminated_instances); taken alone
                # here — never nested inside a per-instance lock (Gotcha 3).
                with self.pool._pool_lock:
                    was_terminated = instance_name in self.pool.terminated_instances
                    self.pool.terminated_instances.discard(instance_name)
                if was_terminated:
                    logger.info(f"[LOG_FILE_LOAD] Cleared stale termination marker for "
                                f"'{instance_name}' so the restored session can run.")

        # BUG_0052 (Change A): give the child a back-reference to the pool so release_slot_permit can
        # recover the router via holder._pool_ref.api_router (slot_queue.py:578). Without this, call_agent
        # children (registered directly here, not via pool/lifecycle.py:90) silently skip the
        # _last_released_endpoint write + committed-marker cleanup on release. Set UNCONDITIONALLY on the
        # FINAL instance — placed AFTER the session-load block above because load_session_from_log can
        # reassign `inst` to a fresh instance (line ~279), which would otherwise lose this reference.
        # Covers all three outcomes: fresh-creation, reuse, and session-restored. Touches no lock (Gotcha 3).
        inst._pool_ref = self.pool

        return inst, is_reuse, session_was_loaded

    def build_system_message(self, agent_class: str, instance_name: str) -> Message:
        """Build system message for new agent.

        Retrieves template and constructs system message with injected instance name.
        Session metadata injection is handled by P7 in _setup_turn for all agents uniformly.

        Args:
            agent_class: Template class name
            instance_name: Instance name to inject into template

        Returns:
            System Message object

        Raises:
            ValueError: If no template found for agent_class
        """
        template = self.pool.get_template(agent_class)
        if not template:
            logger.error('NO TEMPLATE for %s/%s', agent_class, instance_name)
            raise ValueError(f"No template for agent class {agent_class}")

        sys_content = getattr(template, 'base_system_message', getattr(template, 'system_message', ''))
        lines = sys_content.strip().split('\n') if sys_content else []

        # Replace identity line
        if lines and f" {instance_name}" not in lines[0]:
            lines[0] = f"You are {instance_name}."

        return Message(role=SYSTEM, content='\n'.join(lines))

    def build_task_message(self, args: dict, caller: str) -> Message:
        """Build a plain-text task message from the explicit task and context arguments.

        No images are automatically forwarded from the parent conversation. If the
        caller needs the sub-agent to view an image, it should include the file path
        in the task text; the sub-agent can then call view_image on it.

        Matches main AC branch formatting: always wraps context and task with labeled sections,
        adds caller prefix to context section, and includes a closing instruction.

        Args:
            args: Tool arguments (task, context)
            caller: Parent instance name (used for the "This is a message from {caller}." prefix)

        Returns:
            Task Message object with plain-text content
        """
        task_text = args.get('task', '')
        context_text = args.get('context', '')

        # Task 1: Include max_turns info in context when call_agent was called
        # with a custom turn budget
        # This lets the sub-agent know its allocated turn limit upfront
        max_turns_info = ''
        # Tokens mode takes precedence when a token_budget was requested; otherwise
        # the turn-budget notice stays byte-identical (turns-mode unchanged).
        if 'token_budget' in args and isinstance(args['token_budget'], int) and args['token_budget'] > 0:
            max_turns_info = budget_allocation_notice(args['token_budget'], 'tokens')
        elif 'max_turns' in args and isinstance(args['max_turns'], int) and args['max_turns'] > 0:
            max_turns_info = budget_allocation_notice(args['max_turns'], 'turns')

        # Match main AC branch formatting behavior
        caller_prefix = f"This is a message from {caller}."
        if context_text:
            context_text = f"{caller_prefix}\n{context_text}"
        else:
            context_text = caller_prefix

        task_text = f'Context: {context_text}{max_turns_info}\n\nTask: {task_text}\n\nPlease help with this task.'

        # Fallback for empty message (match main AC branch behavior)
        if not task_text.strip():
            task_text = 'Please proceed with your task.'

        return Message(role=USER, content=task_text)

    def initialize_conversation(  # FIX #3 (reviewer): Renamed from initialize_instance_conversation
            self,
            instance: AgentInstance,
            sys_msg: Message,
            task_msg: Message,
            is_reuse: bool,
            instance_name: str,
            agent_class: str,
            from_external_load: bool = False) -> list:
        """Initialize or extend instance conversation.

        For reused instances: resets stale state, updates system message in-place,
        appends task message to preserved conversation, and syncs logger.

        For new instances: builds fresh [system, task] conversation, assigns to instance,
        and logs initial messages.

        Args:
            instance: AgentInstance to initialize
            sys_msg: System message
            task_msg: Task message
            is_reuse: Whether reusing existing instance
            instance_name: Instance name for logger access
            agent_class: Agent class for logger access
            from_external_load: If True, conversation was loaded from a log file and should be preserved

        Returns:
            Conversation list (either preserved or newly created)
        """
        # Pre-log injection (todo.md:155): ensures the block reaches the JSONL on disk for every
        # instance class. This is the PERSISTING writer. The refresh-only path in _setup_turn()
        # only updates an already-present block and never first-injects — so do NOT remove this
        # call assuming _setup_turn covers persistence.
        _inject_metadata_into_message(sys_msg, self.pool, instance)

        # L1 (BUG_0278) prefix-stability guard. Stable system-agent classes reuse their KV
        # prefix, so conversation[0] must not carry a per-run volatile field.
        # _build_session_metadata gates them at runtime; this asserts the rendered block
        # came out clean so drift is surfaced. A volatile field is a cache MISS (perf), not
        # a correctness fault — so log it LOUDLY but do not abort a live agent turn. The
        # build/CI regression test (test_stable_prefix_byte_identity.py) renders the real
        # templates and fails the build on the same condition, catching drift before ship.
        # No-op for non-stable classes.
        if instance.agent_class.lower() in STABLE_SYSTEM_PROMPT_CLASSES:
            try:
                _lint_stable_prefix(sys_msg.content)
            except PrefixStabilityError as exc:
                logger.error('[PREFIX_STABILITY] stable-class %s conversation[0] drift: %s',
                             instance.instance_name, exc)

        # FIX (todo.md:117): Ensure logger metadata reflects actual supervisor.
        # AgentInstanceLogger defaults to "System" — correct it to the real parent.
        # - New loggers: base_metadata is merged before _initial_save() → correct on disk.
        # - Existing loggers (recall/reuse): patch in-memory via update_supervisor().
        #   The on-disk value retains the original spawner (historically accurate).
        try:
            expected_supervisor = instance.parent_instance or 'User'
            log_inst = self.pool.get_logger(instance_name,
                                            agent_class,
                                            base_metadata={'supervisor': expected_supervisor})
            if log_inst.data['metadata'].get('supervisor') != expected_supervisor:
                log_inst.update_supervisor(expected_supervisor)
        except (AttributeError, KeyError, OSError) as e:
            logger.warning(f"Logger supervisor metadata update failed for {instance_name}: {e}")

        # FIX: Initialize before if/else so it's available for both branches
        # (was only set in else branch)
        is_restored_session = False

        if is_reuse:
            # Thread-safe update of instance state for reuse
            with instance._compression_lock:
                # FIX #3: Reset stale state fields to prepare for new task
                instance.compression_summary = None
                instance.latest_marker_index = -1
                instance._generate_cfg_override = None
                instance.max_turns = None
                instance._current_turn = 0

                # FIX #4: Clear is_terminated flag
                instance.is_terminated = False
                # BUG_0205: clear the loop-termination reason so a reused child doesn't
                # report a stale cause on a later, unrelated termination.
                instance._terminated_reason = None

                # SLOT_LEAK HARDENING: Release any held slot permit BEFORE clearing it.
                # Normal case is a no-op (reused instances are IDLE/TERMINATED). Guards
                # against stale callbacks that would leak the pool permit (2026-08-16 deadlock).
                # Shared capture-nullify-release-log helper: atomic under _state_lock,
                # drop-reuse line only when a live permit was actually released.
                if getattr(instance, '_slot_release', None) is not None:
                    logger.warning(f"[SLOT_REUSE_RELEASE] '{instance.instance_name}' held a slot permit "
                                   f"on reuse (unexpected — should be IDLE/TERMINATED). Releasing before clearing.")
                from agent_cascade.slot_queue import release_slot_permit
                _reuse_pool = None
                try:
                    _sched = getattr(self.pool, 'scheduler', None)
                    if _sched is not None:
                        _held_key = getattr(instance, '_slot_key', None)
                        _reuse_pool = _sched._pools.get(_held_key) if _held_key else None
                except Exception:
                    pass
                release_slot_permit(instance,
                                    instance.instance_name,
                                    action='drop-reuse',
                                    context='on reuse',
                                    pool=_reuse_pool)

                # FIX: Preserve & extend conversation
                # Update system message in-place (first message is always
                # system)
                if instance.conversation and len(instance.conversation) > 0:
                    old_sys_msg = instance.conversation[0]

                    # Recall no-op: if system message unchanged, skip edit to preserve prefix cache.
                    if (sys_msg is old_sys_msg) or \
                            (getattr(sys_msg, 'role', None) == getattr(old_sys_msg, 'role', None)
                             and sys_msg.content == old_sys_msg.content):
                        # System prompt unchanged on recall — nothing to rewrite.
                        pass
                    else:
                        # Preserve old system message's timestamp so
                        # update_history() can match it as an update
                        if hasattr(old_sys_msg, 'timestamp') and old_sys_msg.timestamp:
                            try:
                                sys_msg.timestamp = old_sys_msg.timestamp
                            except AttributeError:
                                pass  # Fallback: generate new timestamp below

                        # FIX
                        if not getattr(sys_msg, 'timestamp', None):
                            sys_msg.timestamp = datetime.datetime.now().isoformat()

                        # Update the existing system message with new template
                        # content
                        try:
                            instance.edit_message_in_place(0, sys_msg)  # PR2: centralized API handles cache sync
                        except SystemPromptFrozenError:
                            logger.warning(
                                '[SYS_PROMPT_FROZEN] edit_message_in_place(0) blocked on live %s — '
                                'system prompt preserved', instance.instance_name)
                else:
                    # Fallback: prepend system message if conversation is empty
                    instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync

                # Get the preserved conversation (will be extended with task
                # below)
                conv = instance.conversation

                # FIX
                # (conv already set above with system message updated in-place)
            instance.append_message(task_msg)  # PR2: centralized mutation API handles cache sync

            # FIX
            # load_session_from_log(). Just log the new task message directly
            # instead of
            # calling update_history() with the trimmed working set (~33 msgs
            # vs 63+ in log).
            # The forward-only search in update_history can miss matches
            # against the full
            # history, causing buffer insertions → duplicates.
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                log_inst.log_message(task_msg)

                # Logged task message to logger for reused instance

                # ── Tail sync check after task logging (design doc §5.2 — D1
                # fix) ──
                if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                    from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                    with instance._compression_lock:
                        current_conv = list(instance.conversation)
                    _check_tail(instance_name, current_conv, log_inst.log_path, context='reused_instance_init')
            except Exception as e:
                logger.debug(f"Logging task message for reused {instance_name} failed (non-critical): {e}")
        else:
            # For new instances: check if session was loaded from a log file
            # (explicit parameter, not flag)
            with instance._compression_lock:
                if from_external_load:
                    # Session loaded from log file — only append task message,
                    # preserve restored conversation
                    instance.append_message(task_msg)  # Centralized mutation API handles cache sync

                    conv = instance.conversation
                    is_restored_session = True
                else:
                    # Build conversation: [system, task] for fresh instances
                    conv = [sys_msg, task_msg]
                    instance.rebuild_conversation(conv)  # PR2: centralized mutation API handles full cache invalidation

                    is_restored_session = False

            # Log messages to agent's JSONL file (outside lock — logger has its
            # own synchronization)
            # Note: Direct log_message calls are acceptable here because the
            # initialization path
            # runs single-threaded and messages are already in conversation
            # before logging.
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                if is_restored_session:
                    log_inst.log_message(task_msg)
                else:
                    log_inst.log_message(sys_msg)
                    log_inst.log_message(task_msg)

                # ── Tail sync check after session init logging (design doc
                # §5.2 — D1 fix) ──
                if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                    from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                    with instance._compression_lock:
                        conv = list(instance.conversation)
                    _check_tail(instance_name, conv, log_inst.log_path, context='session_init')
            except Exception as e:
                logger.debug(f"Logging messages for {instance_name} failed (non-critical): {e}")

        return conv

    def propagate_settings(
        self,
        instance: AgentInstance,
        caller: str,
        agent_class: str,
        call_agent_args: dict = None,
    ) -> None:
        """Propagate settings from caller to child instance.

        Propagates max_turns and disabled_tools from the caller agent's configuration
        to the child instance. Uses single lock scope to prevent race conditions where
        another thread reads partial state.

        Note: max_input_tokens is NOT propagated here. It is resolved dynamically
        at call time via _resolve_max_tokens(), which consults the API Router for
        live endpoint values (enabling failover). Baking it into the override would
        freeze the value at spawn time and block router updates.

        Args:
            instance: Child instance to configure
            caller: Parent instance name
            agent_class: Child's agent class
            call_agent_args: Optional args dict from the call_agent tool invocation.
                           If it contains 'max_turns', that value is used (capped by caller's limit).

        Note:
            If target template has no LLM config, max_turns is still set but
            disabled_tools propagation is skipped.
        """
        # FIX
        if not hasattr(self.pool, 'api_router') or not self.pool.api_router:
            logger.debug('Settings propagation skipped — no api_router on pool')
            return

        try:
            caller_inst = self.pool.get_instance(caller)
            if not caller_inst:
                return

            caller_template = self.pool.get_template(caller_inst.agent_class)
            if not caller_template or not hasattr(caller_template, 'llm'):
                return

            # Use caller instance's override first (has user's UI settings),
            # fall back to template's generate_cfg
            llm_cfg = getattr(caller_inst, '_generate_cfg_override', None) or getattr(
                caller_template.llm, 'generate_cfg', {})

            # Propagate max_turns from caller's instance directly.
            # Do NOT read from llm_cfg — it was stripped out of
            # _generate_cfg_override
            # because 'max_turns' is in NON_LLM_KEYS and must not leak to the
            # LLM API.
            caller_max_turns = getattr(caller_inst, 'max_turns', None) or DEFAULT_MAX_TURNS

            # If agent budgeting is enabled, allow per-agent max_turns
            # overrides via call_agent args.
            # Otherwise, all agents use the caller's max_turns limit regardless
            # of call_agent args.
            enable_agent_budgeting = getattr(self.pool.settings, 'enable_agent_budgeting', True) if hasattr(
                self.pool, 'settings') else True

            # Use provided max_turns from call_agent args if specified,
            # otherwise inherit from caller.
            # The caller's limit (UI turn limit) acts as the hard cap.
            if call_agent_args and 'max_turns' in call_agent_args and enable_agent_budgeting:
                requested_max = call_agent_args['max_turns']
                # Validate: must be a positive integer
                if not isinstance(requested_max, int) or requested_max < 1:
                    logger.debug(
                        f"Invalid max_turns={requested_max} for {instance.instance_name}, using caller's limit")
                    instance.max_turns = caller_max_turns
                else:
                    instance.max_turns = min(requested_max, caller_max_turns)
            else:
                instance.max_turns = caller_max_turns

            # ── Tokens-mode budget (cost/token-budget Slice 2) ──────────────────
            # Additive parallel to max_turns: instance.token_budget (0 = disabled).
            # Gated on enable_agent_budgeting (default OFF); caller's token_budget is
            # the hard cap. token_budget is in NON_LLM_KEYS so it must not leak to the
            # LLM API (never written into _generate_cfg_override).
            caller_token_budget = (
                getattr(caller_inst, 'token_budget', 0) or getattr(
                    self.pool.settings, 'token_budget', DEFAULT_TOKEN_BUDGET)
                if hasattr(self.pool, 'settings') else getattr(caller_inst, 'token_budget', 0)
            )
            if call_agent_args and 'token_budget' in call_agent_args and enable_agent_budgeting:
                requested_tok = call_agent_args['token_budget']
                if not isinstance(requested_tok, int) or requested_tok < 1:
                    logger.debug(f"Invalid token_budget={requested_tok} for {instance.instance_name}, using caller's")
                    instance.token_budget = caller_token_budget
                else:
                    instance.token_budget = min(requested_tok, caller_token_budget) if caller_token_budget else requested_tok
                instance.budget_unit = 'tokens'
            else:
                instance.token_budget = caller_token_budget
                instance.budget_unit = getattr(caller_inst, 'budget_unit', 'turns')

            target_template = self.pool.get_template(agent_class)
            if not target_template or not getattr(target_template, 'llm', None):
                # Target template has no LLM — skip settings propagation but
                # continue execution
                logger.warning(f"Target agent instance ({agent_class}) template has no LLM config — "
                               f"skipping settings propagation (disabled_tools only)")
                return

            # FIX: Do NOT bake max_input_tokens into _generate_cfg_override here.
            # Doing so freezes the value at spawn time and short-circuits
            # _resolve_max_tokens() from consulting the API Router for live
            # endpoint values on failover. Per-instance overrides should only
            # contain max_input_tokens when explicitly set via UI (_apply_ui_config),
            # which is already handled correctly there.
            with self.pool._state_lock:
                # Propagate non-LLM config settings from caller to child instance.
                if llm_cfg:
                    cfg = (target_template.llm.generate_cfg or {}).copy()

                    # Ensure max_input_tokens is NOT baked into the override,
                    # so _resolve_max_tokens() can consult the API Router dynamically.
                    cfg.pop('max_input_tokens', None)

                    # Merge UI-level settings from caller's config that should
                    # propagate to children.
                    # Keys in NON_LLM_KEYS are excluded so each agent uses its
                    # own model config (includes max_input_tokens, max_turns, etc.).
                    if llm_cfg:
                        for k, v in llm_cfg.items():
                            if k not in NON_LLM_KEYS and v is not None:
                                cfg[k] = v

                    instance._generate_cfg_override = cfg

                # Centralized disabled_tools resolution — see
                # agent_cascade.utils.disabled_tools
                from agent_cascade.utils.disabled_tools import (merge_disabled_tools, normalize_disabled_tools,
                                                                resolve_disabled_tools_for_agent)

                # Resolve caller's full disabled set (includes class defaults
                # via resolver)
                caller_type = getattr(caller_template, 'agent_type', '') or ''
                caller_name = getattr(caller_template, 'name', '') or ''
                caller_disabled = resolve_disabled_tools_for_agent(
                    instance_override=getattr(caller_inst, '_generate_cfg_override', None),
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=caller_name,
                    agent_type=caller_type,
                )

                # Also resolve disabled tools FOR THE CHILD AGENT from the
                # caller's per-agent dict.
                # The caller's _generate_cfg_override may contain a dict like
                # {'Compressor': [...], 'Coder': [...]} — we need to look up
                # the child's entry.
                target_name = getattr(target_template, 'name', '') or agent_class
                target_type = getattr(target_template, 'agent_type', '') or ''
                # Fallback to instance's agent_class for defense-in-depth
                # (matches execution_engine.py)
                if not target_type:
                    target_type = getattr(instance, 'agent_class', '') or ''
                # BUG_0242: Read the live pool entry for the child's class FIRST.
                # A live pool entry is explicit user config for that class, but the
                # caller's _generate_cfg_override may be None (unwarmed caller, and
                # the reuse path which nulls it in initialize_conversation). Resolving
                # from a None override leaves the resolver's Layer-4 gate at
                # has_explicit_config=False, which attaches the read-only
                # DEFAULT_NEW_AGENT_DISABLED_TOOLS baseline to the child override.
                # The live-pool union below can only ADD disabled tools, so a pool
                # entry like {'Researcher': ['forget_last']} could never re-enable
                # baseline tools (shell_cmd/write_file/edit_file/...). Feeding the
                # pool entry through instance_override flips has_explicit_config=True
                # so the baseline is skipped and the pool entry becomes authoritative
                # for the child. Soul/discovered agents WITHOUT a pool entry keep the
                # baseline (intended read-only-by-default behavior).
                pool_entry = set()
                if self.pool and hasattr(self.pool, 'get_ui_disabled_tools_for_agent'):
                    raw_pool_entry = self.pool.get_ui_disabled_tools_for_agent(target_name, target_type)
                    # Accept collections only — guards against None and against bare
                    # MagicMock pool doubles whose auto-attributes are truthy.
                    if isinstance(raw_pool_entry, (set, frozenset, list, tuple)):
                        pool_entry = set(raw_pool_entry)

                caller_override = getattr(caller_inst, '_generate_cfg_override', None)
                child_override_src = caller_override
                if pool_entry:
                    # Preserve the caller's explicit dict-form entries (e.g. per-agent
                    # dicts like {'Compressor': [...]}) and layer the pool entry under
                    # the child's own name key. Caller-level flat lists apply to every
                    # agent and already reach the child via caller_disabled below, so
                    # replacing a flat form with this targeted dict loses nothing.
                    base_override = dict(caller_override) if isinstance(caller_override, dict) else {}
                    caller_dt = base_override.get('disabled_tools')
                    if isinstance(caller_dt, dict):
                        merged_dt = dict(caller_dt)
                        merged_dt[target_name] = sorted(pool_entry | normalize_disabled_tools(caller_dt.get(target_name)))
                    else:
                        merged_dt = {target_name: sorted(pool_entry)}
                    base_override['disabled_tools'] = merged_dt
                    child_override_src = base_override

                child_disabled_from_caller_cfg = resolve_disabled_tools_for_agent(
                    instance_override=child_override_src,
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=target_name,
                    agent_type=target_type,
                )

                # Propagate caller's disabled tools into child instance
                # override.
                # Merge with any existing disabled_tools already on the child
                # config.
                cfg = (copy.deepcopy(instance._generate_cfg_override) if instance._generate_cfg_override else
                       (target_template.llm.generate_cfg or {}).copy())

                existing_disabled = normalize_disabled_tools(cfg.get('disabled_tools'))
                merged = merge_disabled_tools(existing_disabled, caller_disabled)
                # Also merge child-specific disabled tools extracted from
                # caller's per-agent dict.
                # This ensures entries like {'Compressor': [...]} are properly
                # applied to the child.
                merged = merge_disabled_tools(merged, child_disabled_from_caller_cfg)

                # Check live pool config for real-time tool updates.
                # (BUG_0242: pool_entry was already read above for the baseline
                # decision; re-unioning it here is idempotent and keeps caller
                # explicit disables additive.)
                merged = merge_disabled_tools(merged, pool_entry)

                cfg['disabled_tools'] = list(merged)  # store as list for JSON serialization
                instance._generate_cfg_override = cfg

                # Defense-in-depth defaults (Security/Compressor) are applied
                # both here via the
                # child lookup above AND again at runtime during engine.run() —
                # idempotent by design.
        except Exception as e:
            logger.debug(f"Settings propagation from {caller} to instance failed (non-critical): {e}")

"""Security advisor handler for tool approval checks.

Extracted from api_server.py ask_security block (~400 lines) as Phase 3 of the
API server refactoring plan.  Preserves exact behavior — identical verdict
parsing, timeout handling, auto-apply/reject logic, and cleanup sequence.
"""
import asyncio
import copy
import json
import os
import platform
import time
import threading
from typing import Any, Dict, Optional

# ── Deadlock protection constants ───────────────────────────────────────────
# NOTE: The system-launched Security advisor is bounded by a turn budget
# (SECURITY_AGENT_MAX_TURNS in settings.py), which lets the model finish its
# reasoning and forces a final verdict on its last turn. A wall-clock first-yield
# timer (below) remains only as a last-resort guard against an LLM generator that
# never yields its first token. The user-facing approval_timeout_seconds still
# governs the "taking longer than expected" warning.

# Timeout for acquiring the security check lock.
# Security checks are FIFO-serialized via this global lock — a waiting check must
# wait as long as the previous one legitimately runs (up to ~300s first-yield + turns).
# Set well beyond max legitimate hold time so concurrent requests queue properly.
# The ResettableRLock dead-holder detection still recovers from truly leaked locks.
SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS = int(os.getenv('QWEN_AGENT_SECURITY_LOCK_ACQUIRE_TIMEOUT', 600))

# Last-resort guard against an LLM generator that never yields its first token.
# The engine watchdog only activates after the first output; this timer covers the
# pre-first-yield gap. Generous on purpose — it must not cut off a slow-but-progressing
# model (the turn budget handles normal completion). Only fires if NO yield at all.
SECURITY_FIRST_YIELD_TIMEOUT_SECONDS = int(os.getenv('QWEN_AGENT_SECURITY_FIRST_YIELD_TIMEOUT', 300))

# ── Module-level helpers used by the security handler ───────────────────────


def _get_ws_loop(agent_pool):
    """Get the running WebSocket event loop from the agent pool.

    The security handler runs in background threads that have no event loop.
    We use the pool's stored reference to the main event loop (set by
    run_agent_unified.py) so that run_coroutine_threadsafe() can actually
    execute scheduled coroutines on a running loop.

    Returns None if unavailable — callers should skip WebSocket notifications
    (they are best-effort UI feedback). Aligns with codebase-wide pattern used
    in api_integration.py, stream_publisher.py, and api_server.py.
    """
    if agent_pool is None:
        return None

    ws_loop = getattr(agent_pool, '_ws_loop', None)
    if ws_loop is not None:
        try:
            if not ws_loop.is_closed():
                return ws_loop
        except Exception:
            pass  # Loop object may be corrupted; treat as unavailable

    return None


def _get_security_check_lock(app):
    """Get (creating if needed) the app-level security prompt lock.

    DEADLOCK FIX: Uses RLock to allow reentrant acquisition. If a Security agent
    triggers another security check, the same thread can acquire this lock again
    without deadlocking. This replaces the original non-reentrant Lock().

    Also known as security_prompt_lock — protects prompt building phase only.
    """
    if not hasattr(app, 'security_check_lock'):
        app.security_check_lock = threading.RLock()
    return app.security_check_lock


class ResettableRLock:
    """An RLock wrapper that can recover from a leaked lock.

    The plain ``threading.RLock`` used for the security execution lock is acquired
    by a daemon thread (``_run_check_worker``). If that thread is killed before it
    reaches ``exec_lock.release()`` — e.g. session stop, agent dismissal, or an
    unhandled crash that skips the ``finally`` block — the RLock is leaked forever
    and every subsequent security check times out on ``acquire(timeout=10s)``.

    Python's RLock cannot be force-released from another thread, so this wrapper
    tracks the owning thread and, when a new acquirer detects that the previous
    holder is DEAD (no longer alive), it replaces the internal RLock with a fresh
    one. This is safe because:
      - We only reset when ``acquire()`` timed out, i.e. the current thread is NOT
        the owner, so no live thread holds the lock we are about to discard.
      - A LIVE holder (another check genuinely running) is never reset — its thread
        is still alive, so normal timeout semantics apply and the caller raises.

    Reentrancy is preserved: the internal ``threading.RLock`` handles same-thread
    nested acquisition exactly as before.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._owner_thread = None   # threading.Thread of the current holder (None if free)
        self._acquired_at = 0.0     # time.monotonic() when acquired (for staleness logging)

    def acquire(self, timeout=None):
        """Acquire with optional timeout. Returns True on success, False on timeout.

        ``timeout=None`` means block until acquired (no timeout), matching the
        native RLock's no-arg behavior. We branch explicitly because passing
        ``timeout=None`` to the underlying RLock.acquire() raises TypeError.
        """
        if timeout is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=timeout)
        if acquired:
            self._owner_thread = threading.current_thread()
            self._acquired_at = time.monotonic()
        return acquired

    def release(self):
        """Release the lock (normal path — called from the finally block)."""
        try:
            self._lock.release()
        finally:
            # Clear ownership tracking even if release raised, so a later
            # force_reset decision is not based on stale owner info.
            self._owner_thread = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("ResettableRLock: timed out acquiring lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    @property
    def owner_is_alive(self) -> bool:
        """True if the current holder thread is still running.

        A live holder means another check is genuinely in progress — we must NOT
        steal its lock. A dead holder (or none) means the lock may be leaked.

        Note: reading ``_owner_thread`` and calling ``is_alive()`` is not a single
        atomic step, but this is safe by construction — a thread that is alive when
        read can only transition to *dead* afterwards, never the reverse. So the only
        "wrong" outcome we could observe is treating a just-died holder as alive (we
        then block/timeout normally and recover on the *next* attempt), which is
        strictly safer than the opposite error of stealing a live lock.
        """
        owner = self._owner_thread
        return owner is not None and owner.is_alive()

    def force_reset(self, reason: str = "") -> bool:
        """Force-release a leaked lock by swapping in a fresh RLock.

        DANGEROUS — only call when the previous holder is known to be dead.
        Returns True if the reset actually swapped the lock (i.e. it was held),
        False if the lock was already free (nothing to reset).
        """
        from agent_cascade.log import logger
        was_held = self._owner_thread is not None
        # Swap internals: any future acquire() targets the fresh RLock. The old
        # RLock object becomes garbage once no live thread references it — which
        # is guaranteed because we only call this after an acquire timeout (the
        # current thread was never granted it).
        self._lock = threading.RLock()
        self._owner_thread = None
        self._acquired_at = 0.0
        if was_held:
            logger.warning(
                f"[SECURITY] Execution lock force-reset (leaked by dead holder): {reason}"
            )
        return was_held


def _get_security_execution_lock(app):
    """Get (creating if needed) the app-level security execution lock.

    DEADLOCK FIX: Uses a ResettableRLock (an RLock wrapper) to allow reentrant
    acquisition during engine.run() while also recovering from a leaked lock left
    behind by a killed daemon thread. Protects the execution loop with acquire
    timeout semantics plus stale-holder detection.
    """
    if not hasattr(app, 'security_execution_lock'):
        app.security_execution_lock = ResettableRLock()
    return app.security_execution_lock


def _get_active_checks_state(app):
    """Get (creating if needed) active checks tracking set + its lock.

    Returns:
        (active_checks_set, active_checks_lock) tuple.
    """
    if not hasattr(app, 'active_security_checks'):
        app.active_security_checks = set()
    if not hasattr(app, 'active_security_checks_lock'):
        app.active_security_checks_lock = threading.Lock()
    return app.active_security_checks, app.active_security_checks_lock


def _get_auto_security_enabled(app) -> bool:
    """Check whether Auto-Ask security mode is still enabled."""
    return getattr(app, 'current_auto_security', True)


class SecurityAdvisorHandler:
    """Handles ask_security WebSocket messages.

    Lifecycle per check:
      1. Build prompt from approval data + workspace info
      2. Create unique Security agent instance (keyed by request_id)
      3. Run ExecutionEngine with streaming updates via broadcast helper
      4. Parse [YES]/[NO] verdict using multiple fallback strategies
      5. Auto-approve or auto-reject based on verdict
      6. Clean up instance state

    Thread-safe: uses two RLock-based locks for deadlock prevention:
      - security_prompt_lock (via _get_security_check_lock): protects prompt building phase,
        allows reentrant acquisition if Security agent triggers nested check.
      - security_execution_lock: protects engine execution loop with acquire timeout,
        prevents permanent block on crash and supports reentrancy.
    Plus active_security_checks tracking set to prevent duplicate/overlapping checks.
    """

    # ── Constructor ───────────────────────────────────────────────────────
    def __init__(
        self,
        agent_pool,                         # AgentPool instance
        session: Dict[str, Any],            # Session dict (source of truth for session state)
        app_state,                          # FastAPI app object (holds locks/semaphores)
        send_queue,                         # asyncio.Queue for WebSocket sends
        broadcast_fn,                       # async broadcast(data) -> None  (websocket sender)
    ):
        self.agent_pool = agent_pool
        self.session = session
        self.app_state = app_state
        self.send_queue = send_queue

    # ── Public entry point (async — spawns a background thread for the check) ──
    async def run_check(self, data: dict) -> None:
        """Execute a security advisor check.

        Spawns a daemon thread that runs the full check lifecycle.
        Duplicate request_id checks are guarded by an active-checks tracking set.

        Args:
            data: The parsed WebSocket message payload containing request_id,
                  auto_apply flag, and optionally target_agent.
        """
        # Lazy imports — avoid top-level circular dependencies
        from agent_cascade.log import logger
        from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT

        instance_name = self.session.get('session_name', 'Maine')
        inst = self.agent_pool.get_instance(instance_name) if self.agent_pool else None

        # ── Determine target instance for the security check ───────────────
        sec_target = data.get('target_agent') or instance_name
        sec_inst = (
            self.agent_pool.get_instance(sec_target)
            if (self.agent_pool and sec_target != instance_name) else inst
        )

        # ── Get pending approvals ──────────────────────────────────────────
        pending = self.agent_pool.operation_manager.list_pending_approvals()

        rid = data.get('request_id')
        auto_apply = data.get('auto_apply', False)

        if not rid:
            ap_list = pending
        else:
            ap_list = next((a for a in pending if a.get('request_id') == rid), None)

        if not ap_list:
            return

        ap = ap_list  # Check the first matching approval
        rid = ap['request_id']

        # Resolve the true caller from the approval (the agent that requested the tool).
        # Fall back to session name if missing or not in pool. This fixes deadlock where
        # Security always inherited slot 0 instead of the caller's endpoint.
        caller_agent = ap.get('agent_name')
        if not caller_agent or (self.agent_pool and self.agent_pool.get_instance(caller_agent) is None):
            caller_agent = instance_name

        # Duplicate check guard — prevent overlapping checks for the same request
        active_checks, checks_lock = _get_active_checks_state(self.app_state)
        with checks_lock:
            if rid in active_checks:
                logger.debug(f"Security check already active for request {rid}, ignoring duplicate.")
                return
            active_checks.add(rid)

        # ── Compute timeout from operation manager settings ──────────────
        if self.agent_pool and self.agent_pool.operation_manager:
            op_mgr = self.agent_pool.operation_manager
            if not op_mgr.enable_timeout:
                timeout_seconds = 3600  # Match request_user_approval behavior when disabled
            else:
                timeout_seconds = op_mgr.approval_timeout_seconds
        else:
            timeout_seconds = 3600  # Safe fallback
        timeout_seconds = max(10, timeout_seconds)
        warning_seconds = timeout_seconds * 2 / 3  # Warn at ~67% of timeout (matches 120/180 ratio)

        # Spawn background thread to run the full check lifecycle
        threading.Thread(
            target=self._run_check_worker,
            args=(ap, sec_inst, rid, auto_apply, instance_name, caller_agent,
                  SECURITY_ADVISOR_PROMPT,
                  timeout_seconds,
                  warning_seconds),
            daemon=True,
        ).start()

    # ── Worker function (runs in the spawned thread) ───────────────────────
    def _run_check_worker(
        self, ap: dict, sec_inst, rid: str, auto_apply: bool,
        instance_name: str, caller_agent: str, prompt_template: str,
        timeout_seconds: float, warning_seconds: float,
    ) -> None:
        """Background thread worker — executes the full security check lifecycle."""
        from agent_cascade.log import logger

        _worker_thread = threading.current_thread()
        logger.debug(
            f"[SECURITY] Check worker started for request {rid}, "
            f"thread={_worker_thread.name} (id={_worker_thread.ident})"
        )
        logger.info(f"[SECURITY] Checking request {rid} for tool '{ap.get('tool_name', 'unknown')}'")

        try:
            self._execute_check(
                ap, sec_inst, rid, auto_apply, instance_name, caller_agent,
                prompt_template, timeout_seconds, warning_seconds,
            )
        except Exception as e:
            logger.error(f"Security check failed: {e}")
            if auto_apply:
                self.agent_pool.operation_manager.user_reject(rid, f"Security check error: {e}")
            else:
                loop = _get_ws_loop(self.agent_pool)
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        self.send_queue.put({
                            'type': 'security_response',
                            'response': f"Error during security check: {e}",
                        }),
                        loop,
                    )
        finally:
            # Traces whether the worker thread reached its natural end. If a killed
            # daemon thread skips this line, the execution lock it held is leaked —
            # the stale-holder recovery in _execute_check will detect and reset it.
            logger.debug(f"[SECURITY] Check worker finished for request {rid}")

    # ── Core execution (extracted from the ~400-line inline block) ────────
    def _execute_check(
        self,
        ap: dict,
        sec_inst,
        rid: str,
        auto_apply: bool,
        instance_name: str,
        caller_agent: str,
        prompt_template: str,
        timeout_seconds: float,
        warning_seconds: float,
    ) -> None:
        """Run the full security check lifecycle.

        This is the meat of the handler — prompt building, engine creation,
        streaming execution loop, verdict parsing, auto-apply/reject, and cleanup.
        """
        from agent_cascade.log import logger
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.api_integration import broadcast_stream_update
        from agent_cascade.utils.thinking_block import (
            _THINK_BLOCK_RE, _THINK_BLOCK_BRACKET_RE,
            _MARKDOWN_BOLD_RE, _JUSTIFICATION_PREFIX_RE,
        )

        sec_state_key = None
        sec_instance = None
        sec_warning_timer = None       # Track for cleanup in finally block
        sec_first_yield_timer = None   # Last-resort guard against a hung generator (FIX 1)

        sec_prompt_lock = _get_security_check_lock(self.app_state)
        active_checks, checks_lock = _get_active_checks_state(self.app_state)

        # Fix 6 — Import outside lock block to avoid holding lock during import resolution
        from agent_cascade.constants import NON_LLM_KEYS, DEFAULT_SECURITY_DISABLED_TOOLS
        from agent_cascade.utils import merge_disabled_tools_for_auto_agent
        from agent_cascade.settings import SECURITY_AGENT_MAX_TURNS

        try:
            # ── Build prompt inside lock to prevent race conditions ────────
            with sec_prompt_lock:
                workspace_info = f"Main workspace: {self.agent_pool.operation_manager.base_dir}\n"
                if self.agent_pool.operation_manager.extra_work_folders_ro:
                    extra = [str(p) for p in self.agent_pool.operation_manager.extra_work_folders_ro]
                    workspace_info += f"Additional RO folders: {', '.join(extra)}\n"
                if self.agent_pool.operation_manager.extra_work_folders_rw:
                    extra = [str(p) for p in self.agent_pool.operation_manager.extra_work_folders_rw]
                    workspace_info += f"Additional RW folders: {', '.join(extra)}\n"

                prompt = prompt_template.format(
                    tool_name=ap.get('tool_name', 'unknown'),
                    description=ap.get('description', ''),
                    arguments=json.dumps(ap.get('tool_args', {})),
                    os_info=f"{platform.system()} {platform.release()}",
                    workspace_info=workspace_info,
                )

                # Unique instance name per request_id to prevent state corruption
                sec_state_key = f'Security_{rid}'  # e.g., 'Security_op_091f048b'

                # Create engine and instance INSIDE the lock (prevents lifecycle collisions)
                engine = ExecutionEngine(self.agent_pool)
                # caller=caller_agent attributes this check to the true caller for
                # logging/telemetry and drives the yield/reacquire slot pattern below.
                # (Security resolves its OWN endpoint pool — no caller inheritance.)
                sec_instance = engine._create_system_agent(
                    agent_class='Security',
                    instance_name=sec_state_key,
                    task=prompt,
                    caller=caller_agent,
                )

                # Primary bound: turn budget (see top-of-file NOTE for the full design).
                sec_instance.max_turns = SECURITY_AGENT_MAX_TURNS

                # Configure with UI settings (defense-in-depth tool filtering)
                ui_cfg = copy.deepcopy(self.session.get('generate_cfg', {}))
                llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                if 'disabled_tools' in ui_cfg:
                    llm_safe_cfg['disabled_tools'] = ui_cfg['disabled_tools']
                existing_disabled = llm_safe_cfg.get('disabled_tools', [])
                llm_safe_cfg['disabled_tools'] = merge_disabled_tools_for_auto_agent(
                    existing_disabled, 'Security', DEFAULT_SECURITY_DISABLED_TOOLS
                )

                template = self.agent_pool.get_template('Security')
                if template and hasattr(template, 'llm'):
                    cfg = (template.llm.generate_cfg or {}).copy()
                    cfg.update(llm_safe_cfg)
                    sec_instance._generate_cfg_override = cfg
                else:
                    logger.warning(f"[SECURITY] Template missing for '{sec_state_key}'")
                    sec_instance._generate_cfg_override = {
                        'disabled_tools': llm_safe_cfg.get('disabled_tools', [])
                    }

                logger.info(f"[SECURITY] Created AgentInstance '{sec_state_key}' for request {rid}")

                # Result-tracking flags. These are set True ONLY on a pre-first-yield hang:
                # if the LLM generator never yields its first token, the first-yield timer
                # (below) fires and we break out here so _handle_result can auto-reject.
                # Normal completion is governed by the turn budget (sec_instance.max_turns).
                sec_timeout_reached = False
                sec_elapsed_at_timeout = None
                sec_start_time = time.monotonic()

                # Schedule warning timer (tracked for cleanup)
                def _sec_warning_injector():
                    try:
                        self.agent_pool.enqueue_message(
                            sec_state_key,
                            "[SYSTEM WARNING] Your analysis is taking longer than expected. "
                            "Please provide a verdict as soon as possible — the approval request may timeout soon.",
                        )
                    except Exception as e:
                        logger.debug(f"Security advisor warning injection failed (non-critical): {e}")

                sec_warning_timer = threading.Timer(warning_seconds, _sec_warning_injector)
                sec_warning_timer.daemon = True
                sec_warning_timer.start()

            # ── Slot yield for Security advisor ────────────────────────────
            # Yield the caller's slot so the Security agent can acquire its own via
            # the normal engine.run() path (FIFO). The caller is blocked on this
            # check, so it cannot make LLM calls while the slot is free. We re-acquire
            # for the caller in the finally block below (reacquire_for).
            # NOTE: the actual release happens INSIDE the try block after exec_lock is
            # acquired — if lock acquisition fails we raise before yielding, so there's
            # nothing to reacquire (no lost-slot window).
            caller_inst_sec = self.agent_pool.get_instance(caller_agent) if caller_agent else None
            _yielded_slot = False

            # Fix 4 — Defensive fallback: ensure execution lock exists before using it.
            # DEADLOCK FIX #2: Use RLock instead of Semaphore(1).
            # Note: This is a SEPARATE lock from sec_prompt_lock (_get_security_check_lock).
            # sec_prompt_lock protects the prompt-building phase (short hold, released before execution).
            # security_execution_lock protects the execution loop (longer hold with its own timeout semantics).
            # Both are RLocks to handle nested security checks safely.
            exec_lock = _get_security_execution_lock(self.app_state)

            # DEADLOCK FIX #3: Acquire with timeout instead of blocking forever.
            # If a previous check crashed without releasing, we don't want to hang indefinitely.
            acquired = exec_lock.acquire(
                timeout=SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS
            )
            if not acquired:
                # LEAK RECOVERY: the lock timed out. Distinguish between:
                #   (a) a LIVE holder — another check is genuinely running; we must NOT
                #       steal its lock, so raise as before (caller cleans up + resubmits).
                #   (b) a DEAD holder — a previous daemon thread was killed before reaching
                #       release(), leaking the lock. Reset it so this check can proceed.
                #
                # Defensive: only a ResettableRLock exposes owner tracking. If a plain
                # RLock is present (e.g. injected by tests, or legacy state), we cannot
                # tell live from dead — treat it conservatively as LIVE and raise, which
                # preserves the original timeout behavior without risking a spurious reset.
                if isinstance(exec_lock, ResettableRLock) and not exec_lock.owner_is_alive:
                    logger.warning(
                        f"[SECURITY] Execution lock held >{SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s "
                        f"by a dead holder for request {rid}. A previous check was likely killed "
                        f"without releasing the lock — force-resetting to recover."
                    )
                    exec_lock.force_reset(
                        reason=f"dead-holder leak detected on acquire timeout for request {rid}"
                    )
                    # Re-acquire on the fresh lock. Should succeed immediately since we
                    # just swapped in a brand-new RLock with no other live holders.
                    acquired = exec_lock.acquire(timeout=1.0)
                    if not acquired:
                        raise RuntimeError(
                            f"[SECURITY] Failed to acquire security execution lock even after "
                            f"reset for request {rid}. A live check is contending; manual restart "
                            f"may be required."
                        )
                else:
                    # Live holder (or untracked lock) — do not steal. Raise as before.
                    raise RuntimeError(
                        f"[SECURITY] Failed to acquire security execution lock within "
                        f"{SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s for request {rid}. "
                        f"A previous check is still running (live holder). "
                        f"Manual restart may be required."
                    )

            try:
                # Telemetry: track Security agent call latency (non-blocking)
                _call_start = time.perf_counter()

                # ── Slot yield (inside try, after exec_lock acquired) ────────
                # Release the caller's slot now that we hold exec_lock. Doing this here
                # (rather than before lock acquisition) guarantees the finally block's
                # reacquire always has a matching yield — no lost-slot window if the
                # lock acquire above timed out and raised.
                # Atomic check-and-mark: decide to yield under the instance's state lock so
                # we never act on a stale observation. _release_slot() re-checks under the
                # same lock and is idempotent — if another thread (e.g. stop_session) already
                # released the slot, it is a no-op. We still reacquire in finally to restore
                # the caller's slot so it can continue.
                if caller_inst_sec and hasattr(caller_inst_sec, '_state_lock'):
                    with caller_inst_sec._state_lock:
                        if getattr(caller_inst_sec, "_slot_release", None) is not None:
                            _yielded_slot = True  # Mark BEFORE releasing (under lock)

                if _yielded_slot:
                    logger.debug(
                        f"[SECURITY_SLOT_YIELD] Releasing slot for '{caller_agent}' before Security check"
                    )
                    engine._release_slot(caller_inst_sec, caller_agent, "before_security_check")

                # Last-resort guard against a generator that never yields its first token.
                # The turn budget is the primary mechanism; this timer only covers the
                # pre-first-yield gap the engine watchdog cannot see. Cancelled on first yield.
                _first_yield_timeout_event = threading.Event()

                def _first_yield_timeout_trigger():
                    logger.warning(
                        f"[SECURITY] First-yield timeout trigger fired for request {rid} "
                        f"after {SECURITY_FIRST_YIELD_TIMEOUT_SECONDS}s — model has not yielded."
                    )
                    _first_yield_timeout_event.set()

                sec_first_yield_timer = threading.Timer(
                    SECURITY_FIRST_YIELD_TIMEOUT_SECONDS, _first_yield_timeout_trigger
                )
                sec_first_yield_timer.daemon = True
                sec_first_yield_timer.start()

                # ── Engine execution loop with streaming ───────────────────
                _last_sec_send = 0.0
                _sec_tick_num = 0
                _sec_last_resp_len = 0

                _got_first_yield = False
                for resp in engine.run(sec_instance):
                    if self.agent_pool.stopped:
                        break

                    # First-yield guard: fires only if the generator never yielded a token.
                    # Once we get any yield, cancel the timer — the engine watchdog + turn
                    # budget take over from here on.
                    if not _got_first_yield:
                        _got_first_yield = True
                        try:
                            sec_first_yield_timer.cancel()
                        except Exception:
                            pass  # Timer may have already fired

                        if _first_yield_timeout_event.is_set():
                            sec_timeout_reached = True
                            sec_elapsed_at_timeout = time.monotonic() - sec_start_time
                            logger.warning(
                                f"[SECURITY] First-yield timeout after {sec_elapsed_at_timeout:.0f}s "
                                f"for request {rid}. Generator did not yield in time."
                            )
                            break

                    now_sec = time.monotonic()

                    # Unpack (turn_output, is_streaming_tick) from engine.run() yield
                    if isinstance(resp, tuple) and len(resp) == 2:
                        sec_turn_output, sec_is_streaming_tick = resp
                    else:
                        sec_turn_output, sec_is_streaming_tick = resp, False

                    # WebSocket broadcast for Security agent (shared helper)
                    _last_sec_send, _sec_last_resp_len = broadcast_stream_update(
                        pool=self.agent_pool,
                        instance_name=sec_state_key,
                        turn_output=sec_turn_output,
                        is_streaming_tick=sec_is_streaming_tick,
                        tick_num=_sec_tick_num,
                        now_sec=now_sec,
                        last_send=_last_sec_send,
                        last_resp_len=_sec_last_resp_len,
                    )

                    _sec_tick_num += 1

                    # Update instance_state for UI visibility (thread-safe)
                    with self.agent_pool._execution._state_lock:
                        if sec_state_key in self.agent_pool.instance_state:
                            self.agent_pool.instance_state[sec_state_key]['message_count'] = len(sec_instance.conversation)

            except Exception as e:
                logger.error(f"Security agent execution error: {e}")
                raise
            finally:
                # Telemetry: record Security agent instance call (non-blocking, always fires even on timeout/error)
                _call_latency_ms = (time.perf_counter() - _call_start) * 1000
                if (tel := engine._telemetry()) is not None:
                    try:
                        tel.record_agent_instance_call(
                            sec_state_key, "Security", caller_agent, latency_ms=_call_latency_ms,
                        )
                    except Exception:
                        pass

                # Re-acquire the caller's slot if we yielded it before running Security.
                # Runs inline on the caller's thread, so yield→run→reacquire is in-order.
                if _yielded_slot and caller_inst_sec is not None:
                    if not engine.reacquire_for(caller_inst_sec, caller_agent, "after_security_check"):
                        # Already logged inside reacquire_for — just note the degraded state
                        logger.warning(
                            f"[SECURITY] Caller '{caller_agent}' is slotless after Security check. "
                            f"Subsequent LLM calls will use async path only."
                        )

                # Release concurrency lock for Security checks
                exec_lock.release()

            # ── Extract output and parse verdict ───────────────────────────
            from agent_cascade.compression.helpers import extract_instance_output
            parsing_response = extract_instance_output(sec_instance.conversation, sec_state_key)

            is_yes, is_no, justification = self._parse_verdict(parsing_response)

            # ── Handle result: timeout / verdict / ambiguous ───────────────
            loop = _get_ws_loop(self.agent_pool)
            self._handle_result(
                rid, auto_apply, sec_state_key, parsing_response,
                is_yes, is_no, justification,
                sec_timeout_reached, sec_elapsed_at_timeout,
                timeout_seconds, loop,
            )

        except RuntimeError as e:
            # DEADLOCK FIX #2b: If execution lock acquire times out and raises RuntimeError,
            # we need to clean up active_checks before re-raising. The rid was added in run_check()
            # but _cleanup won't be called if we never created sec_state_key.
            logger.error(f"[SECURITY] Request {rid} failed: {e}")
            # Clean up active_checks entry even if sec_state_key was never created
            with checks_lock:
                active_checks.discard(rid)
            raise

        finally:
            # ── Timer cleanup (CRITICAL — must always run) ──────────────────
            # sec_warning_timer is created inside the prompt-building lock but may leak if
            # an exception occurs before the execution lock's finally block. Cancel here to be safe.
            if sec_warning_timer is not None:
                try:
                    sec_warning_timer.cancel()
                except Exception:
                    pass  # Timer may have already fired

            # First-yield guard timer — cancel if it hasn't fired (already cancelled on first yield).
            if sec_first_yield_timer is not None:
                try:
                    sec_first_yield_timer.cancel()
                except Exception:
                    pass  # Timer may have already fired or been cancelled

            # ── Cleanup: always remove instance state and release tracking ──
            self._cleanup(sec_state_key)

    # ── Verdict parsing (multiple fallback strategies) ────────────────────
    def _parse_verdict(self, text: str) -> tuple[bool, bool, str]:
        """Parse the security advisor response for [YES]/[NO] verdict.

        Uses multiple fallback strategies to handle various LLM output formats:
          1. Check last non-empty line for [YES]/[NO] prefix (primary)
          2. Fallback: single-word responses (YES/SAFE, NO/UNSAFE)
          3. Fallback: find the LAST occurrence of [YES]/[NO] in text

        Returns:
            (is_yes, is_no, justification) tuple.
        """
        from agent_cascade.log import logger
        from agent_cascade.utils.thinking_block import (
            _THINK_BLOCK_RE, _THINK_BLOCK_BRACKET_RE,
            _MARKDOWN_BOLD_RE, _JUSTIFICATION_PREFIX_RE,
        )

        # Clean thinking blocks before parsing
        clean_text = text
        try:
            if '<think' in clean_text.lower() or '<thought' in clean_text.lower():
                clean_text = _THINK_BLOCK_RE.sub('', clean_text)
            if '[think' in clean_text.lower() or '[thought' in clean_text.lower():
                clean_text = _THINK_BLOCK_BRACKET_RE.sub('', clean_text).strip()
        except Exception as e:
            logger.debug(f"Thinking block stripping failed (non-critical): {e}")

        is_yes = False
        is_no = False
        justification = ""

        try:
            # ── Strategy 1: Check last non-empty line ─────────────────────
            lines = [l.strip() for l in clean_text.split('\n') if l.strip()]
            last_line = lines[-1] if lines else ""

            # Remove markdown bolding (e.g. **[YES]** → [YES])
            last_line_clean = _MARKDOWN_BOLD_RE.sub('', last_line).strip()
            last_line_upper = last_line_clean.upper()

            is_yes = last_line_upper.startswith('[YES]')
            is_no = last_line_upper.startswith('[NO]')

            if is_yes:
                justification = last_line_clean[5:].strip()
            elif is_no:
                justification = last_line_clean[4:].strip()

            # Strip "Reason:", "Justification:", etc.
            if is_yes or is_no:
                justification = _JUSTIFICATION_PREFIX_RE.sub('', justification).strip()

            # ── Fallback 1: Single-word responses ────────────────────────
            if not is_yes and not is_no and len(lines) == 1:
                if last_line_upper == 'YES' or last_line_upper == 'SAFE':
                    is_yes = True
                    justification = last_line
                elif last_line_upper == 'NO' or last_line_upper == 'UNSAFE':
                    is_no = True
                    justification = last_line

            # ── Fallback 2: Find LAST [YES]/[NO] in text ─────────────────
            if not is_yes and not is_no:
                upper_text = clean_text.upper()
                yes_pos = upper_text.rfind('[YES]')
                no_pos = upper_text.rfind('[NO]')
                if yes_pos > no_pos:
                    is_yes = True
                elif no_pos > yes_pos:
                    is_no = True

                if is_yes or is_no:
                    # Extract justification from the matching line
                    for line in lines:
                        lc = _MARKDOWN_BOLD_RE.sub('', line).strip().upper()
                        if (is_yes and '[YES]' in lc) or (is_no and '[NO]' in lc):
                            just_text = lc.replace('[YES]', '', 1).replace('[NO]', '', 1).strip()
                            justification = _JUSTIFICATION_PREFIX_RE.sub('', just_text).strip()
                            break

        except Exception as e:
            logger.error(f"Error extracting security verdict: {e}")
            is_yes = False
            is_no = False
            justification = ""

        return is_yes, is_no, justification

    # ── Result handling (timeout / auto-apply / notify) ───────────────────
    def _handle_result(
        self, rid: str, auto_apply: bool, sec_state_key: str,
        parsing_response: str, is_yes: bool, is_no: bool,
        justification: str, timeout_reached: bool,
        elapsed_at_timeout: Optional[float], timeout_seconds: float, loop,
    ) -> None:
        """Handle the security check result.

        Routes to the appropriate action based on verdict and mode:
          - Timeout → auto-reject + UI notification
          - YES/NO with auto_apply → approve/reject + broadcast approvals
          - YES/NO without auto_apply → send verdict for manual confirmation
          - Ambiguous in auto-apply mode → reject + notify
        """
        from agent_cascade.log import logger

        if timeout_reached:
            self._handle_timeout(rid, auto_apply, elapsed_at_timeout, timeout_seconds)
        elif is_yes or is_no:
            self._handle_verdict(rid, auto_apply, is_yes, is_no, justification, parsing_response, loop)
        else:
            self._handle_ambiguous(rid, auto_apply, parsing_response, loop)

    def _handle_timeout(self, rid: str, auto_apply: bool, elapsed: float, timeout_seconds: float) -> None:
        """Handle security check timeout — reject and notify UI."""
        from agent_cascade.log import logger

        logger.info(
            f"[SECURITY] Timeout after {elapsed:.0f}s for request {rid}. "
            f"Auto-rejecting to prevent AFK rejection cascade."
        )

        # Halt the security advisor instance (best-effort)
        if self.agent_pool:
            self.agent_pool.halt_instance(f'Security_{rid}')

        reject_msg = (
            "SECURITY ADVISOR TIMEOUT: The security check took too long to complete. "
            "This may indicate an overly complex request or insufficient justification. "
            "Please resubmit the request with a clearer, more specific justification "
            "to help the security advisor reach a verdict faster."
        )
        self.agent_pool.operation_manager.user_reject(rid, reject_msg)

        # Notify UI about the timeout
        response_text = f"[TIMEOUT] Security check exceeded {timeout_seconds:.0f}s limit after {elapsed:.0f}s."
        if not auto_apply:
            response_text += " Please resubmit with clearer justification if needed."

        loop = _get_ws_loop(self.agent_pool)
        if loop:
            asyncio.run_coroutine_threadsafe(
                self.send_queue.put({
                    'type': 'security_response',
                    'request_id': rid,
                    'response': response_text,
                    'verdict': 'TIMEOUT',
                }),
                loop,
            )

            # Broadcast updated approval list so stale card is removed from frontend
            asyncio.run_coroutine_threadsafe(
                self.send_queue.put({
                    'type': 'approvals',
                    'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                }),
                loop,
            )

    def _handle_verdict(
        self, rid: str, auto_apply: bool, is_yes: bool, is_no: bool,
        justification: str, parsing_response: str, loop,
    ) -> None:
        """Handle a clear YES/NO verdict."""
        from agent_cascade.log import logger

        # Check if Auto-Ask is still enabled BEFORE auto-applying
        auto_ask_still_on = _get_auto_security_enabled(self.app_state)

        if auto_apply and auto_ask_still_on:
            if is_yes:
                logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
                self.agent_pool.operation_manager.user_approve(rid, reason=justification)
            else:
                logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
                reject_msg = justification or "The security advisor flagged this operation as unsafe."
                self.agent_pool.operation_manager.user_reject(rid, reject_msg)

            # Broadcast updated approvals list to UI after auto-apply
            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.send_queue.put({
                        'type': 'approvals',
                        'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                    }),
                    loop,
                )
        else:
            # Auto-Ask toggled off — send to UI for manual confirmation
            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.send_queue.put({
                        'type': 'security_response',
                        'request_id': rid,
                        'response': parsing_response,
                        'verdict': 'YES' if is_yes else 'NO',
                        'reason': justification if is_no else "",
                    }),
                    loop,
                )

    def _handle_ambiguous(
        self, rid: str, auto_apply: bool, parsing_response: str, loop,
    ) -> None:
        """Handle ambiguous verdict (no clear [YES]/[NO] found)."""
        from agent_cascade.log import logger

        if auto_apply:
            # Strict enforcement: Invalid format = Automatic NO
            logger.info(f"[SECURITY] Automatic Rejection for {rid} (Ambiguous/Invalid Format)")
            reject_msg = (
                "The security advisor provided an ambiguous response "
                "without a clear [YES] or [NO] verdict. Please try a different method or provide a clearer justification."
            )
            self.agent_pool.operation_manager.user_reject(rid, reject_msg)

            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.send_queue.put({
                        'type': 'security_response',
                        'request_id': rid,
                        'response': parsing_response + "\n\n**[AUTO-REJECTED: Ambiguous Format]**",
                        'verdict': 'AMBIGUOUS',
                    }),
                    loop,
                )
        else:
            logger.info(f"[SECURITY] Ambiguous response for {rid} in manual mode. Waiting for user decision.")
            if loop:
                asyncio.run_coroutine_threadsafe(
                self.send_queue.put({
                    'type': 'security_response',
                    'request_id': rid,
                    'response': parsing_response,
                    'verdict': 'AMBIGUOUS',
                }),
                loop,
            )

    # ── Cleanup ───────────────────────────────────────────────────────────
    def _cleanup(self, sec_state_key: Optional[str]) -> None:
        """Clean up security advisor instance state."""
        from agent_cascade.log import logger

        if not sec_state_key:
            return

        # Mark instance as inactive in instance_state
        if sec_state_key in self.agent_pool.instance_state:
            with self.agent_pool._execution._state_lock:
                self.agent_pool.instance_state[sec_state_key]['active'] = False

        try:
            self.agent_pool.active_stack_remove(sec_state_key)
        except Exception as e:
            logger.debug(f"Active stack removal failed for {sec_state_key} (non-critical): {e}")

        # Release active check tracking
        active_checks, checks_lock = _get_active_checks_state(self.app_state)
        if sec_state_key:
            with checks_lock:
                released_rid = sec_state_key.replace('Security_', '', 1)
                active_checks.discard(released_rid)
            logger.debug(f"[SECURITY] Released active check for {released_rid}")
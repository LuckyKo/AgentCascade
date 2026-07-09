"""Security advisor handler for tool approval checks.

Extracted from api_server.py ask_security block (~400 lines) as Phase 3 of the
API server refactoring plan.  Preserves exact behavior — identical verdict
parsing, timeout handling, auto-apply/reject logic, and cleanup sequence.
"""
import asyncio
import copy
import json
import platform
import time
import threading
from typing import Any, Dict, Optional

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
    """Get (creating if needed) the app-level security check lock."""
    if not hasattr(app, 'security_check_lock'):
        app.security_check_lock = threading.Lock()
    return app.security_check_lock


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

    Thread-safe: uses the app-level security_check_semaphore (Semaphore(1))
    and active_security_checks tracking set to prevent duplicate/overlapping checks.
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
        from agent_cascade.operation_manager import (
            SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS,
        )

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
            ap_list = [a for a in pending if a['request_id'] == rid]

        if not ap_list:
            return

        ap = ap_list[0]  # Check the first matching approval
        rid = ap['request_id']

        # Duplicate check guard — prevent overlapping checks for the same request
        active_checks, checks_lock = _get_active_checks_state(self.app_state)
        with checks_lock:
            if rid in active_checks:
                logger.debug(f"Security check already active for request {rid}, ignoring duplicate.")
                return
            active_checks.add(rid)

        # Spawn background thread to run the full check lifecycle
        threading.Thread(
            target=self._run_check_worker,
            args=(ap, sec_inst, rid, auto_apply, instance_name,
                  SECURITY_ADVISOR_PROMPT,
                  SECURITY_ADVISOR_TIMEOUT_SECONDS,
                  SECURITY_ADVISOR_WARNING_SECONDS),
            daemon=True,
        ).start()

    # ── Worker function (runs in the spawned thread) ───────────────────────
    def _run_check_worker(
        self, ap: dict, sec_inst, rid: str, auto_apply: bool,
        instance_name: str, prompt_template: str,
        timeout_seconds: float, warning_seconds: float,
    ) -> None:
        """Background thread worker — executes the full security check lifecycle."""
        from agent_cascade.log import logger

        logger.info(f"[SECURITY] Checking request {rid} for tool '{ap.get('tool_name', 'unknown')}'")

        try:
            self._execute_check(
                ap, sec_inst, rid, auto_apply, instance_name,
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

    # ── Core execution (extracted from the ~400-line inline block) ────────
    def _execute_check(
        self,
        ap: dict,
        sec_inst,
        rid: str,
        auto_apply: bool,
        instance_name: str,
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

        sec_lock = _get_security_check_lock(self.app_state)
        active_checks, checks_lock = _get_active_checks_state(self.app_state)

        # Fix 6 — Import outside lock block to avoid holding lock during import resolution
        from agent_cascade.constants import NON_LLM_KEYS, DEFAULT_SECURITY_DISABLED_TOOLS
        from agent_cascade.utils import merge_disabled_tools_for_auto_agent

        try:
            # ── Build prompt inside lock to prevent race conditions ────────
            with sec_lock:
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
                sec_instance = engine._create_system_agent(
                    agent_class='Security',
                    instance_name=sec_state_key,
                    task=prompt,
                    caller=self.session.get('session_name', 'Orchestrator'),
                )

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

                # Initialize timing variables
                sec_timeout_reached = False
                sec_elapsed_at_timeout = None
                sec_start_time = time.monotonic()

                # Schedule warning timer
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

            # ── Slot bypass for Security advisor ───────────────────────────
            caller_name_sec = self.session.get('session_name', 'Orchestrator')
            caller_inst_sec = self.agent_pool.get_instance(caller_name_sec) if caller_name_sec else None

            sec_instance._skip_slot_acquire = True
            logger.debug(
                f"[SECURITY_SLOT_BYPASS] Skipping slot acquire for Security - "
                f"caller={caller_name_sec}, caller_holds_slot={(getattr(caller_inst_sec, '_slot_release', None) is not None) if caller_inst_sec else False}"
            )

            # Fix 4 — Defensive fallback: ensure semaphore exists before using it
            if not getattr(self.app_state, 'security_check_semaphore', None):
                self.app_state.security_check_semaphore = threading.Semaphore(1)

            # Acquire concurrency semaphore (prevents unlimited parallelism)
            self.app_state.security_check_semaphore.acquire()
            try:
                # Telemetry: track Security agent call latency (non-blocking)
                _call_start = time.perf_counter()

                # ── Engine execution loop with streaming ───────────────────
                _last_sec_send = 0.0
                _sec_tick_num = 0
                _sec_last_resp_len = 0

                for resp in engine.run(sec_instance):
                    if self.agent_pool.stopped:
                        break

                    elapsed = time.monotonic() - sec_start_time
                    if elapsed > timeout_seconds:
                        sec_timeout_reached = True
                        sec_elapsed_at_timeout = elapsed
                        logger.warning(
                            f"[SECURITY] Timeout reached after {elapsed:.0f}s for request {rid}. "
                            f"Terminating security advisor to prevent AFK rejection."
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
                            sec_state_key, "Security", caller_name_sec, latency_ms=_call_latency_ms,
                        )
                    except Exception:
                        pass

                # Release concurrency semaphore for Security checks
                self.app_state.security_check_semaphore.release()
                sec_warning_timer.cancel()

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
                loop,
            )

        finally:
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
        elapsed_at_timeout: Optional[float], loop,
    ) -> None:
        """Handle the security check result.

        Routes to the appropriate action based on verdict and mode:
          - Timeout → auto-reject + UI notification
          - YES/NO with auto_apply → approve/reject + broadcast approvals
          - YES/NO without auto_apply → send verdict for manual confirmation
          - Ambiguous in auto-apply mode → reject + notify
        """
        from agent_cascade.log import logger
        from agent_cascade.operation_manager import SECURITY_ADVISOR_TIMEOUT_SECONDS

        if timeout_reached:
            self._handle_timeout(rid, auto_apply, elapsed_at_timeout)
        elif is_yes or is_no:
            self._handle_verdict(rid, auto_apply, is_yes, is_no, justification, parsing_response, loop)
        else:
            self._handle_ambiguous(rid, auto_apply, parsing_response, loop)

    def _handle_timeout(self, rid: str, auto_apply: bool, elapsed: float) -> None:
        """Handle security check timeout — reject and notify UI."""
        from agent_cascade.log import logger
        from agent_cascade.operation_manager import SECURITY_ADVISOR_TIMEOUT_SECONDS

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
        response_text = f"[TIMEOUT] Security check exceeded {SECURITY_ADVISOR_TIMEOUT_SECONDS}s limit after {elapsed:.0f}s."
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

            # Broadcast updated approval list after a brief delay
            async def _delayed_timeout_broadcast():
                await asyncio.sleep(0.15)
                await self.send_queue.put({
                    'type': 'approvals',
                    'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                })

            asyncio.run_coroutine_threadsafe(_delayed_timeout_broadcast(), loop)

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
                reject_msg = (
                    f"SECURITY REJECTED: {justification}" if justification
                    else "SECURITY REJECTED: The security advisor flagged this operation as unsafe."
                )
                self.agent_pool.operation_manager.user_reject(rid, reject_msg)

            # Send security_response to clear active check tracking on frontend
            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.send_queue.put({
                        'type': 'security_response',
                        'request_id': rid,
                        'response': parsing_response,
                        'verdict': 'YES' if is_yes else 'NO',
                        'reason': justification,
                    }),
                    loop,
                )

            # Broadcast updated approvals list after a brief delay to give the agent thread
            # time to submit the next tool for approval (prevents empty broadcast)
            async def _delayed_approvals_broadcast():
                await asyncio.sleep(0.15)
                await self.send_queue.put({
                    'type': 'approvals',
                    'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                })

            if loop:
                asyncio.run_coroutine_threadsafe(_delayed_approvals_broadcast(), loop)
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

                # Broadcast updated approvals list after a brief delay
                async def _delayed_verdict_broadcast():
                    await asyncio.sleep(0.15)
                    await self.send_queue.put({
                        'type': 'approvals',
                        'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                    })

                asyncio.run_coroutine_threadsafe(_delayed_verdict_broadcast(), loop)

    def _handle_ambiguous(
        self, rid: str, auto_apply: bool, parsing_response: str, loop,
    ) -> None:
        """Handle ambiguous verdict (no clear [YES]/[NO] found)."""
        from agent_cascade.log import logger

        if auto_apply:
            # Strict enforcement: Invalid format = Automatic NO
            logger.info(f"[SECURITY] Automatic Rejection for {rid} (Ambiguous/Invalid Format)")
            reject_msg = (
                "SECURITY VERIFICATION FAILED: The security advisor provided an ambiguous response "
                "without a clear [YES] or [NO] verdict. For safety, the operation has been automatically "
                "rejected. Please try a different method or provide a clearer justification."
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

                # Broadcast updated approvals list after a brief delay
                async def _delayed_ambiguous_broadcast():
                    await asyncio.sleep(0.15)
                    await self.send_queue.put({
                        'type': 'approvals',
                        'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                    })

                asyncio.run_coroutine_threadsafe(_delayed_ambiguous_broadcast(), loop)
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

                # Broadcast updated approvals list after a brief delay
                async def _delayed_ambiguous_manual_broadcast():
                    await asyncio.sleep(0.15)
                    await self.send_queue.put({
                        'type': 'approvals',
                        'approvals': self.agent_pool.operation_manager.list_pending_approvals(),
                    })

                asyncio.run_coroutine_threadsafe(_delayed_ambiguous_manual_broadcast(), loop)

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
                rid = sec_state_key.replace('Security_', '', 1)
                active_checks.discard(rid)
            logger.debug(f"[SECURITY] Released active check for {rid}")
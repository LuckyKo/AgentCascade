"""System-agent lifecycle helpers for the execution engine (Phase 3 split).

``SystemAgentMixin`` owns fresh/reused system-agent creation, reuse eligibility
and reset, and WebUI state updates. Method bodies are moved VERBATIM from
:mod:`agent_cascade.engine.core`; the mixin is composed into ``ExecutionEngine``
there so class-method lookups and instance patch points remain unchanged.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.engine.helpers import (_inject_skills_to_system_message, _merge_loaded_skill_names,
                                          _with_run_identity)
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, Message
from agent_cascade.log import logger
from agent_cascade.settings import (AUTO_SKILL_MODE_NONE, DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_AUTO,
                                    LOAD_SKILL_NONE)
from agent_cascade.utils.utils import msg_field

# Security instance reuse (BUG_0029 Phase 3): min settle gap before warm instance reset.
# Performance-only heuristic, NOT a correctness guard - an in-flight call is caught by Gate 5a
# (`_llm_call_active`), which is cleared synchronously at the 'end' stamp before any slot handoff.
# This margin just keeps back-to-back serialized checks from touching the instance in the instant
# after its last activity; real inter-check gaps run ~0.1-1s, so 0.1s lets them reuse while leaving
# a minimal tail buffer. (plan 3.4 item 5 / R1.)
_REUSE_IDLE_EPSILON = 0.1


def _is_explicit_skill_list(load_skill_value) -> bool:
    """Return True when a per-call load_skill value is an EXPLICIT skill list.

    Covers the two shapes callers/LLMs use to name specific skills:
      - a native Python list, e.g. ["docker", "httpx-connection-pooling"]
      - a JSON-encoded list string, e.g. '["docker"]' (LLMs sometimes emit this)
    Bare "AUTO"/"NONE"/None and any non-list string are NOT explicit lists —
    they fall through to auto-matching / no-skill handling instead.
    """
    if isinstance(load_skill_value, list):
        return True
    if isinstance(load_skill_value, str) and load_skill_value.strip().startswith('['):
        try:
            import json as _json_list_check
            return isinstance(_json_list_check.loads(load_skill_value), list)
        except (ValueError, TypeError):
            return False  # not valid JSON — treat as a plain string, not a list
    return False


class SystemAgentMixin:
    """Create, reuse, launch, and surface system agents."""

    def _create_and_run_agent(self,
                              agent_class: str,
                              instance_name: str,
                              args: dict,
                              caller: str,
                              nest_depth: int = 0,
                              force_fresh: bool = False) -> tuple:
        """Create an AgentInstance and run it through the unified loop.

        Shared helper used by both sync and parallel call_agent paths.
        Creates the instance, builds system + task messages, logs them,
        tracks in active_stack, and runs engine.run(inst).

        Returns:
            Tuple of (AgentInstance, conversation history).

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
            force_fresh: If True, always create new instance even if inactive one exists.
                        Used for Security/Compressor agents that should start fresh each time.
        """
        self._create_completed = False  # Reset for this execution cycle

        logger.debug(
            '[CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=%s, class=%s, caller=%s, '
            'nest_depth=%d, force_fresh=%s', instance_name, agent_class, caller, nest_depth, force_fresh)

        # BUG FIX (Bug 2): Extract log_file from args and pass through the
        # chain
        log_file = args.get('log_file')

        # ── Skill Advisor gate (AUTO Skill Helper — Advanced mode) ───────────
        # Runs BEFORE find_or_create_instance() so a DENY never allocates a child.
        _advisor_recommended_skills: Optional[List[str]] = None  # names, or None → use resolve_load_skill
        _advisor_task_notes: str = ''

        load_skill_value = args.get('load_skill')
        if load_skill_value is None:
            load_skill_value = getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE)
        # Normalize: LLM may pass a JSON-encoded list string like '["AUTO"]' or an actual list.
        # For the gate we only care whether it's AUTO vs NONE vs explicit-skill-names.
        if isinstance(load_skill_value, str) and load_skill_value.strip().startswith('['):
            try:
                import json as _json_gate
                _decoded = _json_gate.loads(load_skill_value)
                if isinstance(_decoded, list) and len(_decoded) == 1 and str(
                        _decoded[0]).upper() in (LOAD_SKILL_AUTO, LOAD_SKILL_NONE):
                    load_skill_value = str(_decoded[0])
            except (ValueError, TypeError):
                pass  # not valid JSON — treat as literal string below
        elif isinstance(load_skill_value, list) and len(load_skill_value) == 1:
            _single = str(load_skill_value[0]).strip().upper()
            if _single in (LOAD_SKILL_AUTO, LOAD_SKILL_NONE):
                load_skill_value = _single
        # Multi-element lists (e.g. ["AUTO", "docker"]) default to AUTO — the
        # presence of explicit skill names alongside AUTO means "auto + extras".
        load_skill_mode_upper = (load_skill_value.strip().upper() if isinstance(load_skill_value, str) else 'AUTO')
        auto_skill_mode = getattr(self.pool.settings, 'auto_skill_mode', 'basic')
        _skill_mgr_gate = getattr(self.pool, 'skill_manager', None)

        # Refresh skill list once (cache-respecting) so the gate's count check is fresh.
        try:
            if _skill_mgr_gate is not None:
                _skill_mgr_gate._ensure_discovered()
        except Exception as e:  # noqa: BLE001 — never block delegation on a discovery hiccup
            logger.debug('[SKILL-ADVISOR] gate _ensure_discovered failed: %s', e)

        # NOTE: auto_skill_mode == "none" never runs the advisor. The `== "advanced"`
        # check below already excludes it (only "advanced" passes), so no extra guard
        # is required — this comment documents that intent explicitly.
        should_run_advisor = (
            load_skill_mode_upper == LOAD_SKILL_AUTO and auto_skill_mode == 'advanced' and not force_fresh and
            log_file is None  # external load → skip advisor (needs fresh skills)
            and _skill_mgr_gate is not None and len(_skill_mgr_gate.get_skill_names()) > 0  # nothing to recommend
        )
        logger.debug(
            '[SKILL-ADVISOR] gate check: mode=%s, auto_skill_mode=%s, force_fresh=%s, '
            'log_file=%s, skill_mgr=%s, skills_count=%d → should_run=%s',
            load_skill_mode_upper,
            auto_skill_mode,
            force_fresh,
            log_file,
            _skill_mgr_gate is not None,
            len(_skill_mgr_gate.get_skill_names()) if _skill_mgr_gate else 0,
            should_run_advisor,
        )

        if should_run_advisor:
            # Early recall check replicates _is_recall below WITHOUT calling
            # find_or_create_instance(), since that would allocate a child before
            # we know whether to deny. Locks are released BEFORE the advisor's LLM
            # call. On failure we treat it as "new" — worst case the advisor runs
            # once for a recall, which is harmless.
            _is_early_recall = False
            try:
                with self.pool._execution._state_lock:
                    _existing_inst = self.pool.instances.get(instance_name)
                if _existing_inst is not None:
                    with _existing_inst._state_lock:
                        _early_state = _existing_inst.state
                    if _early_state in (AgentState.IDLE, AgentState.TERMINATED):
                        if _existing_inst.conversation and len(_existing_inst.conversation) > 0 \
                                and getattr(_existing_inst.conversation[0], 'role', None) == SYSTEM:
                            _is_early_recall = True
            except Exception as e:  # noqa: BLE001 — never block delegation on a check failure
                logger.debug('[SKILL-ADVISOR] Early recall check failed (treating as new): %s', e)

            if not _is_early_recall:
                from agent_cascade.skills.advisor import SkillAdvisorResult, run_skill_advisor
                _task_text = args.get('task', '')
                _context_text = args.get('context', '')
                try:
                    _advisor_result = run_skill_advisor(
                        pool=self.pool,
                        skill_manager=_skill_mgr_gate,
                        task_text=_task_text,
                        context_text=_context_text,
                        agent_class=agent_class,
                        caller_name=caller,
                    )
                except Exception as e:  # noqa: BLE001 — advisor must never break delegation
                    logger.error('[SKILL-ADVISOR] Unexpected error; falling back to basic match: %s', e)
                    _advisor_result = SkillAdvisorResult(verdict='ambiguous', reason=f"unexpected: {e}")

                # Telemetry (best-effort, non-blocking)
                try:
                    tel = self._telemetry()
                    if tel is not None and hasattr(tel, 'record_skill_advisor_decision'):
                        tel.record_skill_advisor_decision(
                            instance_name=instance_name,
                            verdict=_advisor_result.verdict,
                            skill_count=len(_advisor_result.recommended_skills),
                            latency_ms=_advisor_result.latency_ms,
                            was_fallback=not _advisor_result.is_usable,
                        )
                except Exception:  # noqa: BLE001
                    pass

                if _advisor_result.verdict == 'deny':
                    logger.warning(
                        '[SKILL-ADVISOR] DENIED delegation to %s: %s',
                        instance_name,
                        _advisor_result.reason,
                    )
                    # No child instance is created — return an error the caller can surface.
                    # Use ASSISTANT role so extract_instance_output() picks up the text
                    # (FUNCTION role triggers the "terminated with tool result" guard).
                    return None, [
                        Message(
                            role=ASSISTANT,
                            content=(f"[SKILL-ADVISOR DENIED] {_advisor_result.reason} "
                                     f"Consider handling this task yourself or rephrasing with more specific context."))
                    ]

                if _advisor_result.verdict == 'approve':
                    _advisor_recommended_skills = list(_advisor_result.recommended_skills)
                    _advisor_task_notes = _advisor_result.task_notes or ''
                    logger.info(
                        '[SKILL-ADVISOR] APPROVED delegation to %s (skills=%s, notes=%d chars)',
                        instance_name,
                        _advisor_recommended_skills,
                        len(_advisor_task_notes),
                    )
                else:  # "ambiguous" → fall back to basic keyword match below
                    logger.warning(
                        '[SKILL-ADVISOR] Ambiguous result for %s (%s) — falling back to basic match',
                        instance_name,
                        _advisor_result.reason,
                    )

        # Phase 4.1: Delegate to lifecycle manager for instance creation/reuse
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(agent_class,
                                                                                    instance_name,
                                                                                    caller,
                                                                                    nest_depth,
                                                                                    force_fresh,
                                                                                    log_file=log_file)

        # ── System message + skills handling ────────────────────────────────────
        # Skill refresh happens ONLY on a rebuild: a NEW instance or an external load
        # (reload-from-file after termination) — both handled in the else branch below,
        # which does a full build_system_message + skill injection. On recall of an
        # existing idle agent we keep conversation[0] byte-for-byte verbatim (no skill
        # refresh, no mutation of any kind) and ignore the per-call load_skill — this
        # preserves the LLM prefix cache so a recall is a cheap cache-hit.
        # A recall must NOT be an external load (log_file restore): that returns
        # is_reuse=True AND session_was_loaded=True and still needs build + skill injection.
        _is_recall = is_reuse and not session_was_loaded and bool(inst.conversation) \
            and getattr(inst.conversation[0], 'role', None) == SYSTEM
        task_text = args.get('task', '')
        skill_manager = getattr(self.pool, 'skill_manager', None)
        context_text = args.get('context', '')  # only used in else branch for resolve_load_skill
        # Skills resolved/injected this run (name, body) pairs. Initialized here so it is always
        # bound even on the recall path (which skips resolution and keeps the system prompt
        # verbatim) — the auto-skill call site below reads it to build the reflection prompt.
        loaded_skills = []
        # Skill Advisor (Advanced mode): append its improved notes to the context so
        # they reach the child agent via build_task_message. Only on fresh instances —
        # recall preserves conversation[0] verbatim and never rebuilds the task message.
        if _advisor_task_notes and not _is_recall:
            context_text = f"{context_text}\n\n[Skill Advisor notes] {_advisor_task_notes}".strip()
        if _is_recall:
            # Reuse path (active-but-IDLE instance): keep conversation[0] EXACTLY as-is —
            # byte-for-byte, no skill refresh, no mutation of any kind. This preserves the
            # LLM prefix cache so a recall is a cheap cache-hit, not a full reprocess.
            # Skill updates are picked up only on a rebuild: a NEW instance or an external
            # load (reload-from-file after termination) — both handled in the else branch
            # below, which does a full build_system_message + skill injection.
            # The per-call load_skill arg is still ignored on recall.
            sys_msg = inst.conversation[0]
            logger.debug(
                '[SKILLS] Recall of %s: system prompt kept verbatim (no skill refresh; '
                'load_skill ignored on recall)',
                instance_name,
            )
        else:
            # New instance OR external load (session restored from log_file) OR a
            # defensive fallback when a reused instance has no valid system message.
            # Build a fresh system message and resolve/inject skills.
            # Use inst.agent_class (may differ from caller's agent_class if session
            # was loaded from log file)
            sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)

            # ── Skills System: Resolve load_skill and inject into sys_msg ─────
            # (task_text / context_text / skill_manager hoisted above the branch)

            # GLOBAL "Enable skills" setting (UI toggle → pool.settings.
            # default_load_skill_mode: 'AUTO'=ON, 'NONE'=OFF). This is the single
            # source of truth for whether Self-Augmentation is present.
            global_skills_enabled = \
                getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) != LOAD_SKILL_NONE

            # Per-call load_skill controls ONLY which MATCHED/auto skills load.
            # It never removes Self-Augmentation while the global toggle is ON.
            load_skill_value = args.get('load_skill')
            if load_skill_value is None:
                # No explicit per-call arg → fall back to the global setting.
                load_skill_value = getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE)

            loaded_skills = []
            if skill_manager and global_skills_enabled:
                # (1) Matched/auto skills — gated by the PER-CALL load_skill arg.
                #     NONE → no matched skills; otherwise resolve as before.
                if isinstance(load_skill_value, str):
                    load_skill_mode_upper = load_skill_value.strip().upper()
                else:
                    load_skill_mode_upper = 'AUTO'
                if load_skill_mode_upper != LOAD_SKILL_NONE and auto_skill_mode != AUTO_SKILL_MODE_NONE:
                    # Basic ("basic") / Advanced ("advanced") behavior — unchanged.
                    if _advisor_recommended_skills is not None:
                        # Skill Advisor (Advanced mode) APPROVED — use its semantic
                        # recommendations instead of basic keyword matching. Names were
                        # already validated against the registry by the advisor.
                        _advisor_loaded_names = []
                        for _name in _advisor_recommended_skills:
                            _body = skill_manager.load_full_instructions(_name)
                            if _body is None:
                                # Skill was removed from the registry between the advisor
                                # run and injection — skip it (Self-Augmentation still loads).
                                logger.warning(
                                    "[SKILL-ADVISOR] Recommended skill '%s' no longer available; skipping",
                                    _name,
                                )
                            elif (_name, _body) not in loaded_skills:
                                loaded_skills.append((_name, _body))
                                _advisor_loaded_names.append(_name)
                        # Telemetry: capture advisor-approved skills (init path only).
                        if _advisor_loaded_names:
                            _tel = getattr(self.pool, 'telemetry', None)
                            if _tel is not None:
                                _tel.record_skills_loaded(inst.agent_class, _advisor_loaded_names, 'advisor')
                    else:
                        try:
                            loaded_skills = skill_manager.resolve_load_skill_pairs(load_skill_value, task_text,
                                                                                   context_text)
                        except Exception as e:
                            logger.warning('[SKILLS] Failed to resolve skills for %s: %s', instance_name, e)
                            loaded_skills = []
                        # Telemetry: capture resolved (explicit/auto) skills. Names come
                        # from the same pairs used for injection so they match exactly.
                        _resolved_names = [name for name, _body in loaded_skills]
                        if _resolved_names:
                            _tel = getattr(self.pool, 'telemetry', None)
                            if _tel is not None:
                                _mode = 'explicit' if _is_explicit_skill_list(load_skill_value) else 'auto'
                                _tel.record_skills_loaded(inst.agent_class, _resolved_names, _mode)
                elif auto_skill_mode == AUTO_SKILL_MODE_NONE and _is_explicit_skill_list(load_skill_value):
                    # "none" mode: skip system auto-matching entirely (no Basic keyword
                    # match, no Advanced advisor). Caller-explicit skill lists are still
                    # honored. We delegate to resolve_load_skill() so it handles every
                    # explicit form consistently with the Basic path — native lists AND
                    # JSON-encoded list strings like '["docker"]' (which LLMs sometimes
                    # emit). _is_explicit_skill_list() is True only for those two shapes,
                    # so a bare "AUTO"/"NONE"/None never reaches here and no auto-matching
                    # leaks. A literal "AUTO" token inside an explicit list is resolved by
                    # name and silently skipped (it won't match a real skill) — existing
                    # resolve_load_skill behavior, preserved so it isn't surprising later.
                    try:
                        loaded_skills = skill_manager.resolve_load_skill_pairs(load_skill_value, task_text,
                                                                               context_text)
                    except Exception as e:
                        logger.warning('[SKILLS] Failed to resolve skills for %s: %s', instance_name, e)
                        loaded_skills = []
                    # Telemetry: capture explicit skills (this branch is explicit-only by
                    # construction — _is_explicit_skill_list gate above). Names come from
                    # the same pairs used for injection so they match exactly.
                    _resolved_names = [name for name, _body in loaded_skills]
                    if _resolved_names:
                        _tel = getattr(self.pool, 'telemetry', None)
                        if _tel is not None:
                            _tel.record_skills_loaded(inst.agent_class, _resolved_names, 'explicit')
                # else: "none" mode + AUTO/default → no auto-matched skills (loaded_skills stays [])

                # (2) Self-Augmentation — gated by the GLOBAL "Enable skills" toggle
                #     (global_skills_enabled), INDEPENDENT of the per-call load_skill arg.
                #     It is the meta-skill that enables runtime discovery and must be
                #     present whenever skills are globally ON, even if this call passed
                #     load_skill="NONE". _inject_skills_to_system_message is idempotent
                #     (skips when '## Active Skills' already exists).
                self_augmentation_instructions = skill_manager.load_full_instructions('self-augmentation')
                if (self_augmentation_instructions and
                    ('self-augmentation', self_augmentation_instructions) not in loaded_skills):
                    loaded_skills.append(('self-augmentation', self_augmentation_instructions))
                    # Telemetry: capture Self-Augmentation (init path only). The restore/
                    # runner paths record it in _inject_self_augmentation_skill instead —
                    # these are mutually exclusive, so each injection is counted once.
                    _tel = getattr(self.pool, 'telemetry', None)
                    if _tel is not None:
                        _tel.record_skills_loaded(inst.agent_class, ['self-augmentation'], 'self-augmentation')

            # Inject skills into system message using general-purpose helper.
            # When the global toggle is OFF (global_skills_enabled False),
            # loaded_skills stays empty → nothing is injected.
            _inject_skills_to_system_message(self.pool, sys_msg, loaded_skills if loaded_skills else None)

            # Record this run's resolved skill names on the instance so the
            # in-loop auto-skill trigger can render them in the reflection prompt
            # (plan §2.3). Recall path leaves it unset → prompt renders "(none)".
            # Merge prior (conversation-derived) names with this run's resolved skills so a
            # restored / externally-loaded instance — whose conversation already holds prior
            # runtime-injected skill user-messages — doesn't lose them on overwrite. A
            # brand-new instance has an empty conversation → prior == [] → byte-identical to
            # before this change (resolved only, or None when nothing resolved). The merge is a
            # shared helper so the exact expression is unit-testable in isolation.
            inst._loaded_skill_names = _merge_loaded_skill_names(
                inst.conversation, [name for name, _body in loaded_skills])

        # Write augmented context (with advisor notes) into a COPY of args so
        # build_task_message picks it up without mutating the caller's dict.
        # Only on fresh instances — recall preserves the original task message verbatim.
        if _advisor_task_notes and not _is_recall:
            args = dict(args)
            args['context'] = context_text

        # Build task message using lifecycle manager
        task_msg = self.lifecycle.build_task_message(args, caller)

        # Phase 4.1: Delegate to lifecycle manager for conversation
        # initialization
        conv = self.lifecycle.initialize_conversation(inst,
                                                      sys_msg,
                                                      task_msg,
                                                      is_reuse,
                                                      instance_name,
                                                      inst.agent_class,
                                                      from_external_load=session_was_loaded)

        # Phase 4.1: Delegate to lifecycle manager for settings propagation
        self.lifecycle.propagate_settings(inst, caller, inst.agent_class, call_agent_args=args)

        # Track in active stack with depth info (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append((instance_name, inst._nest_depth))

        # Item 12: Initialize sub-agent WebUI state before execution begins
        # (Fix
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, inst.agent_class, inst, conv, final_resp=[], is_initial=True)

        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)

        try:
            # Telemetry: track sub-agent call latency (non-blocking)
            _call_start = time.perf_counter()

            # Execute through unified loop — push stream_update events so the
            # frontend sees sub-agent tab updates independently of main agent
            # flow.
            # Without this, the main streaming loop is blocked during tool
            # execution
            # and no WebSocket events arrive until the sub-agent finishes.
            logger.debug('starting engine.run() for %s', instance_name)

            final_resp = []
            _update_counter = 0
            _last_sub_send = 0.0
            _sub_last_resp_len = 0
            _last_tick_suppressed = False  # FIX A: True if the most recent loop tick was throttled out (its state never reached the UI)
            _tick_num = 0

            # Bug
            if self.pool.is_instance_terminated(instance_name):
                logger.info('instance %s terminated before execution - skipping', instance_name)
                # Clear leftover queued messages to prevent accumulation
                q = self.pool.message_queues.get(instance_name)
                if q:
                    q.clear()
                return inst, []

            # Bind the generator so we can close it deterministically on early
            # break (same pattern as core.py:691-692). Without close(), a break
            # on terminal stop leaves run() suspended before its exit finally,
            # so the child stays RUNNING and any re-entry trips the L1 guard.
            # Hoisted from the loop below: importing at module level would create a
            # circular dependency (api_integration_pkg.runner → execution_engine facade
            # → engine.core, which is still mid-load). Importing once here — outside the
            # per-tick loop — avoids re-executing the import on every iteration while
            # staying cycle-safe. Imports directly from streaming.py to skip the
            # api_integration facade re-export chain.
            from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update

            _run_gen = self.run(inst)
            try:
                for resp in _run_gen:
                    now_mono = time.monotonic()  # Single clock read per tick (probe timing + throttle)
                    # Inner run() loop handles compression-halt via cooperative wait at Site 3.
                    # Only break here on terminal stops (which cause run() to yield final state and end).
                    if self._is_terminal_stop(instance_name):
                        break

                    # FIX BOOL_LEAK: Unpack (messages, is_streaming) tuple from
                    # engine.run()
                    # engine.run() yields tuples like (List[Message], bool), but we
                    # only need the message list
                    if isinstance(resp, tuple) and len(resp) == 2:
                        final_resp, is_streaming_tick = resp
                    else:
                        final_resp, is_streaming_tick = resp, False

                    # Item 12: Throttled sub-agent WebUI state update (every 5
                    # turns) — Fix
                    _update_counter += 1
                    if _update_counter % 5 == 0:
                        # Issue Y2: Use shared helper method instead of duplicated
                        # logic
                        current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                        self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

                    # ── Push stream_update to frontend during sub-agent execution ──
                    _last_sub_send, _sub_last_resp_len = broadcast_stream_update(
                        pool=self.pool,
                        instance_name=instance_name,
                        turn_output=final_resp,
                        is_streaming_tick=is_streaming_tick,
                        tick_num=_tick_num,
                        now_sec=now_mono,
                        last_send=_last_sub_send,
                        last_resp_len=_sub_last_resp_len,
                        yield_time=now_mono,
                    )
                    # FIX A: a suppressed tick returns its (stale) last_send unchanged, while a real
                    # broadcast returns now_sec (= now_mono). Compare against the current tick's own
                    # timestamp so detection is robust even if two consecutive sends land on the same
                    # monotonic resolution tick (avoids a false "suppressed" → spurious extra frame).
                    _last_tick_suppressed = (_last_sub_send != now_mono)
                    _tick_num += 1
            finally:
                # Deterministic generator cleanup: close() forces the suspended
                # run() generator to unwind its exit finally (RUNNING→IDLE), so
                # the child instance is never left in RUNNING state. Idempotent.
                # Guard with hasattr: engine.run() may be mocked in tests to return a
                # plain iterator (no .close()); only real generators need closing.
                try:
                    if hasattr(_run_gen, 'close'):
                        _run_gen.close()
                except RuntimeError:
                    pass  # Already closed/exhausted

            # FIX MSG_COUNT_BUG: Removed conv.extend(final_resp) to prevent
            # duplicate messages.
            # Messages are already added to instance.conversation during
            # engine.run() via _process_response().
            # Note: For new instances, rebuild_conversation() creates a copy of
            # conv, so they are NOT the same
            # reference — only reused instances share the same list. Extending
            # again would cause duplication
            # regardless. See: .agent_lessons/lessons_msg_count_bug.md for
            # detailed analysis.
            self._create_completed = True  # Mark for finally-block EXIT log reason tracking

            # Item 12: Always emit final sub-agent state after loop completes
            # (Fix
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            # Issue Y2: Use shared helper method instead of duplicated logic
            current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
            self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

            # ── Push final stream_update after sub-agent completes ──────────────
            # FIX A (turn-end frame dedup): the old code forced last_send=0.0 so the
            # throttle check `(now - 0.0 > 0.1)` was ALWAYS true → an unconditional
            # full frame even when the last loop tick had just delivered one <100ms ago.
            # That produced a same-timestamp duplicate of the final committed state.
            #
            # Safe conditional: only emit a final sub-agent frame when it is NOT already
            # covered by the most recent loop broadcast. need_final is True when ANY of:
            #   (a) the last loop tick was SUPPRESSED (_last_tick_suppressed) — its committed
            #       state never reached the UI, so we MUST deliver it now even if <100ms have
            #       passed since the previous actual send (guards the message-loss regression
            #       on fast turns that finish right after a throttled tick), OR
            #   (b) >=100ms elapsed since the last ACTUAL send — the throttle window has opened,
            #       so the current state is fresh enough to warrant a frame, OR
            #   (c) the response length changed vs. what the last tick reported — a new committed
            #       message not yet broadcast.
            # When need_final is False the loop already delivered an equivalent final state <100ms
            # ago and nothing has changed since, so we skip to avoid the spurious duplicate frame.
            #
            # We pass the REAL tracked values (_last_sub_send / _sub_last_resp_len), NOT 0.0:
            # if the last tick JUST sent (<100ms) and len is unchanged and it was not suppressed,
            # need_final is False → skip; if it was suppressed or throttled out, we send.
            # (broadcast_stream_update re-evaluates the same condition internally with these real
            # values, so no double-send can occur.)
            now_mono = time.monotonic()
            need_final = (_last_tick_suppressed or ((now_mono - _last_sub_send) > 0.1) or
                          (len(final_resp) != _sub_last_resp_len))
            if need_final:
                broadcast_stream_update(
                    pool=self.pool,
                    instance_name=instance_name,
                    turn_output=final_resp,
                    is_streaming_tick=False,
                    tick_num=_tick_num,
                    now_sec=now_mono,
                    last_send=_last_sub_send,
                    last_resp_len=_sub_last_resp_len,
                )

            # Root/caller panel refresh — KEEP (do NOT remove). While the parent is
            # suspended inside the child's tool call it has no other immediate update path,
            # so this is the only thing that refreshes the ROOT panel after the child completes.
            # NOTE: push_final_state targets a DIFFERENT instance (instance_name=caller, full
            # pool snapshot) than the sub-agent final frame above (instance_name=<sub>), so it
            # is NOT a same-timestamp duplicate of that frame — no stagger needed. The measured
            # "same timestamp" pair was queue-level; per-instance there is never a collision.
            self.stream_publisher.push_final_state(inst, caller)

        finally:
            # Telemetry: record agent instance call (non-blocking, fires in
            # finally so failed delegations are counted too)
            _call_latency_ms = (time.perf_counter() - _call_start) * 1000
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_agent_instance_call(
                        instance_name,
                        agent_class,
                        caller,
                        latency_ms=_call_latency_ms,
                    )
                except Exception:
                    pass

            # Always clean up active stack — even on halt or error
            with self.pool._execution._state_lock:
                for i, (name, _depth) in enumerate(self.pool._execution.active_stack):
                    if name == instance_name:
                        self.pool._execution.active_stack.pop(i)
                        break

            # Determine exit reason for debugging
            _completed = getattr(self, '_create_completed', False)
            logger.debug(
                '[CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=%s, reason=%s, '
                'inst_type=%s, conv_len=%d, final_resp_len=%d', instance_name, 'completed' if _completed else 'aborted',
                type(inst).__name__, len(conv),
                len(final_resp) if 'final_resp' in locals() else 0)

        # FIX: Return a copy of the actual instance conversation, not the stale
        # `conv` variable.
        # For new instances, rebuild_conversation() creates a COPY of conv via
        # list(new_messages),
        # so the original `conv` from initialize_conversation never receives
        # appended messages.
        # For reused instances, they happen to be the same reference — but
        # using inst.conversation
        # is correct in both cases. See: investigation report for sub-agent
        # response propagation bug.
        return inst, list(inst.conversation)

    def _create_system_agent(self,
                             agent_class: str,
                             instance_name: str,
                             task: str,
                             caller: str,
                             context: str = '') -> 'AgentInstance':
        """Create a fresh AgentInstance for system-invoked agents (Security, Compressor).

        Unlike _create_and_run_agent(), this always creates a NEW instance even if one
        with the same name exists. This is needed for agents that should start fresh
        each time they're invoked (no conversation history carryover).

        This method:
        - Creates new AgentInstance (never reuses existing)
        - Adds to pool.instances
        - Initializes pool.instance_state for UI visibility
        - Sets up active_stack tracking
        - Returns the instance ready for engine.run() execution

        Args:
            agent_class: The agent class name (e.g., 'Security', 'Compressor')
            instance_name: The instance name (usually same as agent_class for system agents)
            task: The task prompt to give the agent
            caller: The parent/caller instance name
            context: Optional context to prepend to task

        Returns:
            AgentInstance ready for execution via engine.run()
        """
        # FIX

        # Use lifecycle manager with force_fresh=True for system agents
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(agent_class,
                                                                                    instance_name,
                                                                                    caller,
                                                                                    nest_depth=0,
                                                                                    force_fresh=True)

        # Build system message using lifecycle manager (use inst.agent_class
        # for consistency)
        sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)

        # Build the task message via the shared lifecycle builder (H1 — no image scanning; it
        # does not forward caller images). For Security only, inline the supervisor's full log path
        # into the Context line (see _with_run_identity) so its soul's "read caller's logs directly"
        # capability is preserved (V2 metadata fix).
        # NOTE: build_task_message lives on the ENGINE's lifecycle manager (self.lifecycle, set in
        # __init__), NOT on the pool — a real AgentPool has no `lifecycle` attribute. (An earlier
        # v2 draft called self.pool.lifecycle here and crashed at runtime on a real shell_cmd
        # security check; test fixtures masked it by stubbing pool.lifecycle.)
        task_msg = self.lifecycle.build_task_message({'task': task, 'context': context}, caller)
        if inst.agent_class == 'Security':
            task_msg = _with_run_identity(self.pool, task_msg, caller)

        # Initialize conversation using lifecycle manager (pass actual is_reuse
        # value)
        conv = self.lifecycle.initialize_conversation(inst,
                                                      sys_msg,
                                                      task_msg,
                                                      is_reuse=is_reuse,
                                                      instance_name=instance_name,
                                                      agent_class=inst.agent_class,
                                                      from_external_load=session_was_loaded)

        # Phase 4.1: Propagate settings from caller to system agent
        self.lifecycle.propagate_settings(inst, caller, inst.agent_class)

        # Track in active stack (thread-safe)
        self.pool.active_stack_append(instance_name, 0)

        # Initialize WebUI state for immediate tab visibility
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, inst.agent_class, inst, conv, final_resp=[], is_initial=True)

        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)

        # System-invoked agents have no human in the loop for shell approval. Keep shell_cmd
        # AVAILABLE but restrict it to read-only/auto-approved commands: safe commands
        # (git status, dir, …) run without a prompt; anything else is hard-REJECTED (never
        # prompted). See ShellMixin.execute_shell_command / ShellCmd._launch_async gates.
        inst.restricted_shell = True

        return inst

    def _acquire_reusable_system_agent(self,
                                       agent_class: str,
                                       instance_name: str,
                                       task: str,
                                       caller: str,
                                       rid: str = '',
                                        context: str = '') -> Optional[Tuple[Any, bool, str]]:
        """Acquire a *warm* system agent for reuse, resetting its conversation to [system].

        BUG_0029 Phase 3 (Security instance reuse). Instead of spawning a fresh
        ``Security_<rid>`` per check (which makes the LLM prompt prefix cold every time), this
        reuses one long-lived warm instance under a FIXED name so the system message stays
        byte-identical and hits the server-side KV cache. The conversation is reset to
        ``[system]`` + the new task; ``conversation[0]`` is kept as the SAME object (never
        rebuilt) so the serialized prefix is unchanged.

        This method:
          * Takes a process-level claim on ``instance_name`` FIRST (security_reuse.try_claim).
            If another check already owns it, returns None — the caller falls back to the
            untouched legacy ``_create_system_agent`` path.
          * Runs the full eligibility predicate (plan §3.4) with read-only checks BEFORE any
            mutation. If ANY fails → returns None (fresh-spawn fallback). Never raises.
          * On success, under a NON-BLOCKING ``inst._compression_lock``, keeps
            ``conversation[0]`` as the same object, calls ``rebuild_conversation([sys_msg])``
            and applies every manual reset from plan §6 (the CRITICAL callout: rebuild does NOT
            reset _tool_warnings / _cache_notifications / memory-hint / auto-skill / stream-push
            flags — those are assigned explicitly). Keeps ``_system_prompt_frozen=True`` (does
            NOT call ``reset_conversation()``).

        Returns ``(inst, is_reuse, name)``:
          * ``(inst, True, instance_name)``  — reused an existing warm instance
          * ``(inst, False, instance_name)`` — SEEDED: no instance existed under ``instance_name``,
            so one was created under that fixed name (the caller still runs it this check; the NEXT
            check can reuse it)
          * ``None`` — an existing instance was found but is INELIGIBLE, or the claim is held, or an
            exception occurred. The caller falls back to a fresh per-rid spawn and MUST NOT clobber
            the existing instance.

        Claim ownership hand-off: on SUCCESS (reuse OR seed) the claim is left HELD — the caller's
        ``finally`` releases it (after ``_cleanup()``) so it covers the whole run. On every FAILURE /
        early-return / exception path the claim is released HERE (via a finally), otherwise the name
        would be wedged for the process lifetime and reuse would silently stop after the first
        eligibility miss.

        Note: the seed branch (BUG_0029 Phase 3 bootstrap fix) creates the warm instance under the
        FIXED name when the slot is empty, so the NEXT check can reuse it — this restores the
        create-or-reuse contract. It deliberately does NOT reach into ``lifecycle_manager`` for the
        REUSE path (no find_or_create_instance / initialize_conversation there); only the seed branch
        calls ``_create_system_agent``.
        """
        from agent_cascade.security_reuse import try_claim, release_claim
        from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES

        # Gate 1 — claim. The single atomic safety property: exactly one owner at a time.
        # If another check owns this name we must NOT touch its instance → fall back.
        if not try_claim(instance_name, rid):
            return None

        # We now OWN the claim. Ownership hand-off rule:
        #   * SUCCESS path  → do NOT release here; the caller's finally releases it AFTER the run
        #     so the claim covers the whole LLM call (a concurrent check must not reuse mid-run).
        #   * EVERY failure / early-return / exception path → release HERE, or the name is wedged
        #     for the process lifetime and reuse silently stops after the first eligibility miss.
        # _success_returned flips True only on the success return; the finally below releases on
        # every other exit (early returns AND exceptions), because a `finally` still runs when a
        # `return` is hit inside the try body.
        _success_returned = False
        try:
            pool = self.pool

            # Gate 2 — a warm instance of the right class must exist under this fixed name.
            inst = pool.instances.get(instance_name) if getattr(pool, 'instances', None) else None
            if inst is not None and inst.agent_class != agent_class:
                # A DIFFERENT class owns this name — do NOT clobber it; treat as ineligible.
                return None

            if inst is None:
                # ── SEED-ON-MISS (the create-or-reuse contract the v2 BUILD dropped) ──────────
                # The slot under the fixed name is provably empty: we HOLD the claim for this
                # name (the `try_claim` above succeeded and is not reentrant), so no other
                # check can be seeding it concurrently. Create here, under the FIXED name, so the
                # NEXT check can reuse it. is_reuse=False because this check reused nothing — the
                # caller must not treat it as a cache hit.
                #
                # Clean by construction: the instance was JUST built by _create_system_agent, so
                # every field the reuse path normally clears is already at its dataclass default
                # (_auto_skill_task_output=None, _auto_skill_proposed=False,
                # _auto_skill_dirty_stop=False, _tg_first_pushed=False,
                # _tg_final_pushed_phase=None, _tg_final_pushed_text=None,
                # compression_summary=None, _state_label=None, _last_endpoint_config=None).
                # Hence NO reset block runs here — do not "helpfully" add one.
                try:
                    inst = self._create_system_agent(
                        agent_class=agent_class, instance_name=instance_name,
                        task=task, caller=caller)
                except Exception as e:  # noqa: BLE001
                    logger.warning('[SECURITY_REUSE] seed failed for %s (rid=%s): %s '
                                   '— falling back to fresh spawn', instance_name, rid, e)
                    return None            # the finally below releases the claim

                # Freeze now, or the NEXT check fails Gate 8b and the seed is wasted. Non-stable
                # classes keep the normal end-of-_setup_turn freeze lifecycle. `inst` was just built
                # by _create_system_agent(agent_class=agent_class, …) above, so use the param directly.
                if agent_class.lower() in STABLE_SYSTEM_PROMPT_CLASSES:
                    inst._system_prompt_frozen = True

                # Hand the claim to the caller's finally, exactly as the reuse path does.
                _success_returned = True
                return (inst, False, instance_name)

            # ── Eligibility predicate (plan §3.4) — all read-only, BEFORE any mutation ────────

            # 3. State must be IDLE. Not TERMINATED (terminal in the transition matrix) and not
            #    RUNNING/SLEEPING/COMPLETING — engine.run() would raise at the L1 guard otherwise.
            with inst._state_lock:
                if inst.state != AgentState.IDLE:
                    logger.debug('[SECURITY_REUSE] %s ineligible (gate=state_not_idle, rid=%s): state=%s',
                                 instance_name, rid, inst.state)
                    return None

            # 4. A leaked endpoint permit (the slot-permit-leak program). Gate 3 already
            #    proved state == IDLE, so NO live run of this instance holds a permit — a set
            #    _slot_release here is by definition a leaked (orphaned) permit from an
            #    abandoned run. Release it (idempotent, atomic) instead of rejecting, so the
            #    warm instance can be reused. If a live holder still owns the pool entry for
            #    this name, the release no-ops on the pool (acquisition_id mismatch,
            #    slot_queue.py) and we stay conservative: re-check and reject only if it is
            #    still set. (Insurance only — the primary leak leaves state=RUNNING, so Gate 3
            #    rejects before Gate 4 is ever reached; see plan Fix 3 / R1.)
            if inst._slot_release is not None or inst._slot_key is not None:
                cleared = self._discard_stale_permit(
                    inst, instance_name,
                    context='reuse-gate4-stale-permit', action='drop-stale-reuse')
                if not cleared and (inst._slot_release is not None or inst._slot_key is not None):
                    logger.debug('[SECURITY_REUSE] %s ineligible (gate=slot_permit_leaked, rid=%s): slot_release=%r slot_key=%r',
                                 instance_name, rid, inst._slot_release, inst._slot_key)
                    return None
                logger.warning('[SECURITY_REUSE] %s recovered a leaked permit at reuse-gate4 '
                               '(rid=%s) — cleared and proceeding.', instance_name, rid)

            # 5. No live LLM call, and the last activity is older than a small epsilon — guards
            #    against a generator abandoned by `break` without close() still mid-flight.
            if inst._llm_call_active:
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=llm_call_active, rid=%s): live LLM call in flight',
                             instance_name, rid)
                return None
            _last_llm = getattr(inst, '_last_llm_activity', 0.0) or 0.0
            if _last_llm and (time.monotonic() - _last_llm) < _REUSE_IDLE_EPSILON:
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=llm_recent_activity, rid=%s): age=%.3fs eps=%.2fs',
                             instance_name, rid, time.monotonic() - _last_llm, _REUSE_IDLE_EPSILON)
                return None

            # 6. Not genuinely halted (compression-halt / manual stop).
            if getattr(pool, 'is_instance_halted', None) is not None and pool.is_instance_halted(instance_name):
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=halted, rid=%s): instance halted',
                             instance_name, rid)
                return None

            # 7. Not terminated (pool-level set or per-instance flag).
            if getattr(pool, 'is_instance_terminated', None) is not None and pool.is_instance_terminated(instance_name):
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=terminated_pool, rid=%s): pool-level termination set',
                             instance_name, rid)
                return None
            if inst.is_terminated:
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=terminated_flag, rid=%s): instance flag is_terminated',
                             instance_name, rid)
                return None

            # 8. Conversation present and starting with a SYSTEM message (the prefix we keep).
            conv = inst.conversation
            if not conv or getattr(conv[0], 'role', None) != SYSTEM:
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=bad_conversation_head, rid=%s): len=%d head_role=%r',
                             instance_name, rid, len(conv), getattr(conv[0], 'role', None) if conv else None)
                return None

            # 8b. The system prompt must already be FROZEN. An unfrozen instance would re-run the
            #     _setup_turn M1 rewrite on its next turn and inject volatile run-identity lines,
            #     silently killing the byte-identical prefix — so it must fall back to a fresh spawn.
            if inst._system_prompt_frozen is not True:
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=prompt_not_frozen, rid=%s): system prompt not frozen',
                             instance_name, rid)
                return None

            # 9. No pending queued messages for this instance.
            mq = getattr(pool, 'message_queues', None)
            if mq and mq.get(instance_name):
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=queued_messages, rid=%s): count=%d',
                             instance_name, rid, len(mq.get(instance_name)))
                return None

            # 11. Not currently on the active stack (a live run would be mid-conversation).
            with pool._execution._state_lock:
                if any(n == instance_name for n, _d in pool._execution.active_stack):
                    logger.debug('[SECURITY_REUSE] %s ineligible (gate=on_active_stack, rid=%s): instance on active stack',
                                 instance_name, rid)
                    return None

            # 10. Compression lock must be acquirable WITHOUT blocking — a live compressor or
            #     rollback holding it means the instance is not quiescent (plan R3). Non-blocking
            #     acquire guarantees we never stall here (test asserts < 0.5s wall time).
            if not inst._compression_lock.acquire(blocking=False):
                logger.debug('[SECURITY_REUSE] %s ineligible (gate=compression_lock_busy, rid=%s): compression lock held',
                             instance_name, rid)
                return None

            try:
                # ── Reset to [system] — keep conversation[0] as the SAME object (the cache win) ──
                # Take the reference FIRST: reset_conversation() clears the list, so we must hold
                # sys_msg before it. rebuild_conversation([sys_msg]) then re-sets conversation =
                # [sys_msg] with the SAME Message object → byte-identical serialized prefix.
                sys_msg = inst.conversation[0]
                # H2: use reset_conversation() for the BULK of the per-run resets (turn, tool
                # warnings, cache notifications, memory-hint block + conversation.clear()). Its only
                # side effect we must undo is _system_prompt_frozen=False, which we re-set to True
                # immediately after. _compression_lock is an RLock, so this nests safely under the
                # non-blocking acquire we already hold.
                inst.reset_conversation()
                inst.rebuild_conversation([sys_msg])
                # THE one field reset_conversation() clears that must be restored: freezing makes
                # the _setup_turn M1 rewrite (core.py:1642) skip, so conversation[0] is never
                # re-stamped with volatile run-identity on a reused turn.
                inst._system_prompt_frozen = True

                # ── DELTA resets — fields reset_conversation() does NOT touch (plan §7) ────────
                inst.compression_summary = None
                inst.latest_marker_index = -1
                inst.max_turns = None  # caller re-sets immediately after (SECURITY_AGENT_MAX_TURNS)
                inst._generate_cfg_override = None  # caller re-sets immediately after (UI cfg)
                inst._turn_consumed = False
                inst._loop_rollback_count = 0
                inst._compression_suspended_at = 0.0
                inst._suppress_loop_detection_next_turn = False
                inst.is_terminated = False
                inst.sleeping_since = None
                inst._continue_saved_msg = None
                # CRITICAL (R5): a stale _state_label makes _setup_turn do a state/load that
                # destroys the warm KV cache. Clear it + the endpoint config snapshot.
                inst._state_label = None
                inst._last_endpoint_config = None
                # Auto-skill per-run block (agent_instance.py:276-283).
                inst._auto_skill_task_output = None
                inst._auto_skill_source_task = None
                inst._auto_skill_proposed = False
                inst._auto_skill_dirty_stop = False
                # Stream-push flags (agent_instance.py:290-292) — a reused instance must not carry
                # its previous run's "already pushed" state forward.
                inst._tg_first_pushed = False
                inst._tg_final_pushed_phase = None
                inst._tg_first_pushed_text = None
                # Streaming partials (agent_instance.py:323) — reset_conversation() does NOT clear
                # _streaming_responses, so a reused instance would otherwise serve its previous run's
                # stale tail on the first frame. Cleared here (compression lock already held); it is
                # repopulated from the first LLM yield of the new run. Mirrored in
                # lifecycle_manager._prepare_instance_for_reuse so both reuse paths stay in lockstep.
                inst._streaming_responses = []

                # ── H3: child relationship + ownership re-pointing (shared with find_or_create) ──
                # NOTE: _prepare_instance_for_reuse lives on the ENGINE's lifecycle manager
                # (self.lifecycle, set in __init__), NOT on the pool — a real AgentPool has no
                # `lifecycle` attribute. An earlier draft called self.pool.lifecycle here; the
                # AttributeError was swallowed by this method's best-effort except below, so every
                # check fell back to a fresh spawn while the reset above it had already run. Same
                # bug class as the build_task_message call sites fixed in _create_and_run_agent
                # and _create_system_agent.
                self.lifecycle._prepare_instance_for_reuse(inst, caller, nest_depth=0)
                # acquire-specific (NOT in the shared helper): a reused Security instance must run
                # its read-only shell checks without prompting. The lifecycle reuse branch does not
                # set this, so it is applied here to preserve v1's acquire behaviour exactly.
                inst.restricted_shell = True

            finally:
                # We acquired the compression lock non-blocking above; always release it here.
                inst._compression_lock.release()

            # ── Append the new task message (outside the compression-lock block) ──────────────
            # H1: same shared builder as the fresh path + Security-only run-identity relocation, so
            # a fresh and a reused Security instance get an identical conversation[1] shape.
            # build_task_message is on the engine's lifecycle (self.lifecycle), not the pool — see
            # the matching NOTE in _create_system_agent above.
            task_msg = self.lifecycle.build_task_message({'task': task, 'context': context}, caller)
            if inst.agent_class == 'Security':
                task_msg = _with_run_identity(self.pool, task_msg, caller)
            inst.append_message(task_msg)

            # ── UI / stream bookkeeping (mirrors _create_system_agent) ────────────────────────
            pool.active_stack_append(instance_name, 0)
            self._update_webui_state(instance_name, agent_class, inst, inst.conversation, final_resp=[], is_initial=True)
            self.stream_publisher.push_initial_state(inst, caller)

            # Drop the warm instance's cached serialization/version so the first frame of the new
            # run re-serializes from the freshly-reset conversation (stale tail / version otherwise
            # survives reuse). Paired eviction — one extra full re-serialization per reuse is cheap.
            from agent_cascade.api_integration_pkg.cache import _cache_mgr
            _cache_mgr.evict_instance(instance_name)

            logger.info('[SECURITY_REUSE] reusing warm %s (rid=%s, caller=%s)', instance_name, rid, caller)
            _success_returned = True  # hand the claim to the caller's finally — do NOT release here
            return (inst, True, instance_name)

        except Exception as e:  # noqa: BLE001 — reuse is best-effort; NEVER let it break the check
            logger.warning('[SECURITY_REUSE] acquire failed for %s (rid=%s): %s — falling back to fresh spawn',
                           instance_name, rid, e)
            return None

        finally:
            # Release the claim on EVERY non-success exit. A `finally` runs even when a `return`
            # is hit inside the try body, so this covers all early-return eligibility misses AND
            # the except-Exception path. On success we leave it held for the caller's finally.
            if not _success_returned:
                release_claim(instance_name, rid)

    def _update_webui_state(self,
                            instance_name: str,
                            agent_class: str,
                            inst: AgentInstance,
                            conv: list,
                            final_resp: list = None,
                            is_initial: bool = False) -> None:
        """Update WebUI state for an agent instance (shared helper to eliminate duplication).

        Extracted from duplicated logic in _create_and_run_agent and _create_system_agent.
        Handles initial, periodic, and final state updates with thread-safe operations.

        Args:
            instance_name: The agent instance name
            agent_class: The agent class type
            inst: The AgentInstance object
            conv: Current conversation list
            final_resp: Optional final response for message summary extraction
            is_initial: If True, use empty summary (initial state); otherwise extract from final_resp
        """
        try:
            # Build lightweight summary of latest message (or empty for initial
            # state)
            latest_summary = ''
            if not is_initial and final_resp:
                last_msg = final_resp[-1]
                role = msg_field(last_msg, 'role', '')
                content = msg_field(last_msg, 'content', '')
                # For FUNCTION role (tool results), include tool name in
                # summary
                if role == FUNCTION:
                    from agent_cascade.utils.utils import format_tool_result_preview
                    tool_name = msg_field(last_msg, 'name', '')
                    latest_summary = format_tool_result_preview(tool_name, content, max_len=450)
                else:
                    latest_summary = str(content)[:500] if content else ''

            # Thread-safe state read - snapshot under lock before building dict
            with inst._state_lock:
                current_state = inst.state

            state = {
                'active': current_state in (AgentState.RUNNING, AgentState.SLEEPING),
                'agent_state': current_state.name,  # Send actual state name for activity indicator coloring
                'agent_name': f"{instance_name} ({agent_class})",
                'message_count': len(conv),
                'latest_message_summary': latest_summary,
                'conversation_length_tokens': getattr(inst, '_cached_token_count', 0),
            }

            # Update pool state (thread-safe)
            with self.pool._execution._state_lock:
                self.pool.instance_state[instance_name] = state

        except Exception as e:
            logger.debug(f"WebUI state update for {instance_name} failed (non-critical): {e}")

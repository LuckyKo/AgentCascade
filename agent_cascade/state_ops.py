"""State save/restore operations for llama-autoloader endpoints.

Talks to autoloader state endpoints for KV cache slot state management.
All operations are best-effort — failures are logged but never block execution.

Per-instance state limits are obsolete: we use stable labels (instance_name) and rely on
autoloader's per-model max-5 LRU eviction to manage disk usage.
"""

from typing import Optional

import concurrent.futures
import concurrent.futures.thread
import logging
import logging.handlers
import threading
import weakref

import httpx

from agent_cascade.log import _AppLoggerProxy
from agent_cascade.settings import STATE_HTTP_TIMEOUT

# Late-bound delegate to the live app logger (see agent_cascade.log._AppLoggerProxy):
# a module logger (logging.getLogger(__name__)) never reaches the console/file
# handlers, so state save/restore records would silently vanish.
logger = _AppLoggerProxy()

# Track which instances have confirmed no legacy files — skip their cleanup checks.
_legacy_cleanup_done_instances: set[str] = set()


# BUG_0295: hard wall-clock deadline on the restore round-trip. httpx's `timeout`
# bounds EACH socket operation (connect/read/write/pool), NOT the whole request — so a
# half-open connection whose response flush stalls mid-body never trips httpx.ReadTimeout
# and can pin the orchestrator turn forever (RC-1). The executor deadline below is the
# true total-request ceiling. The small margin lets httpx's own read timeout fire first
# (cleaner: it names the exception) in the merely-slow case; only the pathological
# never-returns stall reaches the executor deadline. +5s is negligible against the
# default 120s budget while leaving a grace window.
_RESTORE_TIMEOUT_MARGIN_S = 5.0


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """1-worker executor whose worker is a DAEMON and is NOT registered with
    `concurrent.futures.thread._threads_queues`.

    The restore worker can be abandoned mid-round-trip by the deadline in
    restore_state and stay blocked on the socket. With the stock executor that worker
    is a non-daemon thread and the atexit `_python_exit` hook `t.join()`s it with no
    timeout — so a single wedged restore would block clean interpreter/test-process
    shutdown. This mirrors CPython 3.12 `_adjust_thread_count` (pinned to AC's
    interpreter) but starts a daemon and omits the `_threads_queues` registration, so
    shutdown never joins a wedged worker.
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        if len(self._threads) < self._max_workers:
            t = threading.Thread(
                name='%s_%d' % (self._thread_name_prefix or self, len(self._threads)),
                target=concurrent.futures.thread._worker,
                args=(weakref.ref(self, weakref_cb),
                      self._work_queue,
                      self._initializer,
                      self._initargs),
                daemon=True,
            )
            t.start()
            self._threads.add(t)
            # Deliberately NOT added to concurrent.futures.thread._threads_queues:
            # that registration is exactly what makes `_python_exit` join the worker.


# One shared worker, reused across restores — bounds thread growth (the plan's
# micro-opt, avoids a fresh executor per call). Tradeoff: because it is a SINGLE
# worker, one never-returning restore also makes subsequent restores queue and fail
# fast to cold-start until that socket is reclaimed (or the process restarts). That is
# degraded (no KV restore) but NEVER a pinned turn — the intended tradeoff; monitor
# worker/queue depth if it recurs.
_RESTORE_EXECUTOR = _DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix='state-restore')


def _emit_critical_to_file_handler(message: str) -> None:
    """Best-effort mirror of a critical restore WARNING straight to console.log.

    RC-2 (BUG_0295): the app logger's FIRST handler is a synchronous stdout
    StreamHandler; if the Windows console wedges, `logger.warning` blocks there and
    console.log (the second handler) never receives the record. Emitting the
    actionable line directly through the already-open RotatingFileHandler keeps it on
    disk even when the console handler is stalled. Never raises — a broken log write
    must not turn a logged failure back into an exception on the restore path.
    """
    try:
        for handler in getattr(logger, 'handlers', None) or []:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                record = logging.LogRecord(
                    name='agent_cascade.state_ops',
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg=message,
                    args=(),
                    exc_info=None,
                )
                handler.emit(record)
                break
    except Exception:
        pass


def _warn_restore_failure(label: str, exc: BaseException) -> None:
    """Warn a restore transport/worker failure naming the exception, and mirror it to
    console.log (see _emit_critical_to_file_handler). Shared by the generic failure
    handler and the worker-raised-TimeoutError path in restore_state."""
    message = 'State restore failed for label=%s: %s: %s' % (label, type(exc).__name__, exc)
    logger.warning(message)
    _emit_critical_to_file_handler(message)


def _normalize_api_base(api_base: str) -> str:
    """Normalize api_base to base URL without trailing /v1."""
    base = api_base.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    return base


# Cap on the refusal text kept in one log line. Engine refusals are a sentence
# ("session snapshot does not fit the free KV capacity; evict other sessions first");
# a proxy/gateway error page can be tens of KB, and none of that belongs in a log line.
_REFUSAL_BODY_CHARS = 400


def _refusal_detail(resp) -> str:
    """One-line, bounded rendering of a state save/load refusal body (BUG_0178).

    The engine explains a refusal in the body and the autoloader passes it through, e.g.
    ``400 slot_restore_failed: session snapshot does not fit the free KV capacity; evict
    other sessions first``. Logging only the status (the old behaviour) threw that away,
    so a refused restore was indistinguishable from "file missing" on the AC side and the
    real reason existed only in the autoloader's console log.

    Best-effort by construction: a response whose body cannot be read degrades to a
    placeholder, never to an exception — a failed save/restore must not fail harder
    because its diagnostics did.
    """
    try:
        text = resp.text or ''
    except Exception:
        return '<unreadable body>'
    collapsed = ' '.join(text.split())  # one log line, whatever the server sent
    if not collapsed:
        return '<empty body>'
    if len(collapsed) > _REFUSAL_BODY_CHARS:
        collapsed = f'{collapsed[:_REFUSAL_BODY_CHARS]}… (+{len(collapsed) - _REFUSAL_BODY_CHARS} chars)'
    return collapsed


def is_state_save_capable(endpoint_cfg) -> bool:
    """True if an endpoint config declares state-save capability.

    Shared predicate for the safe-mode caller-endpoint gate: a dict carrying
    ``state_save_enabled`` plus a usable ``api_base``/``model``. Deliberately port-
    independent (no fixed 1234/9123 check) — the capability flag is the source of
    truth. media_gen/image_gen now gate on this same capability check (state_save_enabled
    + api_base + model) inline. ``is_autoloader_endpoint`` (port-based) is a separate,
    coarser check used by the shell_cmd AUTO low-RAM hook to decide whether to probe the
    autoloader's /v1/status — it is NOT a substitute for this capability gate.
    """
    return bool(
        isinstance(endpoint_cfg, dict)
        and endpoint_cfg.get('state_save_enabled')
        and endpoint_cfg.get('api_base')
        and endpoint_cfg.get('model')
    )


def kv_save_effectively_enabled(instance: 'AgentInstance', toggle: bool) -> bool:
    """Whether the AUTOMATIC checkpoint path should run for ``instance`` right now.

    Single source of truth for the automatic slot-yield/sleep checkpoint gates: the
    "State KV Save/Restore" toggle ORed with the instance's safe-mode override (``_safe_mode_kv``). The
    override is what lets a shell_cmd ``safe`` command force save/restore ON for its window
    even when the (default-off) checkpoint toggle is off.

    Two non-obvious points: (1) the flag is written under ``_state_lock`` by shell_cmd but read
    here outside it — safe because it's a single bool (atomic in CPython), write-once/clear-once
    per window; taking the lock would add hot-path contention for zero safety gain. (2) callers
    must pass a real AgentInstance — a bare MagicMock auto-creates a truthy ``_safe_mode_kv`` and
    would silently force KV on.
    """
    if toggle:
        return True
    return bool(getattr(instance, '_safe_mode_kv', False))


def save_instance_state(instance: 'AgentInstance', set_label: bool = True) -> bool:
    """Save KV cache state for an instance.

    Uses cached endpoint config from the instance (set during endpoint allocation),
    saves state via autoloader, and stores the label on the instance under lock.

    Args:
        instance: AgentInstance to save state for.
        set_label: When True (default — every existing caller), store the label on
            the instance, arming the automatic next-turn restore. When False (the
            manual user-initiated path), perform the save but do NOT touch
            _state_label: the KV stays warm and no automatic restore is scheduled.
            The saved FILE still exists under the stable label and can be loaded
            explicitly via restore_held_slot_state(..., label=instance_name).

    Returns:
        True if state was saved (and label stored when set_label is True), False otherwise.
    """
    try:
        with instance._state_lock:
            endpoint_cfg = instance._last_endpoint_config
            session_digest = getattr(instance, '_session_digest', None)
            digest_endpoint = getattr(instance, '_session_digest_endpoint', None)

        if not endpoint_cfg or not isinstance(endpoint_cfg, dict):
            logger.debug('No cached endpoint config for %s', instance.instance_name)
            return False

        if not endpoint_cfg.get('state_save_enabled'):
            logger.debug('State save not enabled for %s (%s)', instance.instance_name, instance.agent_class)
            return False

        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')

        if not api_base or not model:
            logger.debug('Missing api_base/model for state save on %s', instance.instance_name)
            return False

        # BUG_0187: the digest is the NInfer catalog cell the last turn was retained
        # under. Only forward it when it was produced by THIS endpoint — a digest from
        # a previous endpoint names a different model's session and must not be sent.
        if not (isinstance(session_digest, str) and session_digest
                and isinstance(digest_endpoint, tuple) and len(digest_endpoint) == 2
                and digest_endpoint == (api_base, model)):
            session_digest = None

        if session_digest:
            label = save_state(api_base, model, instance.instance_name, session_digest=session_digest)
        else:
            label = save_state(api_base, model, instance.instance_name)
        if label:
            if set_label:
                with instance._state_lock:
                    instance._state_label = label
            logger.debug('Saved state %s for instance %s (label %s)',
                         label, instance.instance_name,
                         'stored' if set_label else 'not stored')
            return True

        logger.debug('State save returned no label for %s', instance.instance_name)
        return False

    except Exception as e:
        logger.warning('Unexpected error during state save for %s: %s', instance.instance_name, e)
        return False


def restore_instance_state(instance: 'AgentInstance', held_endpoint_cfg: Optional[dict] = None,
                          label: Optional[str] = None) -> bool:
    """Restore KV cache state for an instance.

    Reads the label and endpoint config from the instance under lock, uses cached
    endpoint config to restore state via autoloader, and clears the label on failure
    to avoid retrying stale state.

    Args:
        instance: AgentInstance to restore state for.
        held_endpoint_cfg: Optional {'api_base', 'model'} dict of the endpoint the
            instance CURRENTLY holds a slot on. When provided, it is used as the
            restore target INSTEAD of ``instance._last_endpoint_config`` — which may
            be stale (e.g. pointing at a shared conc=0 autoloader the agent no longer
            owns). Restoring to an endpoint we don't hold would make the autoloader
            load the model and auto-evict a live sibling's resident model, so callers
            that own a slot must pass the held endpoint. When None (backward-compat
            path), falls back to ``_last_endpoint_config`` as before.
        label: Optional explicit stable state label (e.g. ``instance_name``). When
            provided it is used as the restore label INSTEAD of the consume-once
            ``instance._state_label``. The safe-mode seam needs this because a system
            agent (Security/Compressor) may have consumed ``_state_label`` inside the
            approval window before the tool unloads — but the state FILE it wrote
            survives the load (the autoloader's load_state does not delete it), so
            re-issuing state/load for the stable label re-warms the KV. When None
            (the default, every existing caller), behavior is byte-for-byte unchanged:
            the consume-once ``_state_label`` is read and a missing label returns
            False with no HTTP.

    Returns:
        True if state was restored successfully, False otherwise.
    """
    try:
        with instance._state_lock:
            pending_label = instance._state_label
            endpoint_cfg = instance._last_endpoint_config

        # An explicitly-supplied stable label wins over the consume-once _state_label.
        # When none is supplied, restore_label == pending_label and behavior is identical
        # to the original path.
        restore_label = label if label is not None else pending_label

        logger.debug('Attempting restore for %s, label=%s', instance.instance_name, restore_label)

        if not restore_label:
            logger.debug('No state label to restore for %s', instance.instance_name)
            return False

        # Prefer the endpoint the caller confirmed we currently hold a slot on.
        # This is the eviction-safety gate: only ever load onto an owned endpoint.
        if held_endpoint_cfg and isinstance(held_endpoint_cfg, dict):
            endpoint_cfg = held_endpoint_cfg

        if not endpoint_cfg or not isinstance(endpoint_cfg, dict):
            logger.debug('No cached endpoint config for %s', instance.instance_name)
            return False

        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')

        if not api_base or not model:
            logger.debug('Missing api_base/model for state restore on %s', instance.instance_name)
            return False
        success = restore_state(api_base, model, restore_label)
        if not success:
            # Restore failed — clear the label to avoid retrying stale state
            with instance._state_lock:
                instance._state_label = None
            logger.warning('State restore failed for %s (label=%s), cleared label', instance.instance_name, restore_label)
            return False

        # Clear the label after successful restore to prevent double-restore.
        with instance._state_lock:
            instance._state_label = None
        logger.debug('Restored state for %s (label=%s)', instance.instance_name, restore_label)
        return True

    except Exception as e:
        # Clear label on unexpected errors to avoid retrying stale state
        try:
            with instance._state_lock:
                instance._state_label = None
        except Exception:
            pass
        logger.warning('Unexpected error during state restore for %s: %s', instance.instance_name, e)
        return False


def save_state(api_base: str, model: str, instance_name: str,
               session_digest: Optional[str] = None) -> Optional[str]:
    """Save KV cache state. Returns label if successful, None on failure.

    ``session_digest`` (BUG_0187) is NInfer's identity for the retained session the
    caller's last turn produced. The autoloader resolves it to the matching catalog
    cell and saves THAT cell (with an ``if_digest`` precondition), instead of the old
    unconditional ``slot 0`` — which under concurrent fleet traffic holds a sibling
    agent's KV and captured the wrong session. Omitting it preserves the legacy
    single-slot behaviour for engines that do not publish session digests.
    """
    try:
        # Defensive check: reject obviously dangerous instance_names (belt-and-suspenders).
        # Autoloader also sanitizes labels via _sanitize_label().
        if '../' in instance_name or '..\\' in instance_name:
            logger.debug('Rejected state save for instance with unsafe name: %s', instance_name)
            return None

        base = _normalize_api_base(api_base)

        # Use instance_name as stable label — autoloader overwrites the same file on each save.
        # Previous timestamped labels caused per-model eviction of live agent state.
        # Autoloader's _sanitize_label() strips \, /, .. to prevent path traversal.
        label = instance_name
        url = f"{base}/v1/models/{model}/state/save"
        body = {'label': label}
        if isinstance(session_digest, str) and session_digest:
            body['session_digest'] = session_digest
        # Must exceed the autoloader's own 120 s slot-action budget: the shortest timeout in the
        # chain always wins, and a 30 s ceiling abandoned a multi-GB save that was still writing
        # (leaving an orphan 1.4 GB .tmp, the save killed by the unload that followed it).
        resp = httpx.post(url, json=body, timeout=STATE_HTTP_TIMEOUT)

        if resp.status_code == 200:
            # Cleanup old states for this instance after successful save
            _cleanup_old_states(base, model, instance_name)
            return label
        # Body kept (DEBUG: saves are best-effort and a non-capable endpoint answers 501 on
        # every attempt, so a WARNING here would be periodic noise). See _refusal_detail.
        logger.debug('State save refused for %s: HTTP %d %s',
                     instance_name, resp.status_code, _refusal_detail(resp))
        return None
    except Exception as e:
        logger.debug('State save failed for %s: %s', instance_name, e)
        return None


def restore_state(api_base: str, model: str, label: str) -> bool:
    """Restore KV cache state. Returns True if successful.

    Bounded by a hard wall-clock deadline (BUG_0295): the blocking POST runs on a
    shared single-worker executor and the caller waits at most
    ``STATE_HTTP_TIMEOUT + _RESTORE_TIMEOUT_MARGIN_S``. On timeout the caller falls back
    to the existing no-restore / cold-start branch (see restore_instance_state, which
    clears the label so it never re-fires) instead of pinning the orchestrator turn on a
    round-trip that never returns. No caller change; fully reversible — delete the
    executor wrapper to revert.
    """
    try:
        base = _normalize_api_base(api_base)
        url = f"{base}/v1/models/{model}/state/load"
        logger.debug('State restore: POST %s (label=%s, timeout=%.0fs)',
                       url, label, STATE_HTTP_TIMEOUT)

        # Explicit per-phase timeouts instead of the bare float: a bare timeout sets
        # connect/read/write/pool ALL to the same value, so a slow connect or a pool wait
        # eats the read budget. The restore is a long response, so read gets the full
        # STATE_HTTP_TIMEOUT while the other phases keep a fixed 10s cap.
        timeout = httpx.Timeout(connect=10, read=STATE_HTTP_TIMEOUT, write=10, pool=10)
        deadline = STATE_HTTP_TIMEOUT + _RESTORE_TIMEOUT_MARGIN_S

        def _post_load():
            return httpx.post(url, json={'label': label}, timeout=timeout)

        # Run the blocking POST off the caller's thread so the CALLER is bounded by a true
        # total-request deadline, independent of whether httpx's per-operation timeout
        # fires. CAVEAT — do NOT read this as "bounded": the abandoned worker may linger
        # blocked on the socket; it is released ONLY if httpx's read timeout eventually
        # fires. Under the exact observed BUG_0295 failure (httpx's timeout never fires)
        # the worker is NOT bounded by httpx and leaks until OS/TCP keepalive or process
        # restart. A leaked (daemon) thread is strictly better than a pinned turn — the
        # intended tradeoff; monitor worker/queue depth if it recurs.
        future = _RESTORE_EXECUTOR.submit(_post_load)
        resp = future.result(timeout=deadline)

        if resp.status_code != 200:
            # WARNING, not DEBUG: the caller reports "State restore failed … cleared label"
            # right after, and that line cannot carry the reason (restore_state returns
            # bool). Keeping the engine's sentence here is what makes the pair actionable —
            # e.g. "does not fit the free KV capacity; evict other sessions first" versus
            # "was saved for a different model". See _refusal_detail / BUG_0178.
            logger.warning('State restore refused for label=%s: HTTP %d %s',
                           label, resp.status_code, _refusal_detail(resp))
            return False

        return True
    except concurrent.futures.TimeoutError:
        # concurrent.futures.TimeoutError is an ALIAS of the builtin TimeoutError (3.11+),
        # so this branch also fires when the WORKER itself raised a TimeoutError. Name an
        # exception only when the worker genuinely raised one (a real transport failure the
        # caller clears the label off); an abandoned round-trip — still pending, or the
        # micro-race that completed-without-exception right as result() gave up — is our
        # deadline and must report the timeout, never a phantom 'NoneType: None'.
        exc = future.exception() if future.done() else None
        if exc is not None:
            _warn_restore_failure(label, exc)
            return False
        # The round-trip never returned within the deadline: httpx's per-operation timeout
        # did NOT unblock us (the BUG_0295 signature) — the EXECUTOR deadline did. Mirror the
        # WARNING to console.log so it survives a wedged console handler (RC-2).
        message = ('[STATE_RESTORE_TIMEOUT] label=%s — restore did not return within %.0fs; '
                   'falling back to no-restore (cold start)'
                   % (label, STATE_HTTP_TIMEOUT + _RESTORE_TIMEOUT_MARGIN_S))
        logger.warning(message)
        _emit_critical_to_file_handler(message)
        return False
    except Exception as e:
        # Transport/timeout: the restore did not happen. The reason is the exception, and the
        # caller will warn that the label was cleared — so this must name the failure.
        _warn_restore_failure(label, e)
        return False


def unload_all_models(api_base: str) -> bool:
    """POST /v1/unload_all on the llama-autoloader to free VRAM.

    Used by image_gen before running ComfyUI so the LLM's resident model is
    evicted and its VRAM is available for the diffusion model. Best-effort:
    failures are logged (warning) and reported via the return value, never raised.

    Args:
        api_base: The autoloader's API base URL (e.g., "http://localhost:8080/v1").

    Returns:
        True if unload succeeded (HTTP 200), False otherwise.
    """
    try:
        base = _normalize_api_base(api_base)
        resp = httpx.post(f"{base}/v1/unload_all", timeout=60)
        if resp.status_code != 200:
            logger.warning('[state_ops] unload_all returned status %d', resp.status_code)
            return False
        return True
    except Exception as e:
        logger.warning('[state_ops] unload_all failed for %s: %s', api_base, e)
        return False


def is_autoloader_endpoint(api_base: str) -> bool:
    """Check if endpoint points to llama-autoloader."""
    return ':1234/' in api_base or ':9123/' in api_base


def probe_memory_status(api_base: str, timeout: float = 1.5, *, quiet: bool = False) -> Optional[dict]:
    """GET {base}/v1/status on the autoloader; return parsed JSON or None (best-effort).

    Runs on the hot AUTO path of every shell_cmd whose held endpoint is an autoloader, so
    the timeout is kept short (~1.5s): a hung/unreachable autoloader must not stall real
    commands. The autoloader caches this body on a background poll, so a client GET is cheap.
    ``quiet`` logs a failed probe at DEBUG instead of WARNING (used by the safe-mode guarantee
    check, where a missing probe is an unverifiable guarantee, not a problem of its own).
    Body shape (see llama-autoloader server.py::_build_status):
      - 'ram':  {total_mb, used_mb, free_mb, pct}
      - 'gpus': [{index, name, total_mb, used_mb, free_mb, utilization_pct}, ...] (may be [])
      - 'models_loaded': int, 'uptime_models': [{id, uptime_s, last_used_s_ago, ...}] (BUG_0150)
    Any non-200 or transport/parse error returns None so callers degrade to the normal
    path — a status hiccup must never block or crash a command.
    """
    try:
        base = _normalize_api_base(api_base)
        resp = httpx.get(f"{base}/v1/status", timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        (logger.debug if quiet else logger.warning)('[state_ops] /v1/status probe failed for %s: %s',
                                                    api_base, e)
        return None


def probe_resident_models(api_base: str, timeout: float = 1.5) -> Optional[list]:
    """Return the models the autoloader reports as LOADED, or None when unverifiable.

    Used by safe-mode's guarantee check (BUG_0150): "was the local model really unloaded,
    and did it stay unloaded while the command ran?" Reads the same ``/v1/status`` body as
    :func:`probe_memory_status` — ``models_loaded`` plus ``uptime_models[].uptime_s`` (how long
    the model process has been up, i.e. when it was loaded) — so one cheap GET answers both.

    Returns:
        ``[{'id': str, 'uptime_s': float | None}, ...]`` — possibly ``[]`` when nothing is
        resident.
        ``None`` when the endpoint cannot answer or is a foreign/older server with no
        ``models_loaded`` field. Callers MUST NOT claim a guarantee they could not verify.

    Failures are logged at DEBUG, not WARNING: this runs on both sides of a safe-mode command
    and a missing probe is not itself a problem worth alerting on.
    """
    status = probe_memory_status(api_base, timeout, quiet=True)
    try:
        if not isinstance(status, dict) or 'models_loaded' not in status:
            return None
        models = [m for m in (status.get('uptime_models') or []) if isinstance(m, dict)]
        if models:
            return [{'id': m.get('id') or 'unknown model', 'uptime_s': m.get('uptime_s')} for m in models]
        if status.get('models_loaded'):
            # Resident per the count but no per-model detail: still a violation, just anonymous.
            return [{'id': 'unknown model', 'uptime_s': None}]
        return []
    except Exception as e:  # noqa: BLE001 - a malformed body must never break the caller's finally
        logger.debug('[state_ops] unparseable residency in /v1/status for %s: %s', api_base, e)
        return None


def ram_vram_pressure(api_base: str, ram_floor_mb: int, vram_floor_mb: int) -> dict:
    """Return {'force_safe': bool, 'low_vram_gpus': [int], 'ram_free_mb': Optional[int]}.

    force_safe    = free SYSTEM RAM < ram_floor_mb (HARD — caller forces safe mode).
                    Uses ONLY status['ram']['free_mb']; VRAM never contributes here.
    low_vram_gpus = indices of GPUs whose free_mb < vram_floor_mb (ADVISORY only).
    On probe failure returns {'force_safe': False, 'low_vram_gpus': [], 'ram_free_mb': None}
    so the caller degrades to the normal path (never blocks/crashes on a status hiccup).
    Missing 'ram'/'free_mb' keys are treated as not-forced (None).
    """
    status = probe_memory_status(api_base)
    if not status:
        return {'force_safe': False, 'low_vram_gpus': [], 'ram_free_mb': None}

    ram = status.get('ram') or {}
    ram_free = ram.get('free_mb')
    force_safe = isinstance(ram_free, (int, float)) and ram_free < ram_floor_mb  # non-numeric → unknown, never forced

    low_vram_gpus = []
    for g in (status.get('gpus') or []):
        free = g.get('free_mb')
        if isinstance(free, (int, float)) and free < vram_floor_mb:
            idx = g.get('index')
            if idx is not None:
                low_vram_gpus.append(idx)

    return {'force_safe': bool(force_safe), 'low_vram_gpus': low_vram_gpus, 'ram_free_mb': ram_free}


def resolve_held_endpoint(pool, instance: 'AgentInstance'):
    """Resolve the endpoint the instance currently holds a slot on.

    Returns {'api_base', 'model'} for the cursor-aware effective endpoint (the
    same resolution used at slot acquisition), or None if it cannot be resolved.
    Used by state-restore call sites so they only ever load onto an owned
    endpoint — loading a stale _last_endpoint_config on a shared conc=0 autoloader
    would auto-evict a live sibling's resident model.

    ``pool`` is duck-typed (only ``pool.api_router`` is touched), mirroring how
    slot_yield_utils duck-types the pool.
    """
    router = pool.api_router if hasattr(pool, 'api_router') else None
    if not router or not hasattr(router, 'get_effective_slot_info'):
        return None
    slot_info = router.get_effective_slot_info(instance.agent_class, instance_name=instance.instance_name) or {}
    api_base = slot_info.get('api_base') or ''
    # get_effective_slot_info carries no model — take it from the rotated chain head.
    chain = router.get_endpoint_chain(instance.agent_class, instance_name=instance.instance_name) or []
    model = (chain[0].get('model') if chain else '') or ''
    if not api_base or not model:
        return None
    return {'api_base': api_base, 'model': model}


def restore_held_slot_state(pool, instance: 'AgentInstance', inst_name: str,
                           label: Optional[str] = None) -> bool:
    """Restore KV state to the endpoint the instance currently holds a slot on.

    Defensive by design: any resolution failure skips the restore (a missed
    restore is far better than a wrongful eviction of a sibling's model).

    ``label`` is an optional stable state label (e.g. ``instance_name``) passed
    through to restore_instance_state INSTEAD of the consume-once
    ``instance._state_label``. The safe-mode seam passes it so the restore still
    works after a system agent consumed ``_state_label`` inside the approval window.
    When omitted (the engine path, unchanged) the consume-once label is used. The
    held endpoint is always resolved live — callers never supply it.

    On the explicit-label path only, a defensive guard also skips (and clears the
    orphaned label) when the state file's save endpoint no longer matches the held
    endpoint — turning what would be a silent 404 state/load into a diagnosable skip.
    """
    try:
        held_cfg = resolve_held_endpoint(pool, instance)
        if not held_cfg:
            logger.info('[STATE_RESTORE_SKIP] %s — could not resolve held endpoint', inst_name)
            # Clear the orphaned label so it does NOT re-trigger an expensive
            # state/load on every subsequent turn (a leaked _state_label would keep
            # firing the 3.1GB restore attempt while resolution stays broken). Mirrors
            # the "clear orphaned label" pattern in tool_dispatcher._run_child_sync.
            clear_orphaned_state_label(instance, inst_name)
            return False
        # P5 (diagnostic hardening, explicit-label path only): a stable-label restore
        # targets a file that lives on the SAVE endpoint (_last_endpoint_config). If the
        # held endpoint we'd restore onto differs, the file isn't there — skip rather
        # than 404 a state/load on the wrong endpoint (turns H2's silent failure into a
        # diagnosable one). The engine's consume-once path (label is None) relies on the
        # held-endpoint resolver and must keep its existing behavior, so it is not gated.
        if label is not None:
            save_cfg = getattr(instance, '_last_endpoint_config', None)
            if isinstance(save_cfg, dict):
                save_base = _normalize_api_base(save_cfg.get('api_base', '') or '')
                save_model = save_cfg.get('model', '') or ''
                held_base = _normalize_api_base(held_cfg.get('api_base', '') or '')
                held_model = held_cfg.get('model', '') or ''
                if save_base and save_model and (save_base, save_model) != (held_base, held_model):
                    logger.info('[STATE_RESTORE_SKIP] %s endpoint moved %s/%s -> %s/%s',
                                inst_name, save_base, save_model, held_base, held_model)
                    # Mirror the sibling skip paths: a label we cannot consume on this
                    # endpoint must not leak and re-fire a 404 state/load every turn.
                    clear_orphaned_state_label(instance, inst_name)
                    return False
        return restore_instance_state(instance, held_endpoint_cfg=held_cfg, label=label)
    except Exception as e:
        # Never evict on a resolution error — skip the restore.
        logger.info('[STATE_RESTORE_SKIP] %s — endpoint resolution failed: %s', inst_name, e)
        clear_orphaned_state_label(instance, inst_name)
        return False


def clear_orphaned_state_label(instance: 'AgentInstance', inst_name: str) -> None:
    """Clear a pending _state_label that could not be consumed by a restore.

    Used when endpoint resolution fails before restore_instance_state() runs — in that
    case the label would otherwise leak and re-trigger an expensive state/load on every
    later turn. Best-effort: never raises.
    """
    try:
        with instance._state_lock:
            if instance._state_label is not None:
                instance._state_label = None
                logger.debug('Cleared orphaned state label for %s (restore skipped)', inst_name)
    except Exception as e:
        logger.debug('Failed to clear state label for %s: %s', inst_name, e)


def _cleanup_old_states(api_base_no_v1: str, model: str, instance_name: str):
    """Clean up legacy timestamped state files for this instance (pre-fix artifacts).

    Per-instance tracking: once no legacy files are found for an instance, it's added to a
    set and all future cleanup checks for that instance return immediately — avoids one HTTP
    GET per save forever. If files were deleted, we keep checking in case more appear from
    other models on subsequent saves.
    """
    global _legacy_cleanup_done_instances

    if instance_name in _legacy_cleanup_done_instances:
        return

    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state"
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return

        data = resp.json()
        labels = data.get('labels', [])

        # Only match legacy timestamped format: instance_name_TIMESTAMP
        legacy_pattern = instance_name + '_'
        legacy_states = [l for l in labels if l.startswith(legacy_pattern)]

        # Delete all legacy timestamped states — the current stable label file is kept.
        for label in legacy_states:
            _delete_state(api_base_no_v1, model, label)

        # No legacy files found for this instance — mark it done to avoid perpetual HTTP calls.
        if not legacy_states:
            _legacy_cleanup_done_instances.add(instance_name)

    except Exception as e:
        logger.debug('Legacy state cleanup failed for %s: %s', instance_name, e)


def _delete_state(api_base_no_v1: str, model: str, label: str):
    """Delete a saved state by label via autoloader DELETE endpoint.

    Silently handles missing files and transient errors — cleanup is best-effort.
    """
    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state/{label}"
        httpx.delete(url, timeout=10)
    except Exception as e:
        logger.debug('State delete failed for label %s: %s', label, e)

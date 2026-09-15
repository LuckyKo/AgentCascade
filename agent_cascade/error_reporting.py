"""Compact, human-readable reporting of LLM API endpoint failures.

Leaf module (plan §3.1). Import audit — HARD constraint: this file imports ONLY the
standard library at module level plus ``agent_cascade.llm.base.ModelServiceError``
(lazy, inside functions). It MUST NOT import from ``api_router_pkg``, ``engine.*`` or
``api_integration_pkg``. openai/httpx exception types are handled purely by duck-typing
(class NAME strings + attributes) so those packages are never imported here — this keeps
the module free of circular-import risk and lets it be unit-tested without the SDKs.

Purpose: turn a raw endpoint failure (a 20-60 line httpx→openai stack dump) into ONE
compact, actionable line for console logs and agent-facing messages. See
``plans/api_failure_feedback_plan.md``.
"""

import hashlib
import re
import threading
import time
from typing import Any, Dict, Optional

# ── Tunables (module constants — NOT settings/config entries; plan §2 goal 5) ────────
#: Max characters of a raw exception message kept in a compact line.
MAX_MSG_CHARS = 160
#: Max characters of an HTTP response body excerpt kept in a compact line.
MAX_BODY_CHARS = 120
#: Terminal [SYSTEM ERROR] message lists at most this many per-endpoint lines.
MAX_ENDPOINT_LINES = 5
#: Outer-retry digest summarizes at most this many first-lines of the exhausted error.
MAX_DIGEST_ERRORS = 3

# TracebackDedup tunables (review fix #1 — counter-based pruning).
#: Full tracebacks are logged at most once per key per WINDOW_SECONDS.
TB_WINDOW_SECONDS = 60.0
#: Stale entries (last seen older than this) are pruned on the periodic sweep.
TB_PRUNE_AFTER_SECONDS = 3600.0
#: Prune sweep runs at most once per this many should_log_full_tb() calls — NOT every call,
#: so a high-rate outage does not pay an O(n) scan per attempt (≤ a few dozen scans/sec).
TB_PRUNE_EVERY_NTH_CALLS = 100

# ── Internal: exception-chain walking helpers (pure duck-typing, no SDK imports) ─────


def _clean_text(s: str, max_len: int) -> str:
    """Collapse whitespace/newlines and truncate to a single line of at most ``max_len`` chars."""
    s = ' '.join(str(s).split())
    if len(s) > max_len:
        return s[:max_len].rstrip() + '…'
    return s


def _iter_chain(e: Any, limit: int = 8):
    """Yield each exception in the causal chain, outermost first.

    Follows ``__cause__`` (raise ... from), ``__context__`` (implicit during handling) and
    the ModelServiceError-specific ``.exception`` attribute. De-duplicates by id() so a
    self-referencing chain cannot loop forever. Bounded by ``limit``.
    """
    seen = set()
    cur = e
    for _ in range(limit):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        yield cur
        nxt = getattr(cur, '__cause__', None)
        if nxt is None:
            nxt = getattr(cur, '__context__', None)
        if nxt is None:
            nxt = getattr(cur, 'exception', None)  # ModelServiceError wraps the real cause here
        cur = nxt


def _root_cause(e: Any) -> Any:
    """The innermost exception in the causal chain (the actual root cause)."""
    last = e
    for ex in _iter_chain(e):
        last = ex
    return last


def _status_code_of(ex: Any) -> Optional[str]:
    """Best-effort HTTP status code from an exception or its response, else None."""
    resp = getattr(ex, 'response', None)
    if resp is not None:
        sc = getattr(resp, 'status_code', None)
        if sc is not None:
            return str(sc)
    for attr in ('status_code', 'code'):
        v = getattr(ex, attr, None)
        if v is not None and str(v).isdigit():
            return str(v)
    return None


def _safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    """getattr that never raises. Some SDK exception properties (e.g. httpx's ``request``)
    raise instead of returning a value when unset — reporting must survive that."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _url_of(ex: Any) -> Optional[str]:
    """Best-effort request URL from an exception or its response, else None."""
    resp = _safe_get(ex, 'response')
    if resp is not None:
        req = _safe_get(resp, 'request')
        url = _safe_get(req, 'url')
        if url:
            return str(url)
    for attr in ('url', 'request_url'):
        v = _safe_get(ex, attr)
        if v:
            return str(v)
    req = _safe_get(ex, 'request')
    url = _safe_get(req, 'url')
    if url:
        return str(url)
    return None


def _body_of(ex: Any) -> Optional[str]:
    """Best-effort short body/excerpt of an HTTP error response (≤ MAX_BODY_CHARS)."""
    resp = _safe_get(ex, 'response')
    text = _safe_get(resp, 'text') if resp is not None else None
    if not text:
        body = _safe_get(ex, 'body')
        if isinstance(body, dict):
            # Common shape: {'error': {'message': ...}} or {'error': '...'}
            err = body.get('error')
            if isinstance(err, dict):
                text = err.get('message') or err.get('detail')
            elif err is not None:
                text = err
        elif body is not None and not isinstance(body, (bytes, bytearray)):
            text = body
    if not text:
        return None
    return _clean_text(text, MAX_BODY_CHARS)


def _is_name(ex: Any, *names: str) -> bool:
    """True if ``ex``'s class name (or any base-class name) is in ``names``.

    Duck-typed so openai/httpx are never imported — a fake exception with the right
    ``__class__.__name__`` behaves identically to the real SDK type.
    """
    cls = getattr(ex, '__class__', None)
    if cls is None:
        return False
    for base in getattr(cls, '__mro__', (cls,)):
        if getattr(base, '__name__', '') in names:
            return True
    return False


def _is_model_service_error(e: Any) -> bool:
    """True if ``e`` is a ModelServiceError (lazy import — keeps the module a leaf)."""
    try:
        from agent_cascade.llm.base import ModelServiceError
        return isinstance(e, ModelServiceError)
    except Exception:
        # Fallback: match on class name so a missing/heavy base.py never breaks reporting.
        return getattr(getattr(e, '__class__', None), '__name__', '') == 'ModelServiceError'


def _is_status_carrying(ex: Any) -> bool:
    """True if the exception looks like an HTTP status error (openai.APIStatusError or
    httpx.HTTPStatusError) — i.e. it carries a numeric status code."""
    return _is_name(ex, 'APIStatusError', 'HTTPStatusError')


# ── Public API: compact formatting ────────────────────────────────────────────────────


def format_endpoint_error(e: Any) -> str:
    """Compact single-line root-cause summary for an endpoint failure.

    Walks the exception chain and returns ONE line, e.g.:
      - "HTTP 502 from http://127.0.0.1:1234/v1: llama-server unreachable"
      - "HTTP 503 from http://127.0.0.1:1234/v1: Failed to load model 'Agents-A1-...'"
      - "connection error to http://127.0.0.1:1234/v1 (WinError 10055: socket buffer full)"
      - "timeout after 30s (httpx.ReadTimeout)"
      - "RuntimeError: <message>"   (fallback)

    Never raises — any unexpected input degrades to a safe fallback string.
    """
    if e is None:
        return 'no error information'
    if not isinstance(e, BaseException):
        # Non-Exception object passed by mistake — describe it safely.
        return f'{type(e).__name__}: {str(e)[:MAX_MSG_CHARS]}'

    try:
        # ModelServiceError is the canonical wrapper in this codebase (oai.py wraps every
        # openai/httpx error in one). Prefer its .code + .message, then descend into the
        # wrapped exception for a more specific summary.
        if _is_model_service_error(e):
            code = str(getattr(e, 'code', None) or '').strip()
            msg = getattr(e, 'message', None)
            if code:
                url = _url_of(e) or _url_of(getattr(e, 'exception', None))
                prefix = f'HTTP {code}' + (f' from {url}' if url else '')
                body = _body_of(getattr(e, 'exception', None)) or _clean_text(msg or '', MAX_BODY_CHARS)
                return f'{prefix}: {body}' if body else prefix
            # No code — descend into the wrapped exception for a specific line.
            inner = getattr(e, 'exception', None)
            if inner is not None:
                return format_endpoint_error(inner)
            if msg:
                return _clean_text(msg, MAX_MSG_CHARS)

        # Walk the chain (outermost → innermost). The FIRST status-carrying error wins;
        # otherwise summarize the root cause.
        root = e
        for ex in _iter_chain(e):
            root = ex
            if _is_status_carrying(ex):
                code = _status_code_of(ex) or '?'
                url = _url_of(ex)
                prefix = f'HTTP {code}' + (f' from {url}' if url else '')
                body = _body_of(ex)
                return f'{prefix}: {body}' if body else prefix

        # Root cause is not an HTTP status error — summarize it.
        name = type(root).__name__
        msg = _clean_text(str(root), MAX_MSG_CHARS)
        url = _url_of(root)
        if _is_name(root, 'ConnectError', 'ConnectionError', 'NewConnectionError'):
            base = f'connection error to {url}' if url else 'connection error'
            detail = msg if msg and msg != name else ''
            return f'{base} ({detail})' if detail else base
        if _is_name(root, 'ReadTimeout', 'ConnectTimeout', 'PoolTimeout', 'TimeoutException', 'TimeoutError'):
            return f'timeout ({name})' + (f': {msg}' if msg and msg != name else '')
        if msg:
            return f'{name}: {msg}'
        return name
    except Exception as inner_err:  # pragma: no cover - defensive; reporting must never raise
        return f'{type(e).__name__}: (formatting error: {_clean_text(str(inner_err), 80)})'


def format_crash(e: Any) -> str:
    """Compact single-line root-cause summary for a *generic* Python exception.

    Sibling of :func:`format_endpoint_error` for non-endpoint crashes (a tool function
    raising, dispatcher/plumbing errors, top-level run-thread crashes). It intentionally does
    NOT walk the chain hunting for HTTP/status-carrying frames — that is endpoint-specific and
    would produce a misleading ``RuntimeError: <msg>``-flavored line for ordinary exceptions.
    Instead it summarizes the ROOT cause (innermost exception in the causal chain) as
    ``{Type}: {message}``, or just ``{Type}`` when the message is empty.

    Preserves the leaf-module constraint of this file: stdlib-only, no SDK imports, never raises.
    """
    if e is None:
        return 'no error information'
    if not isinstance(e, BaseException):
        # Non-Exception object passed by mistake — describe it safely.
        return f'{type(e).__name__}: {_clean_text(str(e), MAX_MSG_CHARS)}'

    try:
        root = _root_cause(e)
        name = type(root).__name__
        msg = _clean_text(str(root), MAX_MSG_CHARS)
        if msg:
            return f'{name}: {msg}'
        return name
    except Exception as inner_err:  # pragma: no cover - defensive; reporting must never raise
        return f'{type(e).__name__}: (formatting error: {_clean_text(str(inner_err), 80)})'


def classify_endpoint_failure(e: Any) -> str:
    """Human-facing short category label for UI messages.

    Returns one of the fixed labels below (user-facing strings, not internal codes):
      'connection refused/unreachable', 'network timeout', 'server error (HTTP xxx)',
      'rate limited (HTTP 429)', 'model load failure', 'authentication failure',
      'unknown error'. Never raises.
    """
    if e is None:
        return 'unknown error'
    try:
        code = None
        if _is_model_service_error(e):
            c = str(getattr(e, 'code', None) or '').strip()
            if c.isdigit():
                code = c

        # Walk the chain collecting status codes (prefer an explicit ModelServiceError code).
        for ex in _iter_chain(e):
            sc = _status_code_of(ex)
            if sc is not None:
                code = sc
                break
            if _is_status_carrying(ex):
                # Status error with no numeric code — fall through to text classification.
                pass

        if code == '429':
            return 'rate limited (HTTP 429)'
        if code in ('401', '403'):
            return 'authentication failure'
        # A 503 is the classic llama.cpp "model still loading / server busy" signature — give it
        # its own actionable label rather than a generic server error.
        if code == '503':
            return 'model load failure'
        if code is not None and code.isdigit():
            # Other 5xx/4xx → generic server-side HTTP failure (4xx auth/rate handled above).
            return f'server error (HTTP {code})'

        # No numeric status — classify by exception type / text.
        for ex in _iter_chain(e):
            if _is_name(ex, 'ReadTimeout', 'ConnectTimeout', 'PoolTimeout', 'TimeoutException', 'TimeoutError'):
                return 'network timeout'
            if _is_name(ex, 'ConnectError', 'ConnectionError', 'NewConnectionError', 'ConnectionResetError',
                        'BrokenPipeError'):
                return 'connection refused/unreachable'

        text = str(e).lower()
        if 'timed out' in text or 'timeout' in text:
            return 'network timeout'
        if any(s in text for s in ('refused', 'unreachable', 'no route to host', 'connection reset', 'broken pipe',
                                   'winerror 10055', 'winerror 10061')):
            return 'connection refused/unreachable'
        if 'rate limit' in text or '429' in text:
            return 'rate limited (HTTP 429)'
        if 'unauthorized' in text or 'forbidden' in text or 'invalid api key' in text \
                or 'authentication' in text:
            return 'authentication failure'
        if 'failed to load model' in text or 'model load' in text or 'still loading' in text:
            return 'model load failure'
        if '503' in text or 'service unavailable' in text:
            return 'server error (HTTP 503)'
        return 'unknown error'
    except Exception:  # pragma: no cover - defensive
        return 'unknown error'


def summarize_exhaustion(e: Any) -> str:
    """Compact digest of a terminal "All API endpoints exhausted" error for log lines.

    If ``str(e)`` starts with the exhaustion marker, joins the first line of each
    per-endpoint error (max MAX_DIGEST_ERRORS, else '…'). Otherwise falls back to
    :func:`format_endpoint_error`. Used by the outer-retry and terminal log sites so the
    full concatenated stack dumps are never re-printed into WARNING/ERROR lines.
    """
    if e is None:
        return 'no error information'
    try:
        text = str(e)
    except Exception:
        text = repr(e)
    first_line = text.split('\n', 1)[0].strip()
    if first_line.startswith('All API endpoints exhausted'):
        lines = [ln.strip() for ln in text.split('\n')[1:] if ln.strip()]
        if not lines:
            return 'all endpoints failed'
        shown = lines[:MAX_DIGEST_ERRORS]
        suffix = '' if len(lines) <= MAX_DIGEST_ERRORS else f', …and {len(lines) - MAX_DIGEST_ERRORS} more'
        return 'all endpoints failed: ' + '; '.join(shown) + suffix
    return format_endpoint_error(e)


def build_terminal_message(e: Any, max_attempts: int) -> str:
    """Build the multi-line [SYSTEM ERROR] body shown to the agent/UI on terminal failure.

    Reads the structured ``e.endpoint_failures`` attribute (a list of compact per-endpoint
    lines attached by the router — plan §3.4). If absent, falls back to the first line of
    ``str(e)``. Lists up to MAX_ENDPOINT_LINES endpoint lines ('…and N more') and appends an
    action hint based on the dominant failure category. Never raises.
    """
    header = f'LLM unavailable after {max_attempts} retries.'
    failures = getattr(e, 'endpoint_failures', None) if e is not None else None

    if isinstance(failures, (list, tuple)) and failures:
        lines = [str(ln).strip() for ln in failures if str(ln).strip()]
        shown = lines[:MAX_ENDPOINT_LINES]
        bullets = ['  • ' + ln for ln in shown]
        if len(lines) > MAX_ENDPOINT_LINES:
            bullets.append(f'  …and {len(lines) - MAX_ENDPOINT_LINES} more')
        body_lines = [header, ' Endpoints tried:'] + bullets
    else:
        # No structured data (e.g. non-router error) — fall back to the first line of str(e).
        try:
            first = str(e).split('\n', 1)[0].strip() if e is not None else ''
        except Exception:
            first = 'unknown error'
        body_lines = [header] + ([f' {first}'] if first else [])

    hint = _action_hint(e)
    if hint:
        body_lines.append(' ' + hint)

    return '\n'.join(body_lines)


def _action_hint(e: Any) -> Optional[str]:
    """Action hint for the terminal message based on the dominant failure category.

    Dominant = the most common classify_endpoint_failure() label across endpoint_failures;
    if absent, classifies ``e`` itself. Returns None when no actionable hint applies.
    """
    labels: Dict[str, int] = {}

    def _bump(label: str) -> None:
        labels[label] = labels.get(label, 0) + 1

    failures = getattr(e, 'endpoint_failures', None) if e is not None else None
    if isinstance(failures, (list, tuple)) and failures:
        for ln in failures:
            _bump(_label_from_line(ln))
    elif e is not None:
        _bump(classify_endpoint_failure(e))

    if not labels:
        return None
    dominant = max(labels.items(), key=lambda kv: (kv[1], kv[0]))[0]

    if dominant.startswith('connection'):
        return 'Check that the LLM server is running and reachable.'
    if dominant == 'model load failure' or dominant == 'server error (HTTP 503)':
        return 'The model may still be loading — it will retry automatically.'
    if dominant.startswith('rate limited'):
        return 'Rate limit hit — requests will be throttled.'
    return None


def _label_from_line(line: str) -> str:
    """Derive a category label from a compact per-endpoint log line.

    Compact lines carry the status code ("HTTP 502 ...") or a connection/timeout phrase, so
    we classify by text rather than re-walking an exception (we only have strings here).
    """
    text = str(line).lower()
    m = re.search(r'HTTP\s+(\d{3})', text)
    if m:
        code = m.group(1)
        if code == '429':
            return 'rate limited (HTTP 429)'
        if code in ('401', '403'):
            return 'authentication failure'
        if code == '503' and ('load model' in text or 'loading' in text):
            return 'model load failure'
        return f'server error (HTTP {code})'
    if 'timeout' in text:
        return 'network timeout'
    if any(s in text for s in ('connection error', 'refused', 'unreachable', 'winerror 10055', 'winerror 10061')):
        return 'connection refused/unreachable'
    if 'rate limit' in text:
        return 'rate limited (HTTP 429)'
    if 'failed to load model' in text or 'still loading' in text:
        return 'model load failure'
    return 'unknown error'


# ── Public API: rolling-window traceback dedup ────────────────────────────────────────


class TracebackDedup:
    """Rolling-window dedup for full tracebacks (plan §3.1, review fix #1/#6).

    ``should_log_full_tb(key)`` returns True at most once per key per WINDOW_SECONDS so a
    high-rate outage logs the full traceback once per window instead of on every attempt.

    Thread-safety: a single ``threading.Lock`` guards the dict and is held for the ENTIRE
    atomic check-and-update inside ``should_log_full_tb``. It is NEVER held while logging —
    callers log outside this method (after it returns). This class takes no other locks, so
    deadlock with router._lock is impossible.

    Bounded memory: pruning is COUNTER-BASED — every TB_PRUNE_EVERY_NTH_CALLS-th call removes
    entries whose last-seen timestamp is older than TB_PRUNE_AFTER_SECONDS. At worst one O(n)
    sweep per 100 calls; during a high-rate outage that is ≤ a few dozen dict scans/sec.
    """

    def __init__(self,
                 window_seconds: float = TB_WINDOW_SECONDS,
                 prune_after_seconds: float = TB_PRUNE_AFTER_SECONDS,
                 prune_every_n_calls: int = TB_PRUNE_EVERY_NTH_CALLS):
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._prune_counter = 0
        self._window_seconds = window_seconds
        self._prune_after_seconds = prune_after_seconds
        self._prune_every_n_calls = prune_every_n_calls

    def get_tb_key(self, e: Any) -> str:
        """Stable dedup key for an exception: (endpoint base, model, root type+msg hash).

        Endpoint/model identity comes from ModelServiceError context where available;
        otherwise the key is derived from the root exception class + first 80 chars of its
        message. A different failure mode (different root type/message) yields a different
        key, so a genuinely new traceback variant still logs once per window.
    """
        if e is None:
            return 'none'
        try:
            from agent_cascade.llm.base import ModelServiceError
            is_mse = isinstance(e, ModelServiceError)
        except Exception:
            is_mse = getattr(getattr(e, '__class__', None), '__name__', '') == 'ModelServiceError'

        endpoint = ''
        model = ''
        if is_mse:
            extra = getattr(e, 'extra', None) or {}
            if isinstance(extra, dict):
                endpoint = str(extra.get('api_base') or extra.get('endpoint') or '')
                model = str(extra.get('model') or '')

        root = _root_cause(e)
        rtype = type(root).__name__
        try:
            rmsg = str(root)[:80]
        except Exception:
            rmsg = repr(root)[:80]
        digest = hashlib.sha1(f'{rtype}|{rmsg}'.encode('utf-8', 'replace')).hexdigest()[:16]
        return f"{endpoint or '?'}|{model or '?'}|{digest}"

    def should_log_full_tb(self, key: str, now: Optional[float] = None) -> bool:
        """Return True if the full traceback for ``key`` should be logged now.

        True at most once per key per window. The lock is held across the entire
        check-and-update (atomic). Counter-based pruning runs on the periodic sweep only.

        ``now`` is the wall-clock reference in seconds. Production callers may OMIT it —
        the method then resolves ``time.time()`` itself, which is safe-by-default: a caller
        that forgets to pass an explicit timestamp still gets correct (wall-clock) windowing
        and pruning rather than silently pinning every key to t=0 (which would cause permanent
        deduplication and never-prune). Tests SHOULD pass an explicit ``now`` for determinism.

        The time resolution happens BEFORE the lock is acquired so the (cheap, uncontended)
        clock read does not hold the lock; only the dict check-and-update is serialized.
        """
        if now is None:
            now = time.time()
        with self._lock:
            self._prune_counter += 1
            if self._prune_counter % self._prune_every_n_calls == 0:
                self._prune_locked(now)

            last = self._last_seen.get(key)
            if last is None or (now - last) >= self._window_seconds:
                self._last_seen[key] = now
                return True
            # Seen within the window — update last-seen so a sustained outage keeps the
            # window sliding rather than re-logging every call.
            self._last_seen[key] = now
            return False

    def _prune_locked(self, now: float) -> None:
        """Remove entries older than prune_after_seconds. Caller MUST hold self._lock."""
        if not self._last_seen:
            return
        stale = [k for k, ts in self._last_seen.items() if (now - ts) >= self._prune_after_seconds]
        for k in stale:
            del self._last_seen[k]

    def _size(self) -> int:
        """Current number of tracked keys (for tests / diagnostics)."""
        with self._lock:
            return len(self._last_seen)


#: Module-level singleton shared by the router and llm_call log sites.
TB_DEDUP = TracebackDedup()

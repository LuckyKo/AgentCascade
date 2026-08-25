"""Tool-call loop detection — parallel checker for tool-call failure loops.

Complements :func:`agent_cascade.loop_detection.detect_loop` (exact contiguous
pattern matcher) with a trailing-run scan over (function_call, function_output)
pairs. Catches two real-world failure modes the exact matcher is blind to:

* **Layer 1 — stable/terminal output run** (async-shell polling loop): ≥5
  trailing pairs with the same normalized action where each output is
  byte-identical to the previous OR matches a terminal-error signature.
  Interleaved assistant prose / user messages are ignored when pairing, so a
  "prose shield" between identical polls cannot break the run.

* **Layer 2 — near-duplicate failing command run** (pytest fixup churn): ≥6
  trailing same-tool pairs with pipe-stripped core-command similarity ≥0.85
  (difflib), an identical semantic target set (quoted filters, ``*.py`` paths,
  ``nodeid::`` targets) and an identical failure class (EXIT:n n≠0 / no-output
  exit / pytest FAILED banner). Any intervening pair from a different tool
  breaks the run — this neutralizes legitimate edit→test→fail dev cycles.

Design reference: reports/loop_detector_research.md (§5 algorithm sketches,
§4-D delivery shape). Both failure samples in loop_failure_samples/ were used
to validate thresholds with ≥2× margin on real data.

FUNCTION output normalization: raw tool replies are wrapped by the harness with
per-call varying noise (security verdict banners, elapsed-time markers,
auto-generated "Security Justification" prose, spillover/truncation notices,
ISO timestamps). :func:`_normalize_output` strips these KNOWN system-injected
formats at pair-extraction time, so FUNCTION content is treated as a WEAK
signal: detection identity comes from the LLM-generated function_calls; the
(normalized) output is used only for stability / failure-class gating. Genuine
output differences (error messages, test results, stdout) survive normalization.

The module is self-contained: it does not modify or import from
``loop_detection.py`` (aside from the shared message schema constants).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple, Union

from agent_cascade.llm.schema import ASSISTANT, FUNCTION, ROLE, Message

# ── Tunables (validated against real failure samples) ───────────────────────

#: Arg keys that vary per call but carry no semantic weight for loop purposes.
VOLATILE_ARG_KEYS = frozenset({"justification"})

#: shell_cmd control directives reduced to (directive, tool_id).
SHELL_DIRECTIVES = frozenset({"__status", "__wait", "__kill", "__ctrl_c"})

#: Output patterns that indicate a terminal (no-retry) error condition.
#: INTENTIONALLY MINIMAL — each pattern must be high-precision (a hit means the
#: retry will never succeed). Extend from field telemetry only: when a new
#: terminal-error phrasing shows up in loop incidents, add it here with a test.
TERMINAL_ERROR_RES = (
    re.compile(r"No running shell found"),
    re.compile(r"'[^']+' is not recognized as an internal or external command"),
    re.compile(r"Connection refused"),
    re.compile(r"No such process"),
)

#: "Command exited with return code N" — failure when N != 0.
_EXIT_CODE_RE = re.compile(r"Command exited with return code (\d+)")

#: pytest FAILED banner (a line like "FAILED path::Test::test_x").
_FAILED_BANNER_RE = re.compile(r"(?m)^\s*FAILED\s+\S+::\S+")

#: Output patterns that indicate a *failing* tool result (used to exclude
#: successful stable-output streaks from Layer 1). A pair whose output matches
#: none of these is considered a success and cannot form a Layer 1 run.
_FAILURE_OUTPUT_RES = (
    re.compile(r"Command exited with return code [1-9]"),
    re.compile(r"No running shell found"),
    re.compile(r"is not recognized as an internal or external command"),
    re.compile(r"(?m)^\s*FAILED\s+\S+::\S+"),
    re.compile(r"\bTraceback \(most recent call last\)"),
    re.compile(r"^\s*(Error|Exception)\s*:", re.M),
)

#: Generic error indicators (exception class names at line start). Used by the
#: Layer 1 generic branch to distinguish failing from successful byte-identical
#: outputs for NON-shell tools. Conservative: only well-known exception/errno
#: prefixes — a bare "Error" word in prose does not qualify.
_GENERIC_ERRNO_RE = re.compile(r"\[Errno \d+\]")
_GENERIC_ERROR_LINE_RE = re.compile(r"[A-Za-z_][\w.]*(?:Error|Exception)\b")


def _is_failing_output(content: str) -> bool:
    """True if the output looks like a failing tool result (non-zero exit,
    terminal error, FAILED banner, traceback or leading Error/Exception line)."""
    return any(rx.search(content) for rx in _FAILURE_OUTPUT_RES)


def _is_generic_error_output(content: str) -> bool:
    """True if the output carries a generic exception/errno indicator (used to
    tell failing from successful byte-identical outputs in Layer 1's generic
    branch, which covers non-shell tools).

    Conservative by design: the first alternative requires the error word at
    line start, so prose like "no errors were found" does not qualify.
    """
    if _GENERIC_ERRNO_RE.search(content):
        return True
    for line in content.splitlines():
        if _GENERIC_ERROR_LINE_RE.match(line.lstrip()):
            return True
    return False

#: Semantic targets: quoted filter phrases (≥3 chars), .py paths, nodeid:: targets.
_TARGET_RE = re.compile(r'"([^"]{3,})"|(\S+\.py\S*)|([\w.]+::[\w.:]+)')

#: Window size in messages (matches the legacy detector's window).
_WINDOW_SIZE = 40


# ── Message normalization helpers ────────────────────────────────────────────

def _as_dict(m) -> dict:
    """Normalize a message to dict form (handles Message objects and dicts)."""
    if isinstance(m, dict):
        return m
    if hasattr(m, "model_dump"):
        try:
            return m.model_dump()
        except (AttributeError, TypeError):
            pass
    return {
        ROLE: getattr(m, "role", ""),
        "content": getattr(m, "content", ""),
        "name": getattr(m, "name", None),
        "function_call": getattr(m, "function_call", None),
    }


def _text_of(content) -> str:
    """Extract plain text from a message content (handles multimodal lists).

    ContentItem items are either dicts ({"type": "text", "text": ...}) or
    schema.ContentItem objects ({'text': ...}); both shapes are supported.
    """
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or ""
            else:
                text = getattr(item, "text", None) or ""
            parts.append(str(text))
        return " ".join(p for p in parts if p)
    return str(content or "")


def _fc_info(m: dict):
    """Return (tool_name, args_json_str) for a function_call message, else None."""
    fc = m.get("function_call")
    if not fc:
        return None
    if isinstance(fc, dict):
        name = fc.get("name") or ""
        args = fc.get("arguments", "")
    else:
        name = getattr(fc, "name", "") or ""
        args = getattr(fc, "arguments", "") or ""
    return str(name), (args if isinstance(args, str) else json.dumps(args))


# ── FUNCTION output normalization ───────────────────────────────────────────

#: Security verdict banner line: "APPROVED: Command exited with return code 1.
#: [TRUNCATED] (elapsed 11.3s)" / "AUTO-APPROVED: ..." (operation_manager/shell.py)
#: or "REJECTED: <reason>" (shell_cmd.py). The verdict prefix itself is system
#: noise, but the status sentence may carry exit-code info that MUST survive.
_VERDICT_BANNER_RE = re.compile(r"^(?:AUTO-)?(?:APPROVED|REJECTED):\s*", re.M)

#: "(elapsed 12.3s)" style markers — appended by shell.py / shell_cmd.py and
#: varying on every call.
_ELAPSED_MARKER_RE = re.compile(r"\s*\(elapsed \d+(?:\.\d+)?s\)")

#: "Completed in 12.3 s (exit code 1)." line from ⟨shell_cmd completed⟩ async
#: messages — the elapsed figure varies per call; the status is kept.
_COMPLETED_IN_RE = re.compile(r"Completed in \d+(?:\.\d+)? s \(")

#: "Security Justification: <auto-generated prose>" block (shell.py, file ops).
#: The prose is regenerated on every call and carries no loop signal, so the
#: whole paragraph is dropped. Continuation lines are consumed ONLY while they
#: do not look like a known section marker (STDOUT/STDERR/Output:) or a pytest
#: FAILED banner — in real wrapped outputs the justification sits between the
#: verdict line and the output body, separated by a blank line, but we must not
#: rely on that: swallowing a "FAILED ..." line would destroy Layer 2's
#: TESTFAIL failure class.
_SECURITY_JUST_RE = re.compile(
    r"(?m)^Security Justification:[^\n]*"
    r"(?:\n(?!\s*$)(?!(?:STDOUT|STDERR|Output):)(?!FAILED\s)[^\n]*)*"
)

#: Spillover/truncation notices — the char count and saved path vary per run:
#:   "[TRUNCATED — Character limit exceeded. Full output (4996 chars) saved to: <path>]"
#:   "[SPILL FILE TRUNCATED — exceeded maximum size]"
#:   "[... 1234 chars omitted ...]" (mid-mode truncation marker)
_TRUNCATION_NOTICE_RE = re.compile(
    r"(?m)^\s*\[(?:TRUNCATED|SPILL FILE TRUNCATED)[^\n]*\]\s*$"
    r"|^\s*\[\.\.\. \d+ chars omitted \.\.\.\]\s*$"
)

#: ISO 8601 timestamps (e.g. "2026-08-24T13:00:48.976877", "2026-08-24 13:00:48")
#: — system-injected in log-style outputs and varying on every line.
_ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def _normalize_output(content: str) -> str:
    """Strip KNOWN system-injected noise from a FUNCTION (tool reply) content.

    Tool replies are wrapped by the harness with per-call varying noise —
    security verdict banners, elapsed-time markers, auto-generated
    "Security Justification" prose, spillover/truncation notices (paths and
    char counts differ every run) and ISO timestamps. That noise makes
    byte-identical outputs look different and hinders loop detection, while
    carrying no signal: the detection identity comes from the LLM-generated
    function_calls, and the output is used only as a weak signal for
    stability / failure-class gating.

    Conservative by design — only KNOWN system-injected formats are removed;
    genuine output differences (error messages, test results, stdout) MUST
    survive normalization. In particular:
      * "APPROVED: Command exited with return code 1." → the verdict prefix is
        dropped but "Command exited with return code 1." survives, so
        ``_fail_class`` still extracts EXIT:1;
      * a line that merely CONTAINS "(elapsed 5s)" in genuine output keeps the
        surrounding text — only the parenthesized marker itself is removed.

    Whitespace runs are collapsed afterwards (a dropped banner/justification
    block can leave blank-line gaps that would break byte-identity).
    """
    if not content:
        return ""
    s = _VERDICT_BANNER_RE.sub("", content)
    s = _ELAPSED_MARKER_RE.sub("", s)
    s = _COMPLETED_IN_RE.sub("Completed in (", s)
    s = _SECURITY_JUST_RE.sub("", s)
    s = _TRUNCATION_NOTICE_RE.sub("", s)
    s = _ISO_TIMESTAMP_RE.sub("", s)
    # Collapse whitespace runs (dropped blocks leave blank-line gaps that would
    # break byte-identity), then restore line structure for patterns that are
    # line-anchored (e.g. the pytest FAILED banner regex): a dropped wrapper
    # block must not glue two lines together and hide a line-start marker.
    s = " ".join(s.split())
    return re.sub(r" (?=FAILED\s)", "\n", s)


# ── Pair extraction ─────────────────────────────────────────────────────────

def _extract_pairs(
    messages: List[Union[dict, Message]],
) -> List[Tuple[int, int, str, str, str]]:
    """Extract ``(fc_idx, func_idx, tool_name, args_json, output_text)`` pairs.

    Index semantics (CRITICAL for pop_count correctness): ``fc_idx`` and
    ``func_idx`` are ABSOLUTE indices into the full ``messages`` list (not
    window-relative). The FUNCTION message is NOT guaranteed to sit at
    ``fc_idx + 1`` — intervening assistant prose / user / system messages can
    separate an FC from its output, so both indices are stored explicitly and
    pop_count is computed from ``func_idx``.

    Scans the last ``_WINDOW_SIZE`` messages. Each ASSISTANT message carrying a
    function_call is paired with the next FUNCTION-role message; intervening
    assistant prose / user / system messages are ignored (this defeats
    Sample 1's "prose shield"). FCs without a following FUNCTION output before
    the window ends are dropped.

    The stored ``output_text`` is NORMALIZED via :func:`_normalize_output` —
    system-injected wrapper noise (security verdict banners, elapsed markers,
    auto-generated justification prose, spillover notices, ISO timestamps) is
    stripped BEFORE any comparison or classification, so BOTH layers operate
    on normalized content only. The output remains a weak signal: detection
    identity comes from the LLM-generated function_calls; the (normalized)
    output is used for stability / failure-class gating only.
    """
    if not messages:
        return []

    window_start = max(0, len(messages) - _WINDOW_SIZE)
    pairs: List[Tuple[int, int, str, str, str]] = []
    pending: Optional[Tuple[int, str, str]] = None  # (fc_idx, name, args)

    for i in range(window_start, len(messages)):
        m = _as_dict(messages[i])
        role = m.get(ROLE) or ""
        if role == ASSISTANT:
            info = _fc_info(m)
            if info and pending is None:
                pending = (i, info[0], info[1])
        elif role == FUNCTION:
            if pending is not None:
                # Normalize at extraction time so both layers (byte-identity,
                # failure classification) operate on noise-stripped content only.
                pairs.append((pending[0], i, pending[1], pending[2],
                              _normalize_output(_text_of(m.get("content", "")))))
                pending = None

    return pairs


# ── Layer 1 helpers ─────────────────────────────────────────────────────────

def _norm_action(tool_name: str, args_json: str) -> Optional[str]:
    """Normalize a function_call to a comparable action string.

    shell_cmd control directives reduce to ``shell_cmd:<directive>:<tool_id>``;
    other calls use canonical sorted-keys JSON with volatile keys dropped.
    Returns None if the args cannot be parsed (call is skipped by Layer 1).
    """
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None

    if tool_name == "shell_cmd":
        cmd = args.get("command", "")
        if isinstance(cmd, str) and cmd in SHELL_DIRECTIVES:
            return f"shell_cmd:{cmd}:{args.get('tool_id', '')}"

    cleaned = {k: v for k, v in args.items() if k not in VOLATILE_ARG_KEYS}
    return f"{tool_name}:{json.dumps(cleaned, sort_keys=True, ensure_ascii=False)}"


def _is_terminal_output(content: str) -> bool:
    """True if the output matches a terminal-error signature."""
    return any(rx.search(content) for rx in TERMINAL_ERROR_RES)


#: Error markers that make an otherwise stable output "noisy" — repeated
#: identical outputs containing these are treated as live/progressive state,
#: not a stuck loop (protects read_file-style retries and error-retry cycles).
#: High-precision forms only: bare words like "Error" in prose must NOT trigger
#: this, or detection would be suppressed on benign stable outputs.
_NOISY_OUTPUT_MARKERS = (
    re.compile(r"\bTraceback \(most recent call last\)"),
    re.compile(r"(?m)^\s*(Error|Exception)\s*:\s*\S"),
)


def _is_noisy_output(content: str) -> bool:
    """True if the output carries error markers (likely live/progressive state)."""
    return any(rx.search(content) for rx in _NOISY_OUTPUT_MARKERS)


# ── Layer 2 helpers ─────────────────────────────────────────────────────────

def _core_command(args_json: str) -> Optional[str]:
    """Pipe-stripped core command of a shell_cmd call (first segment before |)."""
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
    except (json.JSONDecodeError, TypeError):
        return None
    cmd = args.get("command", "") if isinstance(args, dict) else ""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return cmd.split("|")[0].strip()


def _targets(args_json: str) -> frozenset:
    """Semantic target set of a command: quoted filters, .py paths, nodeids."""
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
    except (json.JSONDecodeError, TypeError):
        return frozenset()
    cmd = args.get("command", "") if isinstance(args, dict) else ""
    if not isinstance(cmd, str):
        return frozenset()
    found = set()
    for m in _TARGET_RE.finditer(cmd):
        found.add(m.group(1) or m.group(2) or m.group(3))
    return frozenset(found)


def _fail_class(content: str) -> Optional[str]:
    """Classify a tool output as a failure, or None if not a classifiable failure.

    Returns one of ``EXIT:<n>`` (n≠0), ``NOOUT``, ``TESTFAIL``.
    """
    m = _EXIT_CODE_RE.search(content)
    if m and int(m.group(1)) != 0:
        return f"EXIT:{m.group(1)}"
    if not content.strip():
        return "NOOUT"
    if _FAILED_BANNER_RE.search(content):
        return "TESTFAIL"
    return None


# ── Trailing-run scans ──────────────────────────────────────────────────────

def _layer1_trailing_run(
    pairs: List[Tuple[int, int, str, str, str]], min_stable: int = 5
) -> Optional[Tuple[str, int]]:
    """Layer 1: trailing run of ≥min_stable same normalized-action pairs where
    each output is byte-identical to the previous OR both match a terminal-error
    signature. Only FAILING outputs form runs — successful identical streaks
    (e.g. repeated identical read_file) are not loops. Returns
    (reason, pair_start_index_in_pairs) or None."""
    if len(pairs) < min_stable:
        return None

    run_end = len(pairs) - 1  # index of last pair in the run (== last pair overall)
    prev_action = _norm_action(pairs[run_end][2], pairs[run_end][3])
    if prev_action is None:
        return None
    prev_out = pairs[run_end][4]

    # A trailing run only forms on FAILING outputs. Two ways an output counts
    # as failing (conservative, to protect successful identical streaks such as
    # repeated read_file of the same file):
    #   * primary: matches _FAILURE_OUTPUT_RES (exit codes, terminal signatures,
    #     FAILED banner, traceback, leading Error:/Exception: line)
    #   * generic: carries a generic exception/errno indicator
    #     (_GENERIC_ERROR_RES) — covers non-shell tools like read_file returning
    #     "FileNotFoundError: ..." repeatedly.
    def _failing(content: str) -> bool:
        return _is_failing_output(content) or _is_generic_error_output(content)

    if not _failing(prev_out):
        # The last pair succeeded — no trailing failing/stable run exists.
        return None
    run_start = run_end

    i = run_end - 1
    while i >= 0:
        action = _norm_action(pairs[i][2], pairs[i][3])
        out = pairs[i][4]
        if action != prev_action:
            break
        # Successful outputs (e.g. identical successful read_file streaks) can
        # never form a run.
        if not _failing(out) or not _failing(prev_out):
            break
        # Byte-identical clean outputs count directly (generic branch — this is
        # what catches non-shell tools with repeated identical errors). Noisy
        # outputs (traceback / Error: banners — likely live/progressive state)
        # must instead both match a terminal-error signature.
        if out != prev_out or _is_noisy_output(out) or _is_noisy_output(prev_out):
            if not (_is_terminal_output(out) and _is_terminal_output(prev_out)):
                break
        run_start = i
        prev_out = out
        i -= 1

    run_len = run_end - run_start + 1
    if run_len < min_stable:
        return None

    reason = (
        f"tool-call loop: stable/terminal output — action '{prev_action}' repeated "
        f"{run_len} times with identical or terminal-error outputs"
    )
    return reason, run_start


def _layer2_trailing_run(
    pairs: List[Tuple[int, int, str, str, str]], min_fuzzy: int = 6, sim_threshold: float = 0.85
) -> Optional[Tuple[str, int]]:
    """Layer 2: trailing run of ≥min_fuzzy same-tool failing pairs with
    core-command similarity ≥sim_threshold, identical target set and identical
    failure class. Any pair from a different tool (or unclassifiable output)
    breaks the run. Returns (reason, pair_start_index_in_pairs) or None.

    SCOPE: shell_cmd only — ``_core_command``/``_targets`` parse the
    ``command`` arg, so non-shell tools never form a Layer 2 run by design
    (near-duplicate failing calls from other tools are out of scope)."""
    if len(pairs) < min_fuzzy:
        return None

    last = pairs[-1]
    fail_cls = _fail_class(last[4])
    core = _core_command(last[3])
    tgts = _targets(last[3])
    if fail_cls is None or core is None:
        return None

    run_end = len(pairs) - 1
    run_start = run_end
    i = run_end - 1
    while i >= 0:
        name, args_json, out = pairs[i][2], pairs[i][3], pairs[i][4]
        if name != last[2]:
            break
        fc = _fail_class(out)
        c = _core_command(args_json)
        t = _targets(args_json)
        if fc is None or fc != fail_cls or c is None or t != tgts:
            break
        sim = SequenceMatcher(None, c, core).ratio()
        if sim < sim_threshold:
            break
        run_start = i
        i -= 1

    run_len = run_end - run_start + 1
    if run_len < min_fuzzy:
        return None

    reason = (
        f"tool-call loop: near-duplicate failing command — tool '{last[2]}' invoked "
        f"{run_len} times with similar commands, identical targets and failure class {fail_cls}"
    )
    return reason, run_start


# ── Public API ──────────────────────────────────────────────────────────────

def detect_tool_loop(
    messages: List[Union[dict, Message]],
    min_stable: int = 5,
    min_fuzzy: int = 6,
    sim_threshold: float = 0.85,
) -> Optional[Tuple[str, int]]:
    """Detect tool-call loops invisible to the exact contiguous matcher.

    Args:
        messages: Full conversation history (list of dicts or Message objects).
        min_stable: Layer 1 threshold — trailing pairs with stable/terminal outputs.
        min_fuzzy: Layer 2 threshold — trailing near-duplicate failing pairs.
        sim_threshold: Layer 2 core-command similarity floor (difflib ratio).

    Returns:
        ``(reason, pop_count)`` if a tool-call loop is detected, else ``None``.
        ``pop_count`` follows the same convention as :func:`detect_loop`:
        number of messages to remove from the end so that ONE occurrence of the
        trailing run remains (i.e., keep the first pair of the run — its FC and
        FUNCTION output plus any interleaved prose belonging to that iteration
        — and drop everything after).

    Scope: Layer 1 covers any tool; Layer 2 is shell_cmd-only by design.

    Example:
        info = detect_tool_loop(messages)
        if info:
            reason, pop_count = info
            logger.warning(f"Tool-call loop: {reason}, rolling back {pop_count} msgs")
    """
    pairs = _extract_pairs(messages)
    if len(pairs) < min_stable:
        return None

    hit = _layer1_trailing_run(pairs, min_stable)
    if not hit:
        hit = _layer2_trailing_run(pairs, min_fuzzy, sim_threshold)
    if not hit:
        return None

    reason, pair_start = hit
    # pop_count from the FUNCTION index of the first pair in the run (absolute
    # into the full messages list): dropping everything after it leaves exactly
    # one occurrence of the run — the first pair plus any prose between its FC
    # and output. Using fc_idx + 2 here would be wrong when prose intervenes.
    func_idx = pairs[pair_start][1]
    pop_count = len(messages) - (func_idx + 1)
    return reason, max(pop_count, 0)

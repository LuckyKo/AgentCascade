"""Tests for tool-call loop detection (agent_cascade.tool_loop_detect).

Covers (per reports/loop_detector_research.md §6):
- Fixture regression tests on trimmed real failure samples (Layer 1 / Layer 2)
- Legacy detector pin: full raw sample logs through detect_loop still return None
- False-positive battery (retry-until-success, live progress polling, exploratory
  streaks, multi-file surveys, edit-interleaved retries, stable successful reads,
  threshold boundaries)
- Robustness (empty/short lists, malformed JSON args, missing outputs, Message
  objects vs dicts)
- Integration through ExecutionEngine._pre_llm_checks using the fake-engine
  pattern from TestMaxAutoRollbacksEnforcement: rollback path, log-only staged
  rollout, flag-off no-op, telemetry loop_type="tool"

All tests are self-contained — no LLM or API server required.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.loop_detection import detect_loop
from agent_cascade.tool_loop_detect import detect_tool_loop
from agent_cascade.llm.schema import (
    SYSTEM, USER, ASSISTANT, FUNCTION, Message, FunctionCall,
)


# ──────────────────────────────────────────────
# Helpers — message factories & fixture loading
# ──────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLES_DIR = Path(__file__).resolve().parents[1].parent / "loop_failure_samples"


def _fc_msg(tool: str, args: dict):
    """Assistant message carrying a function_call."""
    return Message(
        role=ASSISTANT, content="",
        function_call=FunctionCall(name=tool, arguments=json.dumps(args)),
    )


def _fn_msg(tool: str, output: str):
    """FUNCTION-role tool output message."""
    return Message(role=FUNCTION, content=output, name=tool)


def _prose(text: str):
    return Message(role=ASSISTANT, content=text)


#: Sample-1-style terminal error reply (shared by all __status poll fixtures).
POLL_OUTPUT = "No running shell found for agent 'kv-restore-confirm' with tool_id 1."

#: Canonical failing pytest command used across the FP battery.
PYTEST_CMD = 'python -m pytest tests/test_x.py::test_y -x 2>&1 | findstr /n "INFO"'


def _poll_pair(justification="Check progress"):
    """Sample-1-style __status poll pair (FC + terminal-error output)."""
    return [
        _fc_msg("shell_cmd", {"command": "__status", "tool_id": "1", "justification": justification}),
        _fn_msg("shell_cmd", POLL_OUTPUT),
    ]


def _pytest_pair(cmd: str, output: str):
    return [_fc_msg("shell_cmd", {"command": cmd}), _fn_msg("shell_cmd", output)]


def failing_poll_pairs(count, user="poll"):
    """`count` identical __status poll pairs with varying justification (Layer 1 fixture)."""
    msgs = [Message(role=USER, content=user)]
    for i in range(count):
        msgs += _poll_pair(justification=f"Check {i}")
    return msgs


def churned_pytest_pairs(count, user="run"):
    """`count` failing pytest pairs whose outputs differ only in GENUINE substantive
    content (line number + prose) — isolates Layer 2; wrapper-only churn would be
    normalized away and let Layer 1 fire at its own threshold."""
    msgs = [Message(role=USER, content=user)]
    for i in range(count):
        out = (f"APPROVED: Command exited with return code 1. (elapsed {11.0 + i * 3.7}s)\n"
               f"Security Justification: auto prose variant {i}\n\n"
               f"{100 + i}: some substantive line that differs per run")
        msgs += _pytest_pair(PYTEST_CMD, out)
    return msgs


def identical_pytest_pairs(count, output, user="run"):
    """`count` failing pytest pairs with a byte-identical output (Layer 1 fixture)."""
    msgs = [Message(role=USER, content=user)]
    for _ in range(count):
        msgs += _pytest_pair(PYTEST_CMD, output)
    return msgs


def read_error_pairs(count, path="missing.txt", user="read it"):
    """`count` identical failing read_file pairs (Layer 1 generic branch fixture)."""
    err = "FileNotFoundError: [Errno 2] No such file or directory: 'missing.txt'"
    msgs = [Message(role=USER, content=user)]
    for _ in range(count):
        msgs.append(_fc_msg("read_file", {"path": path}))
        msgs.append(_fn_msg("read_file", err))
    return msgs


def _load_fixture(name: str):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_raw_sample(name: str):
    """Load a raw failure sample from the workspace (skips tests if absent)."""
    path = SAMPLES_DIR / name
    if not path.exists():
        pytest.skip(f"raw sample not available: {path}")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()][1:]  # drop metadata line


# ══════════════════════════════════════════════
# PART 1 — Fixture regression tests
# ══════════════════════════════════════════════

class TestFixtures:
    """Regression fixtures trimmed from the two real failure samples."""

    def test_sample1_tail_layer1_fires(self):
        msgs = _load_fixture("tool_loop_sample1_tail.jsonl")
        result = detect_tool_loop(msgs)
        assert result is not None, "Layer 1 should fire on sample-1 tail (async-shell polling)"
        reason, pop_count = result
        assert reason.startswith("tool-call loop:")
        assert "__status" in reason, "reason should mention the poll directive"
        assert "stable/terminal output" in reason
        assert pop_count > 0

    def test_sample2_tail_fires(self):
        """Sample-2 tail must be detected; post-normalization it fires via Layer 1
        (byte-identical run) instead of Layer 2, with the trigger point unchanged."""
        msgs = _load_fixture("tool_loop_sample2_tail.jsonl")
        result = detect_tool_loop(msgs)
        assert result is not None, "detector should fire on sample-2 tail"
        reason, pop_count = result
        assert reason.startswith("tool-call loop:")
        assert "stable/terminal output" in reason or "near-duplicate failing command" in reason
        assert pop_count > 0

    def test_sample2_tail_trigger_point_unchanged(self):
        """Normalization must not shift the sample-2 trigger point: the run of
        8 identical normalized 'EXIT:1 / No output produced.' polls starts at
        the same pair as before normalization (pop_count == 15)."""
        msgs = _load_fixture("tool_loop_sample2_tail.jsonl")
        _, pop_count = detect_tool_loop(msgs)
        assert pop_count == 15, f"trigger point shifted: {pop_count} != 15"

    def test_layer2_still_fires_on_synthetic_churn(self):
        """Layer 2 must fire when outputs differ in GENUINE content (varying line
        number), which normalization cannot strip — only fuzzy matching can chain."""
        msgs = churned_pytest_pairs(6)
        result = detect_tool_loop(msgs)
        assert result is not None, "Layer 2 should fire on near-duplicate failing churn"
        reason, pop_count = result
        assert "near-duplicate failing command" in reason
        assert "EXIT:1" in reason
        assert pop_count > 0

    def test_fixture_pop_count_keeps_first_pair(self):
        """pop_count convention: dropping that many messages leaves ONE pair of the run."""
        msgs = _load_fixture("tool_loop_sample1_tail.jsonl")
        reason, pop_count = detect_tool_loop(msgs)
        trimmed = msgs[:len(msgs) - pop_count]
        # After trimming, the trailing run must be shorter than the threshold (5),
        # i.e. only the first pair of the run remains.
        pairs_after = len([m for m in trimmed if isinstance(m, dict) and m.get("function_call")])
        assert pop_count > 0
        assert len(msgs) - pop_count < len(msgs)

    def test_pop_count_with_prose_between_fc_and_output(self):
        """CRITICAL regression: prose between FC and FUNCTION output must not
        break the pop_count math — after rollback exactly one pair remains."""
        # 5 identical __status polls; every iteration has assistant prose both
        # before the FC and between FC and output.
        out = POLL_OUTPUT
        msgs = [Message(role=USER, content="poll")]
        for i in range(5):
            msgs.append(_prose(f"checking status {i}"))
            msgs.append(_fc_msg("shell_cmd", {"command": "__status", "tool_id": "1"}))
            msgs.append(_prose(f"waiting for output {i}"))  # prose between FC and FUNCTION
            msgs.append(_fn_msg("shell_cmd", out))
        result = detect_tool_loop(msgs)
        assert result is not None, "Layer 1 should fire on 5 identical terminal-error polls"
        reason, pop_count = result
        trimmed = msgs[:len(msgs) - pop_count]
        # Exactly ONE function_call and exactly ONE function output remain.
        fc_left = [m for m in trimmed if isinstance(m, Message) and m.function_call is not None]
        fn_left = [m for m in trimmed if isinstance(m, Message) and m.role == FUNCTION]
        assert len(fc_left) == 1, f"expected exactly 1 FC after rollback, got {len(fc_left)}"
        assert len(fn_left) == 1, f"expected exactly 1 FUNCTION output after rollback, got {len(fn_left)}"
        # The remaining pair is the FIRST iteration of the run.
        assert fc_left[0].function_call.arguments == _fc_msg("shell_cmd", {"command": "__status", "tool_id": "1"}).function_call.arguments
        assert fn_left[0].content == out


# ══════════════════════════════════════════════
# PART 2 — Legacy detector pin
# ══════════════════════════════════════════════

class TestLegacyDetectorUnaffected:
    """The exact-match legacy detector must still return None on both raw samples."""

    def test_raw_sample1_legacy_none(self):
        msgs = _load_raw_sample("async_shell_polling_loop_20260821_kv-restore-confirm.jsonl")
        assert detect_loop(msgs) is None, "legacy detector should not flag sample 1"

    def test_raw_sample2_legacy_none(self):
        msgs = _load_raw_sample("coder_impl_phase1_D_fixup_20260824_124318.jsonl")
        assert detect_loop(msgs) is None, "legacy detector should not flag sample 2"


# ══════════════════════════════════════════════
# PART 3 — False-positive battery
# ══════════════════════════════════════════════

class TestFalsePositiveBattery:
    """Legitimate tool usage patterns must NOT be flagged."""

    def test_retry_until_success(self):
        """4 failing retries followed by success — no detection."""
        msgs = [Message(role=USER, content="run the test")]
        for i in range(4):
            msgs += _pytest_pair(PYTEST_CMD, f"APPROVED: Command exited with return code 1. (elapsed {i + 10}s)")
        msgs += _pytest_pair(PYTEST_CMD, "APPROVED: Command completed successfully.")
        assert detect_tool_loop(msgs) is None

    def test_live_progress_polling_changing_outputs(self):
        """10 polls with changing progress outputs — no detection."""
        msgs = [Message(role=USER, content="watch the build")]
        for i in range(10):
            out = f"Build progress: {i * 10}% done. Compiling module_{i}..."
            msgs.append(_fc_msg("shell_cmd", {"command": "__status", "tool_id": "7"}))
            msgs.append(_fn_msg("shell_cmd", out))
        assert detect_tool_loop(msgs) is None

    def test_exploratory_grep_read_streak(self):
        """Many different grep/read_file calls — no detection."""
        msgs = [Message(role=USER, content="find the bug")]
        for i in range(8):
            msgs.append(_fc_msg("grep", {"pattern": f"error_{i}", "path": "src"}))
            msgs.append(_fn_msg("grep", f"src/file{i}.py:12: match {i}"))
            msgs.append(_fc_msg("read_file", {"path": f"src/file{i}.py"}))
            msgs.append(_fn_msg("read_file", f"line {i}: code content number {i}"))
        assert detect_tool_loop(msgs) is None

    def test_multi_file_failing_survey(self):
        """Failing tests across DIFFERENT files (different targets) — no detection."""
        msgs = [Message(role=USER, content="survey the failing suite")]
        for i in range(8):
            cmd = f'python -m pytest tests/test_module_{i}.py -x 2>&1 | findstr /n "INFO"'
            out = (f"APPROVED: Command exited with return code 1. (elapsed {i + 9}s)\n"
                   f"FAILED tests/test_module_{i}.py::test_case_{i}")
            msgs += _pytest_pair(cmd, out)
        assert detect_tool_loop(msgs) is None

    def test_failing_test_interleaved_with_edit_file(self):
        """7 failing pytest runs each followed by an edit_file — run always broken."""
        out = "APPROVED: Command exited with return code 1. (elapsed 11.3s)"
        msgs = [Message(role=USER, content="fix the test")]
        for i in range(7):
            msgs += _pytest_pair(PYTEST_CMD, out)
            msgs.append(_fc_msg("edit_file", {"path": "tests/test_x.py", "new_content": f"v{i}"}))
            msgs.append(_fn_msg("edit_file", f"OK: edited tests/test_x.py (version {i})"))
        assert detect_tool_loop(msgs) is None

    def test_identical_successful_read_file_streak(self):
        """Repeated identical successful read_file calls — no detection (no failure class)."""
        msgs = [Message(role=USER, content="read it again")]
        for i in range(8):
            msgs.append(_fc_msg("read_file", {"path": "src/main.py"}))
            msgs.append(_fn_msg("read_file", "def main():\n    print('hello')"))
        assert detect_tool_loop(msgs) is None

    @pytest.mark.parametrize("n_pairs, expected", [
        (4, None),   # below Layer 1 threshold (5)
        (5, True),   # at Layer 1 threshold
    ])
    def test_layer1_fires_at_threshold_of_5_pairs(self, n_pairs, expected):
        msgs = failing_poll_pairs(n_pairs)
        result = detect_tool_loop(msgs)
        if expected is None:
            assert result is None
        else:
            assert result is not None

    @pytest.mark.parametrize("n_pairs, expected", [
        (5, None),   # below Layer 2 threshold (6)
        (6, True),   # at Layer 2 threshold
    ])
    def test_layer2_fires_at_threshold_of_6_pairs(self, n_pairs, expected):
        # Genuinely varying outputs isolate Layer 2 — wrapper-only churn is
        # normalized away and would let Layer 1 fire at its own threshold (5).
        msgs = churned_pytest_pairs(n_pairs)
        result = detect_tool_loop(msgs)
        if expected is None:
            assert result is None
        else:
            assert result is not None

    def test_similarity_just_below_threshold(self):
        """Core-command similarity just below 0.85 (min pairwise ≈ 0.84) — no detection."""
        base = 'python -m pytest tests/test_x.py::test_y -x --timeout=30 --maxfail=1 -q'
        # ~27-char unique tail per command → min pairwise sim ≈ 0.84 < 0.85
        suffixes = [f"--filter-token={chr(97 + i)}{'z' * (i * 6)}" for i in range(6)]
        out = "APPROVED: Command exited with return code 1. (elapsed 11.3s)"
        msgs = [Message(role=USER, content="run")]
        for sfx in suffixes:
            msgs += _pytest_pair(base + " " + sfx, out)
        assert detect_tool_loop(msgs) is None

    def test_similarity_at_threshold_fires(self):
        """6 pairs with identical core commands (sim 1.0 ≥ 0.85) — detection."""
        msgs = identical_pytest_pairs(6, "APPROVED: Command exited with return code 1. (elapsed 11.3s)")
        assert detect_tool_loop(msgs) is not None

    def test_similarity_just_above_threshold(self):
        """Core-command similarity just above 0.85 — detection fires.

        Robustness (review finding #5): the test asserts on the ACTUALLY
        COMPUTED difflib ratio rather than trusting fixed suffix lengths to
        stay above threshold if the base command ever changes."""
        from difflib import SequenceMatcher
        base = 'python -m pytest tests/test_x.py::test_y -x --timeout=30 --maxfail=1 -q'
        # ~4-char unique tail per command → pairwise sim ≈ 0.98 > 0.85
        suffixes = [f"-t{chr(97 + i)}" for i in range(6)]
        cmds = [base + " " + sfx for sfx in suffixes]
        min_sim = min(SequenceMatcher(None, a, b).ratio()
                      for i, a in enumerate(cmds) for b in cmds[i + 1:])
        assert min_sim > 0.85, f"fixture premise broken: min pairwise sim {min_sim:.4f} ≤ 0.85"
        out = "APPROVED: Command exited with return code 1. (elapsed 11.3s)"
        msgs = [Message(role=USER, content="run")]
        for sfx in suffixes:
            msgs += _pytest_pair(base + " " + sfx, out)
        assert detect_tool_loop(msgs) is not None

    def test_non_shell_identical_error_streak_fires_layer1(self):
        """Byte-identical errors from a NON-shell tool (read_file ×5) must be
        caught by Layer 1's generic branch."""
        msgs = read_error_pairs(5)
        result = detect_tool_loop(msgs)
        assert result is not None, "identical non-shell error streak should fire Layer 1"
        reason, pop_count = result
        assert reason.startswith("tool-call loop:")
        assert pop_count > 0

    def test_non_shell_identical_error_below_threshold(self):
        """4 identical non-shell errors — below Layer 1 threshold, no detection."""
        assert detect_tool_loop(read_error_pairs(4)) is None

    def test_terminal_signature_connection_refused(self):
        """'Connection refused' is a terminal signature — 5 polls with that
        byte-identical output fire Layer 1 (the exit-code line keeps the output
        classifiable; the terminal signature lets identical outputs chain)."""
        out = "APPROVED: Command exited with return code 1.\nConnection refused (host api.internal:8080)"
        msgs = [Message(role=USER, content="poll")]
        for i in range(5):
            msgs += _poll_pair(justification=f"Check {i}")
            # override the output with the connection-refused text
            msgs[-1] = _fn_msg("shell_cmd", out)
        assert detect_tool_loop(msgs) is not None

    def test_different_failure_class_breaks_run(self):
        """Alternating EXIT:1 and TESTFAIL outputs — run broken, no detection."""
        out_exit = "APPROVED: Command exited with return code 1. (elapsed 11.3s)"
        out_fail = ("APPROVED: Command completed successfully.\n"
                    "FAILED tests/test_x.py::test_y")
        msgs = [Message(role=USER, content="run")]
        for i in range(8):
            msgs += _pytest_pair(PYTEST_CMD, out_exit if i % 2 == 0 else out_fail)
        assert detect_tool_loop(msgs) is None

    def test_success_output_breaks_run(self):
        """5 failing + 1 success at the tail — trailing run is only 1, no detection."""
        out = "APPROVED: Command exited with return code 1. (elapsed 11.3s)"
        msgs = identical_pytest_pairs(5, out)
        msgs += _pytest_pair(PYTEST_CMD, "APPROVED: Command completed successfully.")
        assert detect_tool_loop(msgs) is None


# ══════════════════════════════════════════════
# PART 4 — Robustness
# ══════════════════════════════════════════════

class TestRobustness:
    """Edge cases and malformed input handling."""

    def test_empty_list(self):
        assert detect_tool_loop([]) is None

    def test_fewer_than_six_messages(self):
        msgs = [Message(role=USER, content="hi"), Message(role=ASSISTANT, content="hello")]
        assert detect_tool_loop(msgs) is None

    def test_malformed_json_args(self):
        """FC arguments that are not valid JSON must not crash the detector."""
        msgs = [Message(role=USER, content="run")]
        for i in range(8):
            fc = FunctionCall(name="shell_cmd", arguments="{not valid json")
            msgs.append(Message(role=ASSISTANT, content="", function_call=fc))
            msgs.append(_fn_msg("shell_cmd", f"output {i}"))
        assert detect_tool_loop(msgs) is None

    def test_missing_outputs(self):
        """FC messages with no following FUNCTION output are dropped, not crashed on."""
        msgs = [Message(role=USER, content="run")]
        for i in range(6):
            msgs.append(_fc_msg("shell_cmd", {"command": "__status", "tool_id": "1"}))
            if i % 2 == 0:  # every other FC has no output
                msgs.append(_prose(f"thinking {i}"))
        assert detect_tool_loop(msgs) is None

    def test_dict_messages_vs_message_objects(self):
        """Same conversation as dicts and as Message objects → same result."""
        def build(as_dicts: bool):
            msgs = failing_poll_pairs(6)
            if not as_dicts:
                return msgs
            out = []
            for m in msgs:
                d = m.model_dump()
                if d.get("function_call") is not None:
                    d["function_call"] = {"name": d["function_call"]["name"],
                                          "arguments": d["function_call"]["arguments"]}
                out.append(d)
            return out

        res_objs = detect_tool_loop(build(False))
        res_dicts = detect_tool_loop(build(True))
        assert res_objs is not None
        assert res_dicts is not None
        assert res_objs[0] == res_dicts[0]
        assert res_objs[1] == res_dicts[1]

    def test_multimodal_content_list(self):
        """FUNCTION content as a multimodal list must be handled without crashing."""
        msgs = failing_poll_pairs(6)
        # Replace last output with a multimodal list carrying the same terminal text
        from agent_cascade.llm.schema import ContentItem
        msgs[-1] = Message(role=FUNCTION, name="shell_cmd",
                           content=[ContentItem(text=POLL_OUTPUT)])
        assert detect_tool_loop(msgs) is not None

    def test_system_messages_ignored(self):
        """System messages interleaved between poll pairs must not break detection."""
        msgs = [Message(role=USER, content="poll")]
        for i in range(6):
            msgs.append(Message(role=SYSTEM, content=f"system note {i}"))
            msgs += _poll_pair(justification=f"Check {i}")
        assert detect_tool_loop(msgs) is not None


# ══════════════════════════════════════════════
# PART 4b — FUNCTION output normalization
# ══════════════════════════════════════════════

class TestOutputNormalization:
    """Known wrapper formats are stripped; genuine output differences survive."""

    def test_polling_loop_with_wrapper_noise_fires_layer1(self):
        """REGRESSION: a polling loop whose error replies embed varying timestamps,
        elapsed markers and justification prose must still fire Layer 1."""
        base = "Command exited with return code 1.\nConnection refused (host api.internal:8080)"
        msgs = [Message(role=USER, content="poll")]
        for i in range(6):
            out = (
                f"APPROVED: {base} (elapsed {10 + i * 2}.{i}s)\n"
                f"Security Justification: Auto-generated prose variant {i} — "
                f"different wording every call, mentions step {i * 7}.\n"
                f"2026-08-25T13:{i:02d}:44.123456"
            )
            msgs.append(_fc_msg("shell_cmd", {"command": "__status", "tool_id": "9"}))
            msgs.append(_fn_msg("shell_cmd", out))
        result = detect_tool_loop(msgs)
        assert result is not None, "wrapper-noise-varying polling loop must fire Layer 1"
        reason, pop_count = result
        assert "stable/terminal output" in reason
        assert "__status" in reason
        assert pop_count > 0

    def test_genuinely_different_substantive_output_breaks_run(self):
        """No over-normalization: 6 identical failing polls + 1 poll with a
        genuinely different message AND exit code → both layers' chaining broken."""
        cmd = PYTEST_CMD
        msgs = [Message(role=USER, content="run")]
        # 6 identical failing polls ... (clean, non-noisy substantive error text)
        for i in range(6):
            out = (f"APPROVED: Command exited with return code 1. (elapsed {i + 10}s)\n"
                   f"Security Justification: prose variant {i}\n\n"
                   "42: probe counter mismatch — expected 1, got 4")
            msgs += _pytest_pair(cmd, out)
        # ... then one poll with a GENUINELY different substantive message AND
        # a different exit code (breaks both layers' chaining).
        out_diff = (f"APPROVED: Command exited with return code 3. (elapsed 99s)\n"
                    f"Security Justification: prose variant final\n\n"
                    "77: mock server returned an unexpected 503 for the healthy endpoint")
        msgs += _pytest_pair(cmd, out_diff)
        assert detect_tool_loop(msgs) is None, \
            "genuine output difference must break the run (no over-normalization)"

    def test_fail_class_survives_normalization(self):
        """Failure-class extraction on wrapped outputs: exit code, no-output and
        FAILED banner each survive normalization."""
        from agent_cascade.tool_loop_detect import _normalize_output, _fail_class

        # EXIT:1 — the verdict prose is stripped but the exit-code sentence survives.
        out_exit = ("APPROVED: Command exited with return code 1. (elapsed 11.3s)\n"
                    "Security Justification: auto prose that varies per call\n\n"
                    "No output produced.")
        assert _fail_class(_normalize_output(out_exit)) == "EXIT:1"

        # NOOUT — a failing run whose reply is ONLY wrapper noise (banner +
        # justification, nothing else) must still classify as no-output.
        out_noout = ("APPROVED: Command exited with return code 2. (elapsed 3s)\n"
                     "Security Justification: only prose, no substantive output at all")
        assert _fail_class(_normalize_output(out_noout)) == "EXIT:2"

        # TESTFAIL — a FAILED banner right after the justification (no blank
        # line separator) must NOT be swallowed by the justification stripping.
        # No exit-code line: the banner is the only failure indicator, so the
        # class must be TESTFAIL (proves the banner text survived intact).
        out_fail = ("APPROVED: Command completed successfully.\n"
                    "Security Justification: auto prose\n"
                    "FAILED tests/test_x.py::test_y")
        assert _fail_class(_normalize_output(out_fail)) == "TESTFAIL"

        # And with an exit-code line present, the banner still survives
        # normalization (EXIT:n ranks above TESTFAIL in _fail_class — that is
        # pre-existing semantics, not something normalization changes).
        out_fail2 = ("APPROVED: Command exited with return code 1. (elapsed 2s)\n"
                     "Security Justification: auto prose\n"
                     "FAILED tests/test_x.py::test_y")
        norm2 = _normalize_output(out_fail2)
        assert "FAILED tests/test_x.py::test_y" in norm2, \
            "FAILED banner must survive normalization alongside the exit-code line"

    def test_spillover_lines_normalize_identical(self):
        """Spillover/truncation lines with different paths/char counts must
        normalize to identical text (the path varies per run)."""
        from agent_cascade.tool_loop_detect import _normalize_output

        body = "AssertionError: expected 4, got 1"
        t1 = (body + "\n\n[TRUNCATED — Character limit exceeded. Full output (4996 chars) "
               "saved to: logs/spillover/impl_phase1_D_fixup_shell_20260824_125626_155333.txt]")
        t2 = (body + "\n\n[TRUNCATED — Character limit exceeded. Full output (9876 chars) "
               "saved to: logs/spillover/other_agent_tool_cmd_20260825_010101_999999.txt]")
        assert _normalize_output(t1) == _normalize_output(t2)

    def test_elapsed_and_timestamp_stripped_everywhere(self):
        """Elapsed markers and ISO timestamps are stripped from anywhere in the
        output, while surrounding genuine text survives."""
        from agent_cascade.tool_loop_detect import _normalize_output

        a = "build took (elapsed 5s) and failed at 2026-08-24T13:00:48.976877"
        b = "build took (elapsed 42s) and failed at 2026-08-24T13:05:01.000001"
        assert _normalize_output(a) == _normalize_output(b)
        assert "build took" in _normalize_output(a)
        assert "and failed at" in _normalize_output(a)

    def test_genuine_content_differences_survive(self):
        """Conservatism: only KNOWN wrapper formats are removed. Different error
        text, different stdout, different exit codes all survive normalization."""
        from agent_cascade.tool_loop_detect import _normalize_output

        d1 = _normalize_output("APPROVED: Command exited with return code 1. (elapsed 1s)\nAssertionError: got 4")
        d2 = _normalize_output("APPROVED: Command exited with return code 1. (elapsed 9s)\nTypeError: bad operand")
        assert d1 != d2

        e1 = _normalize_output("APPROVED: Command exited with return code 1. (elapsed 1s)")
        e2 = _normalize_output("APPROVED: Command exited with return code 3. (elapsed 9s)")
        assert e1 != e2


# ══════════════════════════════════════════════
# PART 5 — Integration through _pre_llm_checks
# ══════════════════════════════════════════════

class TestPreLlmChecksIntegration:
    """_pre_llm_checks wiring for the tool-loop detector (fake-engine pattern)."""

    def _make_fake_instance(self, name="test_agent"):
        class FakeInstance:
            def __init__(self, instance_name):
                self.instance_name = instance_name
        return FakeInstance(name)

    def _make_fake_pool(self, max_auto_rollbacks=3, auto_rollback_on_loop=True,
                        tool_loop_detection_enabled=True, tool_loop_rollback_enabled=False):
        pool = MagicMock()
        pool.settings = MagicMock()
        pool.settings.max_auto_rollbacks = max_auto_rollbacks
        pool.settings.auto_rollback_on_loop = auto_rollback_on_loop
        pool.settings.tool_loop_detection_enabled = tool_loop_detection_enabled
        pool.settings.tool_loop_rollback_enabled = tool_loop_rollback_enabled
        return pool

    def _make_engine(self, pool):
        from agent_cascade.execution_engine import ExecutionEngine

        engine = MagicMock(spec=ExecutionEngine)
        engine.pool = pool
        engine.compression_handler = MagicMock()
        engine.compression_handler.handle_rollback_command.return_value = False
        engine.compression_handler.handle_compress_command.return_value = False

        # Bind the REAL _pre_llm_checks so we exercise actual wiring.
        engine._pre_llm_checks = ExecutionEngine._pre_llm_checks.__get__(engine, ExecutionEngine)
        engine._check_stop_conditions = MagicMock(return_value=False)
        engine._inject_async_messages = MagicMock(return_value=False)
        engine._check_and_trigger_compression = MagicMock(return_value=False)
        tel = MagicMock()
        engine._telemetry = MagicMock(return_value=tel)
        engine._append_and_log = MagicMock()
        engine._inline_rollback_and_hint = MagicMock()
        return engine

    def _tool_loop_msgs(self):
        """Synthetic sample-1-style conversation (7 terminal-error poll pairs)."""
        return failing_poll_pairs(7, user="launch the run")

    def test_rollback_when_tool_rollback_enabled(self):
        """tool_loop_rollback_enabled=True → rollback path with correct pop_count."""
        pool = self._make_fake_pool(tool_loop_rollback_enabled=True)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = self._tool_loop_msgs()

        expected_pop = detect_tool_loop(msgs)[1]
        turns = [50]
        result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is True, "rollback cycle should continue the loop"
        engine._inline_rollback_and_hint.assert_called_once()
        args = engine._inline_rollback_and_hint.call_args.args
        assert args[2] == expected_pop, "pop_count passed to rollback must match detector"
        assert inst._loop_rollback_count == 1
        pool.terminate_instance.assert_not_called()
        assert turns[0] == 49, "turn consumed on loop rollback"

    def test_log_only_when_rollback_disabled(self):
        """tool_loop_rollback_enabled=False (default) → no rollback, continues to LLM."""
        pool = self._make_fake_pool(tool_loop_rollback_enabled=False)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = self._tool_loop_msgs()

        turns = [50]
        result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is False, "log-only mode must proceed to the LLM call"
        engine._inline_rollback_and_hint.assert_not_called()
        pool.terminate_instance.assert_not_called()
        assert turns[0] == 50, "no turn consumed in log-only mode"
        # Telemetry recorded with auto_rolled_back=False and loop_type="tool"
        tel = engine._telemetry.return_value
        tel.record_loop_detected.assert_called_once()
        kwargs = tel.record_loop_detected.call_args.kwargs
        assert kwargs["auto_rolled_back"] is False
        assert kwargs["loop_type"] == "tool"

    def test_flag_off_no_op(self):
        """tool_loop_detection_enabled=False → detector never runs, no telemetry."""
        pool = self._make_fake_pool(tool_loop_detection_enabled=False)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = self._tool_loop_msgs()

        turns = [50]
        result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is False
        engine._inline_rollback_and_hint.assert_not_called()
        tel = engine._telemetry.return_value
        tel.record_loop_detected.assert_not_called()

    def test_telemetry_loop_type_tool_on_rollback(self):
        """Rollback path records loop_type='tool' (not 'outer') for tool-loop hits."""
        pool = self._make_fake_pool(tool_loop_rollback_enabled=True)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = self._tool_loop_msgs()

        engine._pre_llm_checks(inst, msgs, [], [], [50])

        tel = engine._telemetry.return_value
        tel.record_loop_detected.assert_called_once()
        kwargs = tel.record_loop_detected.call_args.kwargs
        assert kwargs["loop_type"] == "tool"
        assert kwargs["auto_rolled_back"] is True

    def test_outer_detector_takes_priority(self):
        """When the legacy detector fires, loop_type stays 'outer' and tool detector is skipped."""
        pool = self._make_fake_pool(tool_loop_rollback_enabled=True)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = [
            Message(role=USER, content="q"), Message(role=ASSISTANT, content="a"),
            Message(role=USER, content="q"), Message(role=ASSISTANT, content="a"),
            Message(role=USER, content="q"), Message(role=ASSISTANT, content="a"),
        ]

        with patch("agent_cascade.engine.llm_call._canonical_detect_loop", return_value=("repeat", 2)), \
             patch("agent_cascade.engine.llm_call._detect_tool_loop") as mock_tool:
            engine._pre_llm_checks(inst, msgs, [], [], [50])

        mock_tool.assert_not_called()
        tel = engine._telemetry.return_value
        kwargs = tel.record_loop_detected.call_args.kwargs
        assert kwargs["loop_type"] == "outer"

    def test_max_auto_rollbacks_enforced_for_tool_loops(self):
        """max_auto_rollbacks=0: first tool-loop detection → rollback then terminate."""
        pool = self._make_fake_pool(max_auto_rollbacks=0, tool_loop_rollback_enabled=True)
        engine = self._make_engine(pool)
        inst = self._make_fake_instance()
        msgs = self._tool_loop_msgs()

        result = engine._pre_llm_checks(inst, msgs, [], [], [50])

        assert result is True
        assert inst._loop_rollback_count == 1
        engine._inline_rollback_and_hint.assert_called_once()
        pool.terminate_instance.assert_called_once()
        assert pool.terminate_instance.call_args.kwargs.get("set_global_stopped") is False

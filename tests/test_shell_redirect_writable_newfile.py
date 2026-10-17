#!/usr/bin/env python3
"""Tests for the shell_cmd new-file-in-writable-root auto-approval.

Spec: plans/shell_redirect_writable_newfile_PLAN.md §8.2 (cases 1-22).

Covers:
- ALLOW-side: a read-only command whose only side effect is creating a NEW file
  inside a writable root is auto-approved (evaluate_shell_command_safety → True).
  These cases FAIL on the pre-change validator (revert-proof, §10 step 7).
- PROMPT-side: overwrite / outside-root / unresolvable / restricted still decline.
- Ownership: after a successful auto-approved redirect the file is owned by the
  caller (normcase discipline); a command that never created it gains no key.
- Static-classifier unit cases (no filesystem): _classify / strict-bool locks.

Hermetic: a fresh tempfile.mkdtemp() is the writable base (base_dir is always RW,
path_security.py:261). resolve_tool_path delegates to agent_pool.operation_manager._resolve_path
(tool_path_resolver.py:45-48), so a one-attribute fake pool is enough. No real process
runs (subprocess.Popen is patched). No LLM/network.
"""

import os
import tempfile
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.shell import ShellMixin
from agent_cascade.utils.tool_path_resolver import resolve_tool_path

REJECTED_MSG = ('REJECTED: Command not allowed for system agents. '
                'Only read-only filesystem/git commands are permitted. '
                'Use __help to see allowed commands.')


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

class _FakePool:
    """Minimal pool exposing only what the gates read: operation_manager + instances."""

    def __init__(self, operation_manager, instances=None):
        self.operation_manager = operation_manager
        self.instances = dict(instances or {})


def _make_instance(restricted):
    inst = MagicMock()
    inst.restricted_shell = restricted
    return inst


def _make_om(base, instances=None):
    """A real OperationManager rooted at a fresh temp dir, wired to a fake pool.

    The pool's operation_manager is om itself, so resolve_tool_path delegates to
    om._resolve_path (base_dir always RW). instances defaults empty → the test
    agent is NOT restricted (so the auto-approve path is exercised).
    """
    om = OperationManager(base_dir=base, agent_pool=None)
    pool = _FakePool(om, instances)
    om.agent_pool = pool
    return om, pool


def _norm(p):
    return os.path.normcase(str(p))


@contextmanager
def _patch_popen(create_path=None):
    """Patch subprocess.Popen with a no-op fake. If create_path is given, the fake
    'process' creates that file (simulating a redirect that writes a new file).

    The fake's stdout/stderr expose ``read(n) -> ''`` so ``drain_pipe_chunks``
    (shell_utils.py:56) sees a clean EOF and ``drain_errors`` stays empty —
    otherwise the executor's PID-logging branch fires and the
    ownership block is never reached.
    """
    class _FakeProc:
        returncode = 0
        pid = 0

        def wait(self, timeout=None):
            pass

    class _FakePipe:
        def read(self, n=-1):
            return ''

    def _fake_popen(*a, **k):
        p = _FakeProc()
        p.stdout = _FakePipe()
        p.stderr = _FakePipe()
        if create_path is not None:
            try:
                Path(create_path).touch()
            except OSError:
                pass
        return p

    with patch('subprocess.Popen', side_effect=_fake_popen):
        yield


def _approve(*a, **k):
    return (True, 'ok')


# ────────────────────────────────────────────────────────────────────────
# ALLOW-side (these FAIL on the pre-change validator — real coverage)
# ────────────────────────────────────────────────────────────────────────


def test_allow_new_relative_file():
    """§8.2 case 1: git diff > pr_full.diff (relative, cwd inside writable root) → True."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, targets = om.evaluate_shell_command_safety(
        'git diff > pr_full.diff', cwd=base, agent_name='test_agent')
    assert is_safe is True
    expected = _norm(Path(base) / 'pr_full.diff')
    assert len(targets) == 1 and _norm(targets[0]) == expected


def test_allow_motivating_command():
    """§8.2 case 2: the exact motivating cd && git diff > abs && wc chain → True.

    evaluate_shell_command_safety never runs git, so no skip is needed; the static
    + filesystem half is the whole decision. The repo is the writable root itself
    (a separate tempdir would be outside base and fail cwd resolution).
    """
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    target = Path(base) / 'pr_full.diff'
    cmd = f'cd {base} && git diff 296b59d2..HEAD > {target} && wc -l {target}'
    is_safe, targets = om.evaluate_shell_command_safety(cmd, cwd=base, agent_name='test_agent')
    assert is_safe is True
    assert len(targets) == 1 and _norm(targets[0]) == _norm(target)


def test_allow_quoted_target_with_spaces():
    """§8.2 case 3: ls > "my file.txt" → True (old code truncated the token to '"my')."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, targets = om.evaluate_shell_command_safety(
        'ls > "my file.txt"', cwd=base, agent_name='test_agent')
    assert is_safe is True
    expected = _norm(Path(base) / 'my file.txt')
    assert len(targets) == 1 and _norm(targets[0]) == expected


def test_allow_trailing_semicolon_trimmed():
    """§8.2 case 4: ls > out.txt; wc -l out.txt → True, key is out.txt not 'out.txt;'."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, targets = om.evaluate_shell_command_safety(
        'ls > out.txt; wc -l out.txt', cwd=base, agent_name='test_agent')
    assert is_safe is True
    expected = _norm(Path(base) / 'out.txt')
    assert len(targets) == 1 and _norm(targets[0]) == expected


def test_allow_append_new_file():
    """§8.2 case 5: dir >> new.log (absent) → True (>> ≡ >, existence decides)."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, targets = om.evaluate_shell_command_safety(
        'dir >> new.log', cwd=base, agent_name='test_agent')
    assert is_safe is True
    expected = _norm(Path(base) / 'new.log')
    assert len(targets) == 1 and _norm(targets[0]) == expected


def test_background_redirect_still_prompts():
    """§8.2 case 6 / plan row 17 — DEVIATION.

    The plan claims `git status &> status.txt` auto-approves. But the background
    guard regex (?<![>&])&(?!&) is explicitly NOT touched (coupled
    to the redirect guard per .agent_lessons/shell-redirect-guard-fd-dup-and-background-coupling.md),
    and it matches the `&` in `&>` (preceded by a space, followed by `>`), so the
    command is structurally rejected and still PROMPTS. We lock the ACTUAL behaviour.
    """
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, _targets = om.evaluate_shell_command_safety(
        'git status &> status.txt', cwd=base, agent_name='test_agent')
    assert is_safe is False


def test_allow_null_sink_order_independent():
    """§8.2 case 7: ls > out.txt 2>/dev/null and ls 2>/dev/null > out.txt → True."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    for cmd in ('ls > out.txt 2>/dev/null', 'ls 2>/dev/null > out.txt'):
        is_safe, targets = om.evaluate_shell_command_safety(
            cmd, cwd=base, agent_name='test_agent')
        assert is_safe is True
        expected = _norm(Path(base) / 'out.txt')
        assert len(targets) == 1 and _norm(targets[0]) == expected


def test_allow_dedup_normcase_one_entry():
    """§8.2 case 8: same target written with different case → one entry (normcase dedup)."""
    base = tempfile.mkdtemp()
    ok, targets = ShellMixin._extract_shell_redirect_targets('ls > out.txt > OUT.TXT')
    assert ok is True
    assert len(targets) == 1  # deduped by os.path.normcase


# ────────────────────────────────────────────────────────────────────────
# PROMPT-side (must stay False through the instance path)
# ────────────────────────────────────────────────────────────────────────


def test_prompt_existing_target():
    """§8.2 case 9: target exists before the run → False (the real overwrite guard)."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    existing = Path(base) / 'output.txt'
    existing.touch()  # create it first
    is_safe, targets = om.evaluate_shell_command_safety(
        'ls > output.txt', cwd=base, agent_name='test_agent')
    assert is_safe is False
    assert targets == []


def test_prompt_target_outside_roots():
    """§8.2 case 10: target outside every allowed root → False."""
    base = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()  # a real dir, but NOT base and not an extra root
    om, _pool = _make_om(base)
    target = Path(outside) / 'scratch.txt'
    is_safe, _targets = om.evaluate_shell_command_safety(
        f'ls > {target}', cwd=base, agent_name='test_agent')
    assert is_safe is False


def test_prompt_unresolvable_and_locks():
    """§8.2 case 12: unresolvable targets + §6 regression locks → all False."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    locks = [
        'ls > $OUT', 'ls > *.txt', 'ls > ~/x', 'ls > >(wc -l)',
        'ls >', 'echo "a>b"', "git log --format='%h>%s'",
        'sort -k>1', "awk 'a>b'", 'ls 2>nul & echo x',
    ]
    for cmd in locks:
        is_safe, _targets = om.evaluate_shell_command_safety(
            cmd, cwd=base, agent_name='test_agent')
        assert is_safe is False


def test_prompt_mixed_new_and_existing():
    """§8.2 case 13: one new + one existing target → False (all-or-nothing, D4)."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    (Path(base) / 'existing.txt').touch()
    is_safe, _targets = om.evaluate_shell_command_safety(
        'ls > new.txt && ls > existing.txt', cwd=base, agent_name='test_agent')
    assert is_safe is False
    # block_reason must name the FIRST OFFENDING target (existing.txt), not targets[0]
    # (new.txt) — this is the only case that distinguishes "first offending" from "targets[0]".
    _ok, _paths, reason = om._resolve_shell_redirect_targets(['new.txt', 'existing.txt'], base)
    assert reason is not None and 'existing.txt' in reason and 'new.txt' not in reason


def test_prompt_relative_target_cwd_outside_roots():
    """§8.2 case 14: relative target whose cwd is outside the allowed roots → False."""
    base = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    is_safe, _targets = om.evaluate_shell_command_safety(
        'ls > out.txt', cwd=outside, agent_name='test_agent')
    assert is_safe is False


def test_restricted_new_file_still_rejected():
    """§8.2 case 15: restricted/system agent + new-file redirect → REJECTED (locks D5).

    This is the silent-violation guard for the two-variable split in §4.1: if the
    widened value were used at the restricted gate, this would auto-approve.
    """
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base, instances={'sec': _make_instance(True)})
    with _patch_popen():
        result = om.execute_shell_command(
            command='ls > new.txt', justification='x', agent_name='sec', cwd=base)
    assert result == REJECTED_MSG


# ────────────────────────────────────────────────────────────────────────
# Ownership (integration; Popen patched, file created by the fake)
# ────────────────────────────────────────────────────────────────────────


def test_ownership_recorded_after_new_file_redirect():
    """§8.2 case 16: after a successful auto-approved redirect the file is owned."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    target = Path(base) / 'pr_full.diff'
    with _patch_popen(create_path=target):
        result = om.execute_shell_command(
            command='git diff > pr_full.diff', justification='x',
            agent_name='test_agent', cwd=base)
    assert not result.startswith('REJECTED')
    assert om._get_owner(target) == 'test_agent'
    # normcase discipline: a differently-cased lookup still matches.
    assert om._get_owner(str(Path(base) / 'PR_FULL.DIFF')) == 'test_agent'


def test_no_ownership_when_file_never_created():
    """§8.2 case 17: command that never created the target → no stale key."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    target = Path(base) / 'pr_full.diff'
    with _patch_popen(create_path=None):  # fake runs but writes nothing
        result = om.execute_shell_command(
            command='git diff > pr_full.diff', justification='x',
            agent_name='test_agent', cwd=base)
    assert not result.startswith('REJECTED')
    assert om._get_owner(target) is None


def test_existing_file_through_approval_gains_no_ownership():
    """§8.2 case 18: an existing-file redirect that went through approval → no key."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    target = Path(base) / 'output.txt'
    target.touch()  # exists → prompts
    om.request_user_approval = _approve  # user approves
    with _patch_popen(create_path=target):
        result = om.execute_shell_command(
            command='ls > output.txt', justification='x',
            agent_name='test_agent', cwd=base)
    assert not result.startswith('REJECTED')
    assert om._get_owner(target) is None


def test_ownership_key_is_normcase_of_resolved():
    """§8.2 case 19: file_ownership key equals os.path.normcase(str(resolved))."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    target = Path(base) / 'pr_full.diff'
    with _patch_popen(create_path=target):
        om.execute_shell_command(
            command='git diff > pr_full.diff', justification='x',
            agent_name='test_agent', cwd=base)
    assert len(om.file_ownership) == 1
    key = next(iter(om.file_ownership))
    assert key == _norm(target)


# ────────────────────────────────────────────────────────────────────────
# Static-classifier unit cases (no filesystem)
# ────────────────────────────────────────────────────────────────────────


def test_classify_real_file_target_and_strict_bool():
    """§8.2 case 20: classifier reports the target; strict bool stays False (D1 lock)."""
    assert ShellMixin._classify_shell_command('ls > out.txt') == (True, ['out.txt'])
    assert ShellMixin._is_safe_readonly_shell_command('ls > out.txt') is False


def test_classify_fd_and_null_no_targets():
    """§8.2 case 21: fd-dup + null sinks → (True, [])."""
    assert ShellMixin._classify_shell_command('git log 2>&1 > /dev/null') == (True, [])


def test_classify_malformed_rejects():
    """§8.2 case 22: glued operator / quoted operator / bare > → structural False."""
    assert ShellMixin._classify_shell_command('sort -k>1')[0] is False
    assert ShellMixin._classify_shell_command('echo "a>b"')[0] is False
    assert ShellMixin._classify_shell_command('ls >')[0] is False


# ────────────────────────────────────────────────────────────────────────
# async gate must use the widened decision, not the strict bool
# ────────────────────────────────────────────────────────────────────────


def test_async_new_file_redirect_auto_approves_no_ownership():
    """The async gate must use the widened decision, not the strict bool — proven with a real pool.

    A real OperationManager (NOT a MagicMock) makes evaluate_shell_command_safety return a
    real (bool, list) tuple, so the `isinstance(tuple)` fallback is NOT taken — the widened
    path executes. The strict bool would reject `git diff > pr_full.diff`, so AUTO-APPROVED
    here proves the widened decision was used. Async records NO ownership at launch (Phase 2).
    """
    base = tempfile.mkdtemp()
    om, pool = _make_om(base)
    pool._async_shell_tracker = MagicMock()
    # completed_early=True, rc=0 → returns the AUTO-APPROVED completion string, no real process.
    pool._async_shell_tracker.launch.return_value = (1, 4242, None, True, 0)
    pool.llm_cfg = {}

    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    tool = ShellCmd()
    tool.agent_pool = pool
    tool.agent_name = 'test_agent'

    result = tool._launch_async(
        agent_name='test_agent',
        command='git diff > pr_full.diff',
        justification='write a fresh diff',
        cwd=base,
        timeout=None,
        heartbeat_interval=-1,
    )
    assert 'AUTO-APPROVED' in result, f"expected AUTO-APPROVED, got {result!r}"
    assert 'REJECTED' not in result and 'SECURITY WARNING' not in result
    # Phase 2: async must NOT optimistically own the file at launch.
    assert om.file_ownership == {}, f"async must not own the file, got {om.file_ownership!r}"


# ────────────────────────────────────────────────────────────────────────
# the prompt reason must name the SPECIFIC blocking condition (overwrite), not a vague claim
# ────────────────────────────────────────────────────────────────────────


def test_prompt_reason_names_the_blocking_condition():
    """An overwrite prompt must say 'already exists' + the offending target, not a generic claim."""
    base = tempfile.mkdtemp()
    om, _pool = _make_om(base)
    existing = Path(base) / 'out.txt'
    existing.touch()  # pre-existing → the redirect would overwrite it

    captured = {}

    def _capture(**k):
        captured.update(k)
        return (True, 'ok')

    om.request_user_approval = _capture
    om.execute_shell_command(
        command='ls > out.txt', justification='x', agent_name='test_agent', cwd=base)
    d = captured.get('description', '')
    assert 'already exists' in d and 'out.txt' in d, f"imprecise reason: {d!r}"
    assert 'outside your writable folders' not in d, 'must not claim the wrong condition'

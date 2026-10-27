"""Shell execution — safety check and command execution mixin."""

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Project imports (used by ShellMixin.execute_shell_command)
from agent_cascade.log import logger
from agent_cascade.shell_utils import (DRAIN_THREAD_JOIN_TIMEOUT, build_shell_env, configure_windows_utf8,
                                       drain_pipe_chunks, git_autogc_hint_for_output)
from agent_cascade.tool_utils import truncate_with_spillover

# ─── Named constants (avoid magic numbers) ──────────────────────────────
TASKKILL_RETRY_DELAY = 0.5  # Seconds between taskkill passes on timeout
MAX_PROCESS_TREE_DEPTH = 10  # Max recursion depth when killing process descendants
DEFAULT_SHELL_TIMEOUT = 30  # Default seconds before shell command times out
MAX_SHELL_TIMEOUT = 3600  # Maximum allowed shell command timeout (1 hour)

# Platform flag (evaluated once at import time)
ON_WINDOWS = os.name == 'nt'

# Matches a Python launcher (python / python3 / py) followed by a -c or -e flag.
# Used by ShellMixin._detect_multiline_python (BUG_0012); the argument itself is
# scanned separately with quote-state tracking so inner quotes don't hide newlines.
# The token boundary excludes .exe-suffixed / versioned forms (e.g. python.exe,
# python3.11) — same accepted "path-qualified invocation" limitation class as
# ShellCmd._split_command_stages (a bare `py` after a path separator still matches).
_PYTHON_C_FLAG_RE = re.compile(r'\b(?:python[23]?|py)(?:\s+-\w+)*\s+-[ce]\s+', re.IGNORECASE)

# ─── Module-level helper (used inside ShellMixin) ──────────────────────


def _run_taskkill(pid: int, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run taskkill to forcibly terminate a Windows process by PID.

    Args:
        pid: Process ID to kill.
        timeout: Seconds before the taskkill call itself times out.

    Returns:
        subprocess.CompletedProcess on success.

    Raises:
        Exception if taskkill fails or times out.
    """
    return subprocess.run(
        ['taskkill', '/F', '/PID', str(pid)],
        capture_output=True,
        timeout=timeout,
        encoding='utf-8',
        errors='replace',
    )


# ─── Mixin: Shell methods for OperationManager ─────────────────────────


class ShellMixin:
    """Shell execution methods. Expects self to have __init__-set attributes."""

    # ------------------------------------------------------------------
    # Safe git sub-commands (all are read-only by nature)
    _SAFE_GIT_SUBCOMMANDS: set = {
        'diff',
        'status',
        'log',
        'show',
        'branch',
        'tag',
        'remote',
        'rev-parse',
        'config',
        'merge-base',
        'describe',
        'ls-files',
        'ls-tree',
        'stash',
        'shortlog',
        'blame',
        'name-rev',
        'hash-object',
        'cat-file',
        'for-each-ref',
        'var',
        'symbolic-ref',
        'version',
        'rev-list',
        'reflog',
        'worktree',
        'count-objects',
        'interpret-trailers',
        'notes',
        'pack-refs',
        'prune',
        'replace',
        'rerere',
        'verify-commit',
        'verify-tag',
        'verify-pack',
        'show-ref',
    }

    # Git flags to skip before detecting the subcommand
    # (some consume the next token: -c, -C, --git-dir, --work-tree)
    _GIT_FLAGS: set = {'--no-pager', '-c', '-p', '--paginate', '-C', '--git-dir', '--work-tree'}

    # Async shell control commands (used for validation in both shell.py and shell_cmd.py)
    _CONTROL_COMMANDS: tuple = ('__status', '__kill', '__ctrl_c', '__wait')
    _CONTROL_HEARTBEAT_PREFIX: str = '__heartbeat='

    # Dangerous arguments per git subcommand
    _DANGEROUS_GIT_ARGS: dict[str, set[str]] = {
        'stash': {'drop', 'pop', 'apply', 'clear'},
        'branch': {'-d', '-D', '-m', '-M', '-r', '--delete', '--move', '--set-upstream-to'},
        'tag': {'-d', '-D', '--delete', '-f', '-F', '-a', '-s'},
        'remote': {'set-url', 'add', 'rm', 'rename', 'set-branches', 'set-head'},
        'config': {'--set', '--add', '--unset', '--unset-all', '-e', '--list-all'},
        'worktree': {'add', 'remove', 'checkout', 'prune'},
    }

    # Safe pipe/filter commands for pipelines (e.g. git diff | grep 'changed')
    _SAFE_PIPE_COMMANDS: set = {
        'grep',
        'egrep',
        'fgrep',
        'head',
        'tail',
        'sort',
        'uniq',
        'wc',
        'cat',
        'more',
        'less',
        'cut',
        'tr',
        'comm',
        'diff',
        'nl',
        'rev',
        'fold',
        'findstr',
    }

    # Safe primary commands (directory listing / file inspection)
    _SAFE_PRIMARY_COMMANDS: set = {
        'find',
        'dir',
        'ls',
        'tree',
        'directory',
        'vfd',
        'where',
        'whereis',
        'locate',
        'which',
        'type',
        'pwd',
        'stat',
        'file',
        'du',
        'df',
        # Read-only text utilities (BUG_0057) — no side effects, no filesystem writes.
        # `cat` caveat: reads arbitrary paths the agent could already read_file; truncation/
        # spillover covers size. `clip` is deliberately EXCLUDED (mutates user clipboard state).
        'echo',
        'cat',
        'wc',
        'sort',
        'uniq',
        'head',
        'tail',
        'diff',
        'basename',
        'dirname',
        'realpath',
        'date',
        'env',
        'printenv',
        'hostname',
        'whoami',
        'id',
        'uname',
        'test',
        'true',
        'false',
        # Read-only search utilities (writes only to stdout; no -exec-style escape).
        'findstr',
        'grep',
        'egrep',
        'fgrep',
    }

    # Policy: any command safe as a primary is also safe as a pipe stage.
    # Effective set used by _validate_pipeline_stages at BOTH call sites
    # (regular pipelines and git pipelines). Derived dynamically from the two
    # allow-lists so the policy can never drift if either set changes. Must be
    # defined AFTER both base sets above. Keeps _SAFE_PIPE_COMMANDS itself
    # untouched — tests and __help text reference it directly.
    _PIPE_STAGE_COMMANDS: set = _SAFE_PIPE_COMMANDS | _SAFE_PRIMARY_COMMANDS

    # Redirect operator: optional fd prefix (2>, 1>), the bash &> "both" form,
    # then a single or double >. Does NOT match && or || (no > present).
    _REDIRECT_RE: 're.Pattern[str]' = re.compile(r'(?:\d*|&)>>?')

    @staticmethod
    def _extract_shell_redirect_targets(cmd: str) -> tuple[bool, list[str]]:
        """Scan the FULL command for redirect operators; classify their targets.

        Returns ``(well_formed, real_file_targets)``:
          - ``well_formed=False`` → the command contains a malformed or ambiguous
            redirect (bare ``>``, an unbalanced quote such as ``echo "a>b"`` /
            ``awk 'a>b'`` / ``git log --format='%h>%s'``, a glued operator like
            ``sort -k>1``, or an unresolvable target such as ``$VAR``/globs/``~``/
            process substitution). Such commands must be rejected.
          - ``real_file_targets`` are the quote-stripped, metacharacter-trimmed
            tokens that are genuine file sinks. Null-device (``/dev/null``, ``NUL``)
            and fd-duplication (``2>&1``, ``>&2``) sinks are skipped. Order is
            preserved; duplicates are deduplicated by ``os.path.normcase``.

        This is a *pure* classification: it never consults the filesystem. Whether a
        non-empty target list is safe is decided at runtime by
        :meth:`evaluate_shell_command_safety` (new file inside a writable root).
        """
        targets: list[str] = []
        seen: set[str] = set()
        for m in ShellMixin._REDIRECT_RE.finditer(cmd):
            # 1. Accept a quoted token so `> "my file.txt"` is one target.
            target_match = re.match(r'\s*("[^"]*"|\'[^\']*\'|\S+)', cmd[m.end():])
            if not target_match:
                return (False, [])  # bare `>` / no target → malformed
            token = target_match.group(1)

            # 2. Quote balance: an odd number of " or ' means the `>` was inside
            #    quotes (echo "a>b", awk 'a>b', git log --format='%h>%s'). Reject.
            if token.count('"') % 2 or token.count("'") % 2:
                return (False, [])
            # Strip exactly one matched surrounding pair, if present.
            if len(token) >= 2 and token[0] in '"\'' and token[-1] == token[0]:
                token = token[1:-1]

            # 3. Trim trailing metacharacters glued to the target so
            #    `git diff > patch.diff; wc -l patch.diff` yields `patch.diff`.
            token = token.rstrip(';|&<>')

            # 4. Sink classification (unchanged semantics): fd-duplication and the
            #    exact null device are not files.
            if token.startswith('&'):
                continue
            if token.lower() in ('/dev/null', 'nul'):
                continue

            # Nothing left after trimming (e.g. `> ;`) → malformed.
            if not token:
                return (False, [])

            # 5. Ambiguity veto (fail closed): targets that cannot be resolved to a
            #    definite path at validation time. NOTE: no space here — a space can
            #    only appear inside an already-delimited quoted token (legitimate, e.g.
            #    "my file.txt"); unquoted tokens never contain spaces.
            if any(c in '()`$*?[]{}~!\n' for c in token):
                return (False, [])

            # 6. Standalone-operator rule: a real-file target's operator must be
            #    preceded by whitespace or be at position 0. A glued operator
            #    (`-k>1`, `a>b`) is ambiguous. Applied only to real-file sinks, so
            #    null/fd sinks with a glued operator (e.g. `echo hi> /dev/null`)
            #    are unaffected.
            op_start = m.start()
            if op_start != 0 and not cmd[op_start - 1].isspace():
                return (False, [])

            # 7. Append (dedup by normcase).
            key = os.path.normcase(token)
            if key not in seen:
                seen.add(key)
                targets.append(token)
        return (True, targets)

    @staticmethod
    def _is_structurally_safe_shell_command(command: str) -> bool:
        """Structural (pure) safety check — no filesystem, no instance state.

        Returns True iff the command is well-formed and every pipeline/chain
        segment uses only read-only vocabulary. A well-formed redirect to a real
        file does NOT make this False; it is reported as a target by
        :meth:`_extract_shell_redirect_targets` and decided at runtime. Malformed
        or ambiguous redirects (bare ``>``, ``echo "a>b"``, ``sort -k>1``,
        ``$VAR``/globs) DO make this False.

        Handles:
          - Basic commands: ls, find, dir, tree, pwd, stat, etc.
          - Git commands: git diff, git status, git log, etc.
          - cd && git pattern: cd <path> && git <command>
          - Pipes: git diff --stat | grep 'changed'
          - Async shell control commands: __status, __kill, __ctrl_c, __heartbeat=N
        """
        cmd = command.strip()
        if not cmd:
            return False

        # Async shell control commands are always safe
        if cmd in ShellMixin._CONTROL_COMMANDS or cmd.startswith(ShellMixin._CONTROL_HEARTBEAT_PREFIX):
            return True

        # Subshell execution: $(...) or backticks
        if '$(' in cmd or '`' in cmd:
            return False

        # Redirections: a well-formed redirect to a real file is NOT a rejection
        # here — it is collected as a target and decided at runtime. Only
        # malformed/ambiguous redirects reject.
        well_formed, _targets = ShellMixin._extract_shell_redirect_targets(cmd)
        if not well_formed:
            return False

        # Background processes: standalone & that is not part of && nor part of an
        # fd-duplication (i.e. a & directly following a >, as in 2>&1 or >&2). A genuine
        # background & is never preceded by > so it still rejects here.
        if re.search(r'(?<![>&])&(?!&)', cmd):
            return False

        # ── Strip "cd <path> &&" prefix ──
        stripped_cmd = ShellMixin._strip_cd_prefix(cmd)

        # Handle remaining chaining after stripping cd prefix: split on &&, ;, ||
        # and recursively validate each segment. Since the global checks (subshells,
        # redirects, background) already ran on the full command above, each segment
        # is independently validated here for primary + pipeline safety.
        if '&&' in stripped_cmd or ';' in stripped_cmd or '||' in stripped_cmd:
            segments = re.split(r'\s*(?:&&|;|\|\|)\s*', stripped_cmd)
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                if not ShellMixin._is_structurally_safe_shell_command(seg):
                    return False
            return True

        # Split into pipeline stages
        pipeline = stripped_cmd.split('|')

        first_cmd_part = pipeline[0].strip()
        words = first_cmd_part.split()
        if not words:
            return False

        primary_cmd = words[0].lower()

        # Check for "cmd /c" or "powershell -c" wrappers
        if primary_cmd in ('cmd', 'command.com'):
            return False
        if primary_cmd in ('powershell', 'pwsh'):
            return False

        # ── Git command handling ──
        if primary_cmd == 'git':
            return ShellMixin._check_git_command(words, pipeline)

        # ── Regular command handling ──
        if primary_cmd not in ShellMixin._SAFE_PRIMARY_COMMANDS:
            return False

        # For "find" commands, check that they don't use -exec/-execdir with dangerous actions
        if primary_cmd == 'find':
            if '-exec' in stripped_cmd.lower() or '-ok' in stripped_cmd.lower():
                return False

        # Validate pipe stages are safe (any primary-safe command is also a valid stage)
        if not ShellMixin._validate_pipeline_stages(pipeline, ShellMixin._PIPE_STAGE_COMMANDS):
            return False

        return True

    @staticmethod
    def _classify_shell_command(command: str) -> tuple[bool, list[str]]:
        """Three-way structural classification of a shell command.

        Returns ``(structural_ok, real_file_targets)``:
          - ``structural_ok=False`` → hard reject (malformed / ambiguous / unsafe
            vocabulary). ``real_file_targets`` is empty in this case.
          - ``structural_ok=True`` with an empty target list → fully read-only,
            unconditionally safe to auto-approve.
          - ``structural_ok=True`` with a NON-EMPTY target list → *conditionally*
            safe: the command is read-only except that it redirects to one or more
            real files. This is NOT an approval decision — it is approved only if
            :meth:`evaluate_shell_command_safety` proves every target is a NEW
            file inside a writable root. Do not mistake "conditional" for "approved".
        """
        ok = ShellMixin._is_structurally_safe_shell_command(command)
        if not ok:
            return (False, [])
        well_formed, targets = ShellMixin._extract_shell_redirect_targets(command)
        return (well_formed, targets if well_formed else [])

    @staticmethod
    def _is_safe_readonly_shell_command(
        command: str, *, precomputed: tuple[bool, list[str]] | None = None
    ) -> bool:
        """Strict read-only predicate — semantically UNCHANGED from before.

        Kept as a strict subset of :meth:`_classify_shell_command` so every
        existing caller and test keeps its current expectation: a real-file
        redirect still returns False here. The widened approval decision (new
        file inside a writable root) lives in
        :meth:`evaluate_shell_command_safety`; restricted/system-invoked agents
        must keep using THIS bool. ``precomputed`` optionally supplies an
        already-computed ``(structural_ok, targets)`` from :meth:`_classify_shell_command`
        so a caller that classified the command once (the sync gate) does not re-scan it;
        the default (no precomputed) path is byte-for-byte identical for all existing callers.
        """
        if precomputed is not None:
            ok, targets = precomputed
        else:
            ok, targets = ShellMixin._classify_shell_command(command)
        return ok and not targets

    @staticmethod
    def _format_help(restricted: bool = False) -> str:
        """Return a formatted list of auto-approved shell commands/patterns for __help.

        Rendered directly from the class-level allow-list sets so the output can never
        drift from what ``evaluate_shell_command_safety`` actually auto-approves — the
        widened policy (read-only commands PLUS a new-file redirect inside a writable
        root). The strict ``_is_safe_readonly_shell_command`` bool is NARROWER and is
        NOT the source of truth for the approval decision; do not cite it here.
        """
        def _join(items):
            return ', '.join(sorted(items))

        banner = ('[RESTRICTED MODE] You are a system-invoked agent. ONLY the read-only '
                  'commands below will run; everything else is REJECTED (no user approval).') \
            if restricted else \
            '[NORMAL MODE] Commands below run WITHOUT approval. Anything else prompts the user.'

        lines = [
            'shell_cmd auto-approval reference',
            '=' * 40,
            banner,
            '',
            'Primary read-only commands:',
            f"  {_join(ShellMixin._SAFE_PRIMARY_COMMANDS)}",
            '',
            "Git subcommands — run as 'git <subcommand> ...':",
            f"  {_join(ShellMixin._SAFE_GIT_SUBCOMMANDS)}",
            '',
            'Allowed git flags before the subcommand:',
            f"  {_join(ShellMixin._GIT_FLAGS)}",
            '',
            'Dangerous git args that are BLOCKED (per subcommand):',
        ]
        for sub in sorted(ShellMixin._DANGEROUS_GIT_ARGS):
            lines.append(f"  git {sub}: {' '.join(sorted(ShellMixin._DANGEROUS_GIT_ARGS[sub]))}")
        lines += [
            '',
            "Safe pipe/filter stages (anything after a '|'):",
            # Any primary-safe command is also a valid stage, so list the union.
            f"  {_join(ShellMixin._PIPE_STAGE_COMMANDS)}",
            '',
            'Allowed patterns:',
            "  - 'cd <path> && <cmd>' or 'cd <path>; <cmd>' prefix (single command after)",
            "  - pipelines: '<safe cmd> | <safe filter> | ...'",
            '  - redirections to /dev/null, NUL, or file descriptors (e.g. 2>/dev/null, 2>&1) are allowed',
        ]
        if not restricted:
            lines += [
                '  - redirections to a NEW file inside your writable workspace are auto-approved and the file',
                '    becomes owned by you; redirecting over an EXISTING file, or to a path outside your writable',
                '    folders, still requires approval',
            ]
        lines += [
            '',
            'Async shell control commands:',
            f"  {_join(ShellMixin._CONTROL_COMMANDS)}, __heartbeat=N",
            '  (these require a tool_id from a previously launched async shell)',
            '',
            "NOT allowed: subshells $(...)/`...`, background '&', redirections that overwrite an existing file",
            "  or target a non-writable/unresolvable path ($VAR, globs, ~, quoted or glued '>' as in \"a>b\"),",
            "  cmd/powershell wrappers, chained '&&'/'||'/';' beyond the cd prefix.",
        ]
        return '\n'.join(lines)

    def _agent_is_restricted(self, agent_name: str) -> bool:
        """True if the calling agent instance is system-invoked (restricted shell).

        Resolves ``agent_name`` against ``self.agent_pool.instances``. Requires a real dict
        of instances so that MagicMock pools (whose ``instances`` is an auto-attribute mock,
        not a dict) degrade to False — never blocks and never mis-flags a mock pool. A real
        instance built before this change simply lacks the attribute → False via getattr.
        """
        pool = getattr(self, 'agent_pool', None)
        if not pool or not hasattr(pool, 'instances'):
            return False
        instances = pool.instances
        if not isinstance(instances, dict):
            return False
        inst = instances.get(agent_name)
        return bool(getattr(inst, 'restricted_shell', False))

    @staticmethod
    def _strip_cd_prefix(cmd: str) -> str:
        """Strip 'cd <path> &&' or 'cd <path>;' prefix from a command.

        Handles both && and ; chaining after cd. Returns the command
        with the cd prefix removed, or the original cmd if no cd prefix.
        """
        # Two-step parse: find the first && or ;, check prefix starts with cd
        sep_match = re.search(r'\s*(&&|;)\s*', cmd)
        if sep_match:
            prefix = cmd[:sep_match.start()].strip()
            rest = cmd[sep_match.end():].strip()
            if prefix.lower().startswith('cd'):
                return rest
        return cmd

    @staticmethod
    def _validate_pipeline_stages(pipeline: list, safe_commands: set) -> bool:
        """Validate that all pipe stages after the first use safe commands.

        Args:
            pipeline: List of pipeline stages (split by '|').
            safe_commands: Set of allowed pipe/filter command names.

        Returns:
            True if all pipe stages are safe (or there are no extra stages).
        """
        for stage in pipeline[1:]:
            stage_words = stage.strip().split()
            if not stage_words:
                continue
            if stage_words[0].lower() not in safe_commands:
                return False
        return True

    @staticmethod
    def _check_git_command(words: list, pipeline: list) -> bool:
        """Check if a git command is safe (read-only).

        Skips git flags before detecting the subcommand, then validates that
        the subcommand isn't followed by write-modifying arguments
        (e.g. stash drop, branch -d, tag -d).

        Args:
            words: Split words of the first pipeline stage.
            pipeline: All pipeline stages after splitting by '|'.

        Returns:
            True if the git command is read-only.
        """
        # Handle 'git' with no subcommand (shows help, safe)
        if len(words) == 1:
            return True

        # Flags that consume the next token as their value (space-separated)
        _flags_with_values = {'-c', '-C', '--git-dir', '--work-tree'}

        # Skip git flags to find the actual subcommand
        idx = 1  # words[0] is 'git'
        while idx < len(words):
            word = words[idx].lower()
            # Check for exact flag match or =-joined form (e.g. --git-dir=.git)
            flag = word.split('=', 1)[0]
            if flag not in ShellMixin._GIT_FLAGS:
                break
            if flag in _flags_with_values:
                if '=' in words[idx]:
                    idx += 1  # --git-dir=.git is one token
                elif idx + 1 < len(words):
                    idx += 2  # -c color.ui=always is two tokens
                else:
                    idx += 1
            else:
                idx += 1

        if idx >= len(words):
            return True  # git with only flags is safe

        subcommand = words[idx].lower()
        if subcommand not in ShellMixin._SAFE_GIT_SUBCOMMANDS:
            return False

        # Collect remaining arguments after the subcommand
        args = [w.lower() for w in words[idx + 1:]]

        # Validate subcommand-specific arguments
        if args and args[0] in ShellMixin._DANGEROUS_GIT_ARGS.get(subcommand, set()):
            return False

        # Validate pipe stages are safe (any primary-safe command is also a valid stage)
        return ShellMixin._validate_pipeline_stages(pipeline, ShellMixin._PIPE_STAGE_COMMANDS)

    # ------------------------------------------------------------------
    @staticmethod
    def _detect_multiline_python(command: str) -> bool:
        """Check if a Python launcher's -c/-e argument contains a literal newline.

        Returns True when a ``python``/``python3``/``py`` command carries a ``-c``/``-e``
        script argument whose value spans a newline. cmd.exe treats an embedded newline
        as a command separator, so the child never receives the full script and the tool
        reports success with no output (BUG_0012).

        The old ``[^"]*\\n[^"]*`` regex could not match an argument containing an inner
        double-quote — the common case — so the hint never fired. This locates the flag
        with :data:`_PYTHON_C_FLAG_RE`, then walks the quoted argument tracking quote
        state (same model as :meth:`ShellCmd._split_command_stages`): a newline *inside*
        the argument's quotes is the signal, while an unquoted newline is a separator that
        ends the argument. Handles single- and double-quoted args and inner quotes of either
        kind. Single quotes are treated as argument boundaries here — unlike the
        ``;``→``&`` translator (which honors cmd's literal-``'`` semantics), a newline
        inside a single-quoted ``-c`` arg is still split by cmd.exe, so it must be flagged.
        Pure and side-effect-free; no host I/O.
        """
        m = _PYTHON_C_FLAG_RE.search(command)
        if not m:
            return False

        quote = None  # active quote char, or None when unquoted
        i = m.end()
        n = len(command)
        while i < n:
            ch = command[i]
            if quote:
                if ch == quote:
                    quote = None
                elif ch == '\n':
                    return True  # newline inside the quoted -c argument
            elif ch in ('"', "'"):
                quote = ch
            elif ch in (' ', '\t', '\n', '&', '|', ';'):
                return False  # unquoted whitespace/separator ends the argument
            i += 1
        return False

    @staticmethod
    def _multiline_python_hint() -> str:
        """Return a system hint for multi-line python -c usage on Windows."""
        return "[SYSTEM: Multi-line python -c detected. On Windows, use semicolons (;) instead of newlines inside quoted strings, e.g., python -c \"import sys; print(sys.version)\"]"

    def _configure_windows_utf8(self, command: str) -> Tuple[str, int]:
        """Prepend chcp 65001 to force CMD into UTF-8 mode on Windows.

        Returns:
            (modified_command, creationflags) tuple ready for subprocess.Popen.
        """
        return configure_windows_utf8(command, create_new_console=False)

    # ------------------------------------------------------------------
    def _terminate_process_tree_windows(self, pid: int):
        """Kill a Windows process and all its descendants.

        Three-stage strategy to handle race conditions where children spawn
        new processes between termination passes:

          1. taskkill /F (first pass — kills the target + immediate children)
          2. Brief sleep then second taskkill /F — catches children that were
             spawned in the window between the first kill and process exit,
             since a dying process can fork new children before it fully terminates.
          3. WMIC sweep — discovers deeper descendants (grandchildren+) that may
             have been created during passes 1–2 and kills them individually.
        """
        # First pass: taskkill with tree flag
        try:
            result = _run_taskkill(pid)
            if result.returncode != 0:
                logger.warning(f"taskkill returned code {result.returncode} for PID {pid}")
        except Exception as e:
            logger.warning(f"taskkill failed for PID {pid}: {e}")

        # Second pass: retry taskkill after brief delay
        time.sleep(TASKKILL_RETRY_DELAY)
        try:
            result = _run_taskkill(pid)
            if result.returncode != 0:
                logger.debug(f"Second-pass taskkill returned code {result.returncode} for PID {pid}")
        except Exception as e:
            logger.debug(f"Second-pass taskkill failed (non-critical): {e}")

        # WMIC sweep for deeper descendants
        try:

            def _get_child_pids(parent_pid):
                """Query child PIDs of a given parent via WMIC."""
                res = subprocess.run(
                    ['wmic', 'process', 'where', f'ParentProcessId={parent_pid}', 'get', 'ProcessId'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding='utf-8',
                    errors='replace',
                )
                pids = []
                for line in res.stdout.strip().split('\n'):
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
                return pids

            descendants: set = set()
            to_check = [pid]
            depth = 0

            # Recurse until no new children are found, bounded by max depth guard
            while to_check and depth < MAX_PROCESS_TREE_DEPTH:
                next_level = []
                for check_pid in to_check:
                    children = _get_child_pids(check_pid)
                    for cpid in children:
                        if cpid not in descendants and cpid != pid:
                            descendants.add(cpid)
                            next_level.append(cpid)
                to_check = next_level
                depth += 1

            # Kill discovered descendants individually (best effort)
            for dpid in descendants:
                try:
                    _run_taskkill(dpid, timeout=5)
                except Exception as e:
                    logger.debug(f"WMIC child kill failed (non-critical): {e}")
        except Exception as e:
            logger.warning(f"WMIC descendant sweep failed: {e}")

    # ------------------------------------------------------------------
    def _resolve_shell_redirect_targets(
        self, raw_targets: list[str], resolved_cwd
    ) -> tuple[bool, list[Path], str | None]:
        """Decide whether EVERY redirect target is a NEW file in a writable root.

        Returns ``(all_eligible, eligible_paths, block_reason)``. ``all_eligible`` is
        False if ANY target fails (all-or-nothing, D4). A target is eligible only when
        it resolves inside a writable root (``resolve_tool_path(mode='rw')``) AND does
        not already exist. Fail-closed: any resolution error → not eligible. ``block_reason``
        is None when all targets are eligible; otherwise it is a concise, human-facing
        fragment naming the FIRST offending target and the SPECIFIC condition that tripped
        (already-exists / outside-writable-roots), so the approval prompt is not misleading
        about which target caused it.
        """
        from agent_cascade.utils.tool_path_resolver import resolve_tool_path
        eligible: list[Path] = []
        for raw in raw_targets:
            # 1. Build the candidate. Absolute / virtual-prefix tokens pass through
            #    unchanged; relative tokens are joined with the ACTUAL shell cwd
            #    (resolved_cwd), not base_dir — the shell runs with cwd=resolved_cwd.
            #    The join only fixes *which* path is checked; containment is re-checked
            #    by resolve_tool_path below (a `..\..\x` join can still escape).
            candidate = raw
            is_abs = (Path(raw).is_absolute()
                      or raw.startswith('/workspace')
                      or raw.startswith('/extra_rw_')
                      or raw.startswith('/extra_ro_'))
            if not is_abs:
                candidate = os.path.join(str(resolved_cwd), raw)
            # 2. Resolve against the writable-root set. Any failure → not eligible.
            try:
                resolved = resolve_tool_path(candidate, mode='rw', agent_pool=self.agent_pool)
            except Exception as e:
                logger.debug('[shell_cmd] redirect target %r not writable: %s', raw, e)
                return (False, [],
                        f"the redirect target {raw!r} lies outside your writable folders "
                        f"(or cannot be resolved), so it is not auto-approved")
            # 3. Existing file (or directory) → overwrite → not eligible.
            if os.path.exists(resolved):
                logger.debug('[shell_cmd] redirect target %r already exists: %s', raw, resolved)
                return (False, [],
                        f"the redirect target {raw!r} already exists, so it would be "
                        f"overwritten rather than created as a new file")
            eligible.append(resolved)
        return (True, eligible, None)

    def evaluate_shell_command_safety(
        self, command: str, cwd: str, agent_name: str, resolved_cwd=None,
        precomputed: tuple[bool, list[str]] | None = None,
    ) -> tuple[bool, list[Path]]:
        """Single approval decision shared by every shell gate (sync/async/advisor).

        Returns ``(is_safe, new_file_paths)`` where ``is_safe`` is the FINAL
        approval decision: structurally safe AND (no real-file targets, OR every
        target is a new file inside a writable root). ``new_file_paths`` are the
        targets proven absent at validation time (only non-empty when is_safe is
        True via the new-file path). Restricted/system-invoked agents must NOT use
        this widened value at their hard-reject gate — see the two-variable wiring
        in :meth:`execute_shell_command`. ``precomputed`` optionally supplies an
        already-computed ``(structural_ok, targets)`` from :meth:`_classify_shell_command`
        so a caller that classified the command once (the sync gate) does not re-scan it.
        ``agent_name`` is part of the public API (tests pass it by keyword) but is reserved
        for the restricted-agent gate / future use; the new-file resolver itself does not
        read it.
        """
        if precomputed is not None:
            ok, targets = precomputed
        else:
            ok, targets = ShellMixin._classify_shell_command(command)
        if not ok:
            return (False, [])
        if not targets:
            return (True, [])
        # Resolve cwd (reuse the caller's if supplied to avoid double work).
        if resolved_cwd is None:
            from agent_cascade.utils.tool_path_resolver import resolve_tool_path
            try:
                resolved_cwd = resolve_tool_path(cwd, mode='rw', agent_pool=self.agent_pool)
            except Exception:
                # The caller's own "Invalid working directory" path fires first in
                # execute_shell_command; here we simply decline to widen.
                return (False, [])
        all_ok, paths, _reason = self._resolve_shell_redirect_targets(targets, resolved_cwd)
        return (all_ok, paths if all_ok else [])

    # ------------------------------------------------------------------
    def execute_shell_command(
        self,
        command: str,
        justification: str,
        agent_name: str,
        cwd: str = '.',
        char_limit: int = 2000,
        timeout: Optional[int] = None,
        execution_mode: str = 'sync',
        before_execute: Optional[Callable[[], None]] = None,
    ) -> str:
        """Execute a shell command — auto-approved for safe read-only commands."""
        try:
            from agent_cascade.utils.tool_path_resolver import resolve_tool_path
            resolved_cwd = resolve_tool_path(cwd, mode='rw', agent_pool=self.agent_pool)
        except Exception as e:
            return f"ERROR: Invalid working directory: {str(e)}"

        if len(command) > char_limit:
            return f"ERROR: Command exceeds maximum length of {char_limit} characters."

        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0 or
                                    timeout > MAX_SHELL_TIMEOUT):
            return f"ERROR: Invalid timeout value: {timeout}. Must be a positive integer between 1 and {MAX_SHELL_TIMEOUT}."
        effective_timeout = timeout if timeout is not None else DEFAULT_SHELL_TIMEOUT

        # Two-variable split (§4.1): `strict` is the UNCHANGED read-only bool and is
        # the ONLY thing the restricted-agent gate may consult (D5: system-invoked
        # agents never get the new-file allowance). `is_safe` is the widened decision
        # (new-file-in-writable-root) and feeds ONLY the auto-approve branch and the
        # AUTO-APPROVED prefix. `new_file_targets` are targets proven absent + writable.
        structural_ok, redirect_targets = ShellMixin._classify_shell_command(command)
        strict = ShellMixin._is_safe_readonly_shell_command(
            command, precomputed=(structural_ok, redirect_targets))
        is_safe, new_file_targets = self.evaluate_shell_command_safety(
            command, cwd, agent_name, resolved_cwd=resolved_cwd,
            precomputed=(structural_ok, redirect_targets))

        # ── Restricted (system-invoked) agents: hard-reject non-read-only commands.
        #    No user approval prompt — there is no human in the loop for system agents. ──
        if not strict and self._agent_is_restricted(agent_name):
            return ('REJECTED: Command not allowed for system agents. '
                    'Only read-only filesystem/git commands are permitted. '
                    'Use __help to see allowed commands.')

        if is_safe:
            approved = True
            reason = 'Auto-approved: safe read-only filesystem operation'
            justification_text = reason
        else:
            description = (
                f"⚠️ **SECURITY WARNING**: This is a host shell command. It can potentially bypass folder restrictions!\n\n"
                f"**CWD**: {resolved_cwd}\n"
                f"**Execute Shell Command**:\n```bash\n{command}\n```\n**Justification**: {justification}")

            # §4.2: when the prompt is caused by a redirect, tell the user WHY — the
            # plain description gives no way to tell why a `git diff` needed approval.
            # The static scan is reused via the precomputed targets above; the filesystem
            # resolve repeats here only on the prompt path (about to block on human input),
            # which is acceptable, and surfaces the SPECIFIC condition the resolver tripped
            # (overwrite vs outside-root).
            if redirect_targets:
                _, _, _why = self._resolve_shell_redirect_targets(redirect_targets, resolved_cwd)
                if _why:
                    description += f"\n\nWhy this prompted: {_why}"

            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='shell_cmd',
                tool_args={
                    'command': command,
                    'justification': justification,
                    'cwd': cwd,
                    'execution_mode': execution_mode,
                    'timeout': effective_timeout,
                },
                description=description,
            )

            if not approved:
                return f"REJECTED: {reason}"
            justification_text = reason

        try:
            # ── Pre-execution hook (safe mode): runs only after approval, before spawn. ──
            # First statement inside this try so any exception it raises is caught by the
            # handler at the bottom (which returns an ERROR: string) — the caller's own
            # try/finally then still restores KV. Never runs on the rejection path above.
            if before_execute is not None:
                before_execute()

            # ── Platform-specific setup ──────────────────────────────
            original_command = command  # keep for multi-line detection hint
            # BUG_0139: build_shell_env() sets Windows UTF-8 handling and disables
            # git auto-gc so a background "geometric repack" cannot stall the command.
            env = build_shell_env()
            if ON_WINDOWS:
                command, creationflags = self._configure_windows_utf8(command)
            else:
                creationflags = 0

            exec_start = time.time()  # Local elapsed timing (sync mode has no task record)

            proc = subprocess.Popen(
                command,
                cwd=str(resolved_cwd),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=creationflags,
                start_new_session=True,
                env=env,
            )

            # Use threaded pipe reading to prevent output loss on timeout/hang.
            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []
            drain_errors: List[Exception] = []

            t_out = threading.Thread(target=drain_pipe_chunks,
                                     args=(proc.stdout, stdout_chunks, drain_errors),
                                     daemon=True,
                                     name='shell_stdout_reader')
            t_err = threading.Thread(target=drain_pipe_chunks,
                                     args=(proc.stderr, stderr_chunks, drain_errors),
                                     daemon=True,
                                     name='shell_stderr_reader')
            t_out.start()
            t_err.start()

            # Wait for process to finish within the timeout window
            try:
                proc.wait(timeout=effective_timeout)
                result_ok = True
            except subprocess.TimeoutExpired:
                result_ok = False
                if ON_WINDOWS:
                    self._terminate_process_tree_windows(proc.pid)
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception as e:
                        logger.debug(f"Unix process group kill failed (falling back to proc.kill): {e}")
                        try:
                            proc.kill()
                        except Exception as e:
                            logger.debug(f"Process kill fallback failed (non-critical): {e}")

            # Wait for reader threads to drain remaining pipe buffers.
            t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
            t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

            if t_out.is_alive() or t_err.is_alive():
                time.sleep(0.1)  # Brief grace period for thread cleanup

            if drain_errors:
                logger.warning(f"Pipe drain errors on PID {proc.pid}: " + '; '.join(str(e) for e in drain_errors))

            stdout: str = ''.join(stdout_chunks)
            stderr: str = ''.join(stderr_chunks)

            # ── Ownership marking for new-file redirects (§4.3) ──────────
            # Bookkeeping, NEVER a failure mode: guarded by its own try/except so an
            # OSError from _own/os.path.exists cannot turn a successful command into
            # "ERROR: Approved but execution failed" (the outer handler at the bottom).
            # Gated on "did not exist before" (new_file_targets were proven absent) +
            # "exists after" (created by this run) — not on exit status, since `git
            # diff > f` can exit non-zero yet still write the file.
            if new_file_targets:
                try:
                    for _owned_path in new_file_targets:
                        if os.path.exists(_owned_path):
                            self._own(_owned_path, agent_name)
                except Exception as _own_err:
                    logger.debug('[shell_cmd] redirect ownership marking failed: %s', _own_err)

            if result_ok:
                output = ''
                if stdout:
                    output += f"STDOUT:\n{stdout}\n"
                if stderr:
                    output += f"STDERR:\n{stderr}\n"

                if proc.returncode == 0:
                    status = 'Command completed successfully.'
                else:
                    status = f"Command exited with return code {proc.returncode}."

                if not output.strip():
                    # Detect multi-line python -c and append a hint on Windows
                    if ON_WINDOWS and self._detect_multiline_python(original_command):
                        output = f"No output produced.\n\n{self._multiline_python_hint()}"
                    else:
                        output = 'No output produced.'

                final_output = truncate_with_spillover(
                    output,
                    char_limit,
                    instance_name=agent_name,
                    tool_name='shell',
                    base_dir=self.base_dir,
                    operation_mode='mid',
                )
                if final_output is not output:
                    status += ' [TRUNCATED]'

                elapsed = time.time() - exec_start
                status += f" (elapsed {elapsed:.1f}s)"

                if is_safe:
                    final_msg = f"AUTO-APPROVED: {status}\n"
                else:
                    final_msg = f"APPROVED: {status}\n"
                    if justification_text:
                        final_msg += f"Security Justification: {justification_text}\n"

                # BUG_0139: a failed background auto-gc can make git exit non-zero
                # even though the primary operation succeeded. Surface the hint next
                # to the error instead of leaving a misleading bare exit code.
                hint = git_autogc_hint_for_output(stdout + stderr) if proc.returncode != 0 else ''
                if hint:
                    return final_msg + f"\n{final_output}\n\n{hint}"
                return final_msg + f"\n{final_output}"

            # ── Timeout path ────────────────────────────────────────
            output = ''
            if stdout:
                output += f"STDOUT (partial):\n{stdout}\n"
            if stderr:
                output += f"STDERR (partial):\n{stderr}\n"

            elapsed = time.time() - exec_start
            timeout_msg = (f"ERROR: Command timed out after {effective_timeout} seconds "
                           f"(elapsed {elapsed:.1f}s). "
                           f"All child processes have been forcibly terminated. "
                           f"Command was: `{command[:200]}`. "
                           f"If the process is expected to take a long time, consider using a background command "
                           f"(e.g. using '&' on linux or 'Start-Job' on windows) or optimizing the task.")

            # Append multi-line python hint if applicable
            if ON_WINDOWS and self._detect_multiline_python(original_command):
                timeout_msg += f"\n\n{self._multiline_python_hint()}"

            # BUG_0139: partial output may already contain git's "geometric repack"
            # failure. The commit/push/fetch often succeeded before auto-gc hung, so
            # tell the agent to check state rather than blindly retrying.
            autogc_hint = git_autogc_hint_for_output(stdout + stderr)
            if autogc_hint:
                timeout_msg += f"\n\n{autogc_hint}"

            if output.strip():
                return f"{timeout_msg}\n\n{output}"
            return timeout_msg

        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

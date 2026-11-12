"""Claude Code adapter for ``call_cli_agent``.

Verified against ``claude`` 2.1.2 ``--help`` (version-pinned, 2026-11-12):
* ``-p`` / ``--print`` — "Print response and exit"; workspace-trust dialog skipped.
* ``--output-format text`` — plain text (choices: text/json/stream-json; text is default).
* ``--permission-mode acceptEdits`` — default-safe autonomy (choices include acceptEdits,
  bypassPermissions, default, delegate, dontAsk, plan).
* ``--dangerously-skip-permissions`` — destructive YOLO, opt-in only (never a default).

Auth: own Claude subscription token (``claude setup-token``) or ``ANTHROPIC_API_KEY`` —
independent of AC, so no AC credential is required or inherited.
"""
from __future__ import annotations

import shutil

from agent_cascade.engine.cli_agent_adapters import (
    append_task_guarded, resolve_posture_flags, strip_ac_credentials)


class ClaudeCodeAdapter:
    """One-shot ``claude -p`` invocation with a default-safe permission posture."""

    harness = 'claude_code'
    binary = 'claude'
    output_format = 'text'  # --output-format text

    # Posture → argv tokens. 'bypass' is destructive and is never selected by the tool
    # schema (Phase 0 exposes only 'acceptEdits'); it exists here for a future config
    # opt-in and is gated by the runner, not by this adapter.
    _POSTURE_FLAGS: dict[str, list[str]] = {
        'acceptEdits': ['--permission-mode', 'acceptEdits'],
        'bypass': ['--dangerously-skip-permissions'],
    }

    def preflight(self) -> bool:
        """Presence probe: is ``claude`` on the PATH (host side)?"""
        return shutil.which(self.binary) is not None

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str) -> list[str]:
        """Build the one-shot argv. ``workspace`` is unused here — the working directory
        is set by the runner's ``docker run -w`` (keeps the adapter container-agnostic).

        The task is a single positional token (no shell → no metacharacter injection); a task
        starting with ``-`` is guarded with ``--`` (via the shared ``append_task_guarded``) so
        it is not parsed as a flag.
        """
        cmd = [self.binary, '-p', *resolve_posture_flags(self._POSTURE_FLAGS, posture),
               '--output-format', self.output_format]
        append_task_guarded(cmd, task)
        return cmd

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Strip AC's ``OPENAI_*`` credential namespace; keep harness-native auth."""
        return strip_ac_credentials(base_env)

    def project(self, *, workspace: str, skills, guidance) -> None:
        """NO-OP in Phase 0. Phase 2: project ``.claude/skills/`` + ``CLAUDE.md``."""
        return None

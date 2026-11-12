"""Google Antigravity CLI adapter for ``call_cli_agent``.

DEVIATION FROM PLAN (evidence-based): plan §12 assumed the binary + presence probe was
``antigravity``. Verified on the host (2026-11-12) and against ``agy`` 1.5.3 ``--help``:
``antigravity`` is NOT on PATH; the real binary is **``agy``**. The registry key / tool-schema
enum stays ``antigravity`` (plan §12), but ``binary``/``preflight``/``build_launch_cmd`` use
``agy``. This was confirmed both by ``where antigravity`` (absent) and ``where agy``
(present). See ``.agent_lessons/ai-coding-cli-headless-argv.md``.

Verified against ``agy`` 1.5.3 ``--help`` (version-pinned, 2026-11-12):
* ``-p`` (aliases ``--print`` / ``--prompt``) — "Run a single prompt non-interactively in
  headless mode and print the response." Response → stdout, diagnostics → stderr.
* Default output is plain text (``--output-format text``); clean stdout/stderr split.
* ``--mode accept-edits`` — documented autonomy analogue to the default-safe posture.
* ``--dangerously-skip-permissions`` — destructive, opt-in only.
* ``--print-timeout <sec>`` — added as a safety net: ``agy -p`` is reported (issue #318,
  v1.0.6) to HANG with no output in a non-TTY; default is 0 = wait forever. The runner's
  own hard wall-clock timeout already bounds this, but an explicit CLI-side timeout gives
  a clean self-exit before the container kill.

Auth: own Google AI account cache (interactive ``agy`` login once); independent of AC. An
unauthenticated CI run exits "authentication required" rather than hanging.
"""
from __future__ import annotations

import shutil

from agent_cascade.engine.cli_agent_adapters import (
    append_task_guarded, resolve_posture_flags, strip_ac_credentials)


class AntigravityAdapter:
    """One-shot ``agy -p`` invocation (headless; response on stdout)."""

    harness = 'antigravity'
    binary = 'agy'  # NOT 'antigravity' — see module docstring (verified deviation)
    output_format = 'text'  # --output-format text is agy's default; emitted for explicitness
    # Self-exit guard against the agy non-TTY hang (issue #318). Bounded well under the
    # runner's default wall-clock cap (1800s) so the CLI exits cleanly first.
    _PRINT_TIMEOUT_SEC = 600

    _POSTURE_FLAGS: dict[str, list[str]] = {
        'acceptEdits': ['--mode', 'accept-edits'],
        'bypass': ['--dangerously-skip-permissions'],
    }

    def preflight(self) -> bool:
        """Presence probe: is the ``agy`` binary on the PATH (host side)?"""
        return shutil.which(self.binary) is not None

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str) -> list[str]:
        """Build the one-shot argv: ``agy -p [--mode accept-edits] --print-timeout N -- <task>``.

        ``workspace`` is unused here — set by the runner's ``docker run -w``. The task is a
        single positional token guarded with ``--`` via the shared ``append_task_guarded``.
        """
        cmd = [
            self.binary, '-p', *resolve_posture_flags(self._POSTURE_FLAGS, posture),
            '--output-format', self.output_format,
            '--print-timeout', str(self._PRINT_TIMEOUT_SEC),
        ]
        append_task_guarded(cmd, task)
        return cmd

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Strip AC's ``OPENAI_*`` credential namespace; keep harness-native auth."""
        return strip_ac_credentials(base_env)

    def project(self, *, workspace: str, skills, guidance) -> None:
        """NO-OP in Phase 0. Phase 2: project agy rule files here."""
        return None

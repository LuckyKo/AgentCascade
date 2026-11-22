"""GitHub Copilot CLI adapter for ``call_cli_agent``.

Copilot CLI convention (best-known; the one-shot/print mode mirrors ``claude -p``):
* ``-p`` — one-shot "print the response and exit"; response → stdout, diagnostics → stderr.
* Default output is plain text, so (like opencode) the ``--output-format`` flag is declared
  but not emitted in argv (Phase 0 does not rely on a JSON contract).
* ``--allow-all-tools`` — auto-approve tool use (the default-safe autonomy posture). ``--yolo`` is
  the destructive, opt-in full-permissions bypass; it is never the default and is gated by the
  runner, not here.

If a future Copilot CLI release renames these flags, adjust ``_POSTURE_FLAGS`` only — the
argv shape, the shared posture resolver, and the task guard are already harness-agnostic.

Auth: GitHub's ``GITHUB_TOKEN`` (a GitHub account token, NOT an AC LLM key). The CLI
authenticates against GitHub with that token — independent of AgentCascade, so no AC
credential is required or inherited (the child env is stripped of ``OPENAI_*``).
"""
from __future__ import annotations

import shutil
from typing import Optional

from agent_cascade.engine.cli_agent_adapters import (
    append_task_guarded, resolve_posture_flags, strip_ac_credentials)


class CopilotAdapter:
    """One-shot ``copilot -p`` invocation with a default-safe permission posture."""

    harness = 'copilot'
    binary = 'copilot'  # the GitHub Copilot CLI executable
    output_format = 'text'  # plain text is copilot -p's default output; emitted nowhere in argv
    # GitHub's token env var. The CLI authenticates against GitHub with it — not an AC key —
    # so the runner injects no AC credential and status reports key_configured from cli_copilot_key.
    auth_env_var = 'GITHUB_TOKEN'
    auth_type = 'api_key'
    auth_volume_target = None  # key-based auth: no persistent volume needed
    model_flag = None  # copilot -p exposes no --model flag; the model follows the GitHub plan.
                       # A requested model is therefore ignored (schema: "harnesses without a
                       # documented model flag ignore it"), so no --model block is emitted here.

    # Posture → argv tokens. 'bypass' is destructive and is never selected by the tool schema
    # (Phase 0 exposes only 'acceptEdits'); it exists here for a future config opt-in and is
    # gated by the runner, not by this adapter. '--allow-all-tools' is the default-safe autonomy
    # token (non-interactive mode denies tools it is not told to allow).
    _POSTURE_FLAGS: dict[str, list[str]] = {
        'acceptEdits': ['--allow-all-tools'],  # auto-approve tool use
        'bypass': ['--yolo'],  # full-permissions bypass; destructive, opt-in only
    }

    def preflight(self) -> bool:
        """Presence probe: is ``copilot`` on the PATH (host side)?"""
        return shutil.which(self.binary) is not None

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str,
                         model: Optional[str] = None) -> list[str]:
        """Build the one-shot argv: ``copilot -p <task> [--allow-all-tools | --yolo]``.

        The task value MUST immediately follow ``-p`` (clap requires ``--prompt <text>`` to have
        its value there); the posture flags trail it. ``workspace`` is unused here — the working
        directory is set by the runner's ``docker run -w`` (keeps the adapter container-agnostic).
        The task is a single token (no shell → no metacharacter injection); a task starting with
        ``-`` is guarded with ``--`` (via the shared ``append_task_guarded``) so it is not parsed
        as a flag. ``model`` is ignored — copilot -p exposes no model flag (``model_flag`` is
        None), so a requested model leaves the argv unchanged (byte-safe default).
        """
        cmd = [self.binary, '-p']
        append_task_guarded(cmd, task)  # task value must immediately follow -p
        cmd.extend(resolve_posture_flags(self._POSTURE_FLAGS, posture))
        return cmd

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Strip AC's ``OPENAI_*`` credential namespace; keep the GitHub-native auth token."""
        return strip_ac_credentials(base_env)

    def project(self, *, workspace: str, skills, guidance) -> None:
        """NO-OP in Phase 0. Phase 2: project ``.github/copilot-instructions.md`` here."""
        return None

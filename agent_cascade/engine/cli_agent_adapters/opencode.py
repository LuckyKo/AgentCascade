"""opencode adapter for ``call_cli_agent``.

Verified against ``opencode`` 2.2.7 ``run --help`` (version-pinned, 2026-11-12):
* ``opencode run`` — "Run OpenCode with a message"; runs WITHOUT opening the interactive
  TUI, so stdout capture is unambiguous.
* Prompt is a POSITIONAL ``message...`` token, not a ``-p`` value.
* There is NO ``--output`` flag. ``--format {default,json}`` only; plain text is the
  ``default`` format → we omit the flag (a deviation from the plan's ``--output`` guess).
* ``--auto`` — "Auto-approve permissions that are not explicitly denied" (respects deny
  rules, so not a full bypass) — closest documented analogue to the default-safe posture.

Auth: own provider auth (``opencode auth``) or provider env keys — independent of AC.
"""
from __future__ import annotations

import shutil
from typing import Optional

from agent_cascade.engine.cli_agent_adapters import (
    append_task_guarded, resolve_posture_flags, strip_ac_credentials)


class OpencodeAdapter:
    """One-shot ``opencode run`` invocation (plain-text default output, no TUI)."""

    harness = 'opencode'
    binary = 'opencode'
    output_format = 'default'  # plain text is opencode's default --format; no flag emitted
    # opencode reads per-provider env keys (no single native var). The stored key is injected as
    # the most common provider's var (OPENAI_API_KEY); a non-OpenAI provider needs a
    # cli_opencode_key_env follow-up. See DIG finding #2 / plan open-assumption A2.
    auth_env_var = 'OPENAI_API_KEY'
    model_flag = '--model'  # opencode run -m/--model provider/model

    # Posture → argv tokens. opencode has no documented destructive bypass flag; 'bypass'
    # intentionally maps to the same default-safe ``--auto`` (we never emit a YOLO token).
    # TODO(verify): confirm ``--auto`` is the precise 'acceptEdits' equivalent — it is the
    #   documented non-bypass autonomy option, but its exact permission semantics for
    #   file-mutation vs bash-tool approval in non-interactive mode are unconfirmed.
    _POSTURE_FLAGS: dict[str, list[str]] = {
        'acceptEdits': ['--auto'],
        'bypass': ['--auto'],  # no opencode bypass flag exists; stay default-safe
    }

    def preflight(self) -> bool:
        """Presence probe: is ``opencode`` on the PATH (host side)?"""
        return shutil.which(self.binary) is not None

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str,
                         model: Optional[str] = None) -> list[str]:
        """Build the one-shot argv: ``opencode run [--auto] [--model M] [--] <task>``.

        ``workspace`` is unused here — set by the runner's ``docker run -w``. The task is a
        single positional token guarded with ``--`` via the shared ``append_task_guarded``.
        When ``model`` is set it is appended as ``--model <model>`` (no model → no flag).
        """
        cmd = [self.binary, 'run', *resolve_posture_flags(self._POSTURE_FLAGS, posture)]
        if model and self.model_flag:
            cmd += [self.model_flag, model]
        append_task_guarded(cmd, task)
        return cmd

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Strip AC's ``OPENAI_*`` credential namespace; keep harness-native auth."""
        return strip_ac_credentials(base_env)

    def project(self, *, workspace: str, skills, guidance) -> None:
        """NO-OP in Phase 0. Phase 2: project opencode's AGENTS.md / rules here."""
        return None

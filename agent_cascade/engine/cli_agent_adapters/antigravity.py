"""Google Antigravity CLI adapter for ``call_cli_agent``.

DEVIATION FROM PLAN (evidence-based): plan §12 assumed the binary + presence probe was
``antigravity``. Verified on the host (2026-11-12) and against ``agy --help``:
``antigravity`` is NOT on PATH; the real binary is **``agy``**. The registry key / tool-schema
enum stays ``antigravity`` (plan §12), but ``binary``/``preflight``/``build_launch_cmd`` use
``agy``. This was confirmed both by ``where antigravity`` (absent) and ``where agy``
(present). See ``.agent_lessons/agy-headless-auth-credential-volume.md``.

Verified against ``agy`` 1.5.8 ``--help`` (re-verified 2026-11-15; the Docker image installs
the latest release, so the docstring tracks the current binary rather than a pin):
* ``-p`` (aliases ``--print`` / ``--prompt``) — "Run a single prompt non-interactively in
  headless mode and print the response." Response → stdout, diagnostics → stderr.
* Default output is plain text (``--output-format text``); clean stdout/stderr split.
* ``--mode accept-edits|auto|plan`` — autonomy modes. ``accept-edits`` pre-approves routine
  file edits only; it does NOT auto-approve shell/non-edit tools. ``auto`` exists in 1.5.8
  but its exact tool-approval semantics are unverified, so the explicit flag below is used.
* ``--model`` — model-selection flag (documented in 1.5.8); ``--effort low|medium|high|xhigh|max``.
* ``--dangerously-skip-permissions`` — auto-approve all tool permission requests.
* ``--print-timeout <sec>`` — Go duration string; bounds the model turn only.

Headless permission posture: with no TTY any approval prompt is unanswerable (it aborts or
burns the print timeout), so ``acceptEdits`` MUST also carry ``--dangerously-skip-permissions``.
Container containment is unchanged and owned by the runner (``--cap-drop=ALL``,
``--security-opt=no-new-privileges``, cgroup caps, workspace-scoped mounts).

Auth: Google OAuth 2.0 account store — there is NO env-var API key (the 1.5.8 binary reads
no ``GEMINI_API_KEY``/``GOOGLE_API_KEY``). On Windows the token lives in Credential Manager
(``cmdkey /list`` → ``LegacyGeneric:target=gemini:antigravity``, DPAPI-encrypted), which a
Linux container cannot read; inside the container agy keeps a file store under
``/root/.gemini``. That path must be backed by a persistent named Docker volume so a
one-time interactive login survives across one-shot runs (``auth_volume_target`` below).
An unauthenticated run prints the OAuth URL, waits 60 s, and exits "authentication
required" rather than hanging.
"""
from __future__ import annotations

import shutil
from typing import Optional

from agent_cascade.engine.cli_agent_adapters import (
    resolve_posture_flags, strip_ac_credentials)


class AntigravityAdapter:
    """One-shot ``agy -p`` invocation (headless; response on stdout)."""

    harness = 'antigravity'
    binary = 'agy'  # NOT 'antigravity' — see module docstring (verified deviation)
    output_format = 'text'  # --output-format text is agy's default; emitted for explicitness
    # agy authenticates with a cached interactive Google OAuth login — there is NO env-var API
    # key, so the runner injects no key and status reports key_configured=None (DIG finding #2).
    auth_env_var = None
    # OAuth: the account store lives under auth_volume_target and must be backed by a persistent
    # named Docker volume (Windows host tokens are DPAPI/keyring-bound → not bind-mountable).
    auth_type = 'oauth'
    auth_volume_target = '/root/.gemini'
    # Documented in agy 1.5.8; appended only when a model actually resolves (byte-safe otherwise).
    model_flag = '--model'
    # Self-exit guard against the agy non-TTY hang. Bounded well under the runner's default
    # wall-clock cap (1800s) so the CLI exits cleanly first. agy's --print-timeout is a Go
    # duration string, so the value is emitted with an 's' suffix. It does NOT extend agy's own
    # 60 s interactive OAuth wait.
    _PRINT_TIMEOUT_SEC = 600

    # No TTY in the container, so an approval prompt is unanswerable: acceptEdits must also
    # auto-approve tools. Containment is owned by the runner's docker flags, not this argv.
    _POSTURE_FLAGS: dict[str, list[str]] = {
        'acceptEdits': ['--mode', 'accept-edits', '--dangerously-skip-permissions'],
        'bypass': ['--dangerously-skip-permissions'],
    }

    def preflight(self) -> bool:
        """Presence probe: is the ``agy`` binary on the PATH (host side)?"""
        return shutil.which(self.binary) is not None

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str,
                         model: Optional[str] = None) -> list[str]:
        """Build the one-shot argv: ``agy [--mode accept-edits --dangerously-skip-permissions] [--model M] --output-format text --print-timeout Ns -p=<task>``.

        ``workspace`` is unused here — set by the runner's ``docker run -w``. The prompt is
        attached to ``-p`` via ``=`` and placed LAST because agy's ``-p`` is greedy: a bare
        ``-p <task>`` would let it consume the following flag (e.g. ``--mode``) as its prompt
        value (verified live). ``model`` is emitted as ``--model <model>`` only when supplied
        (agy 1.5.8 documents the flag); with no model the argv is unchanged.
        """
        cmd = [
            self.binary, *resolve_posture_flags(self._POSTURE_FLAGS, posture),
            '--output-format', self.output_format,
            '--print-timeout', f'{self._PRINT_TIMEOUT_SEC}s',
        ]
        if model and self.model_flag:
            cmd += [self.model_flag, model]
        cmd += [f'-p={task}']
        return cmd

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Strip AC's ``OPENAI_*`` credential namespace; keep harness-native auth."""
        return strip_ac_credentials(base_env)

    def project(self, *, workspace: str, skills, guidance) -> None:
        """NO-OP in Phase 0. Phase 2: project agy rule files here."""
        return None

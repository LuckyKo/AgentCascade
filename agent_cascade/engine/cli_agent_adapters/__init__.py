"""Runtime-adapter registry for external CLI coding agents (``call_cli_agent``).

A *harness* is an external AI coding CLI (Claude Code, opencode, Google Antigravity
``agy``). Everything else in the ``call_cli_agent`` pipeline — the one-shot Docker
runner, concurrency cap, approval + Security-advisor gating, untrusted wrapping,
hard timeout + container teardown, no-credential-inheritance — is **harness-agnostic**.

An adapter is the only harness-specific piece. It supplies:

* ``preflight``     — presence probe (``shutil.which``); False → ``HarnessNotFoundError``.
* ``build_launch_cmd`` — the argv list (no ``shell=True``), one-shot, final answer on stdout.
* ``child_env``     — strips AC-owned credentials from the inherited env.
* ``project``       — workspace/skill projection (NO-OP in Phase 0; Phase 2 insertion point).

The registry key (and the tool schema enum) is the *harness name* (``claude_code``,
``opencode``, ``antigravity``); the *binary token* inside ``build_launch_cmd`` is the
CLI's real executable, which differs from the harness name for Antigravity
(``agy``, not ``antigravity`` — verified against the installed binary).
"""
from __future__ import annotations

from typing import Optional, Protocol


class HarnessNotFoundError(Exception):
    """Raised when the requested harness is unregistered, or its CLI is not installed.

    Subclasses ``Exception`` (not ``ValueError``) so callers can distinguish a genuine
    "this harness does not exist" from an argument-type error while still being catchable
    generically. The handler converts it into a clean tool-result string — never a crash.
    """


class RuntimeAdapter(Protocol):
    """The minimal contract every external-CLI adapter must satisfy.

    Adapters are pure (no slot/KV awareness). The runner calls exactly these methods.
    """

    harness: str  # registry key, matches the tool-schema enum value
    binary: str  # real CLI executable token used inside build_launch_cmd's argv
    output_format: str  # per-adapter output-format default; baked into argv only when the CLI
                        # exposes the flag — opencode omits it (plain text is already its default)
    auth_env_var: Optional[str]  # container env var this CLI reads for auth; None == not an API-key
                                 # model (e.g. antigravity uses a cached interactive OAuth login)
    model_flag: Optional[str]  # argv flag that selects the model (e.g. '--model'); None == no flag
                               # is emitted even when a model is requested (byte-safe default)

    def preflight(self) -> bool:
        """Return True when this harness's CLI is present and usable (presence probe)."""
        ...

    def build_launch_cmd(self, *, task: str, workspace: str, posture: str,
                         model: Optional[str] = None) -> list[str]:
        """Return the one-shot argv list (NO ``shell=True``) that runs ``task`` once.

        ``posture`` is a resolved, already-authorized permission posture (e.g.
        ``'acceptEdits'``). The final answer MUST go to stdout; diagnostics to stderr.
        ``workspace`` is already the container CWD via the runner's ``docker run -w``; it is
        carried here for a future (Phase 2) adapter that needs it inside argv.
        ``model`` (when non-empty) is appended as ``[model_flag, model]`` ONLY if the adapter
        declares a ``model_flag``; with no flag or no model the argv is unchanged (byte-safe).
        """
        ...

    # Optional per-harness quota probe: ``probe_quota(self) -> str`` returning
    # ``'ok' | 'exhausted' | 'unknown'``. Adapters MAY implement it; the status builder
    # treats its absence (or any exception) as ``'unknown'`` so a 4th adapter gets a
    # working status surface with zero extra code and no run is ever blocked.

    def child_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Return a copy of ``base_env`` with AC-owned credentials stripped."""
        ...

    def project(self, *, workspace: str, skills, guidance) -> None:
        """Phase-2 insertion point: project skills/guidance into ``workspace``. NO-OP in Phase 0."""
        ...


def strip_ac_credentials(base_env: dict[str, str]) -> dict[str, str]:
    """Strip AC-owned LLM credentials from a child env. Shared by all adapters (DRY).

    Defined BEFORE ``_build_registry`` because the adapter modules import it at their
    module top, and the eager registry build below imports those adapters.

    AgentCascade's OpenAI-compatible clients read the key/endpoint from the ``OPENAI_*``
    env namespace (``llm/oai.py:408`` / ``llm/azure.py:36``), so every ``OPENAI_*`` var is
    an AC secret and must never reach an external child. Harness-native auth (a Claude
    subscription token, an opencode provider key, an ``agy`` account cache) is preserved
    — those CLIs authenticate with their own account store, not AC's key, so over-stripping
    every ``*_API_KEY`` would break harness auth for no security gain. Conservative: only
    the AC namespace is removed; everything else passes through unchanged.
    """
    return {k: v for k, v in base_env.items() if not k.upper().startswith('OPENAI_')}


def resolve_posture_flags(posture_flags: dict[str, list[str]], posture: str) -> list[str]:
    """Return the argv tokens for ``posture``, falling back to the default-safe
    ``'acceptEdits'`` entry when the posture is unrecognized. Shared by every adapter so the
    fallback policy cannot drift between adapters (a 4th adapter gets it for free).
    """
    return posture_flags.get(posture, posture_flags['acceptEdits'])


def append_task_guarded(cmd: list[str], task: str) -> None:
    """Append ``task`` to ``cmd`` as one positional token, prefixed with ``--`` when it starts
    with ``-`` so a task is never parsed as a CLI flag (flag-injection guard). Shared by every
    adapter — a new adapter using this cannot silently omit the guard.
    """
    if task.startswith('-'):
        cmd.append('--')
    cmd.append(task)


def _build_registry() -> dict[str, RuntimeAdapter]:
    """Eagerly construct the Phase-0 adapter registry from the three harness modules.

    Explicit (not self-registering) so the registry contents are statically obvious and
    there is no import-order magic. Each adapter is stateless and reusable.
    """
    from agent_cascade.engine.cli_agent_adapters.antigravity import AntigravityAdapter
    from agent_cascade.engine.cli_agent_adapters.claude_code import ClaudeCodeAdapter
    from agent_cascade.engine.cli_agent_adapters.opencode import OpencodeAdapter

    return {
        a.harness: a
        for a in (ClaudeCodeAdapter(), OpencodeAdapter(), AntigravityAdapter())
    }


_ADAPTERS: dict[str, RuntimeAdapter] = _build_registry()


def get_adapter(harness: str) -> RuntimeAdapter:
    """Return the adapter for ``harness`` or raise ``HarnessNotFoundError``.

    Unknown harness names are rejected here (before any approval/advisor/spawn), so an
    unsupported name never reaches the gating path.
    """
    adapter = _ADAPTERS.get(harness)
    if adapter is None:
        raise HarnessNotFoundError(
            f"Unknown harness '{harness}'. Registered: {', '.join(sorted(_ADAPTERS))}.")
    return adapter


def registered_harnesses() -> tuple[str, ...]:
    """Sorted tuple of registered harness names. Live consumer: the schema-drift test asserting dna enum == registry; also used to build the tool schema."""
    return tuple(sorted(_ADAPTERS))

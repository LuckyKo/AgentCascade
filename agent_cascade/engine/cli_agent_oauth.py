"""Manual OAuth login command + auth re-check for key-less external CLI harnesses (call_cli_agent).

One-shot runs are container-isolated, so a harness that authenticates with OAuth (agy) keeps its
account store in a persistent Docker volume (``cli_agent_agy_auth``). agy's OAuth URL is
**TTY-bound** — it is printed only in a real terminal — so it cannot be driven from a one-shot
container with piped stdin/stdout (see ``.agent_lessons/bug-0307b-agy-oauth-minimal-manual-login.md``).
The earlier interactive ``start_session`` flow (spawn a piped container, scrape the consent URL,
relay a pasted code) therefore always settled ``expired`` and has been removed.

The supported flow is manual + re-check, both owned here:

* :meth:`CliAgentOAuthManager.build_manual_command` returns the one-time ``docker run -it`` command
  the user runs **in a real terminal**; agy prints the OAuth URL there, the human signs in, and the
  token is written into the shared volume. Every subsequent one-shot run already mounts that volume,
  so it is zero-touch.
* :meth:`CliAgentOAuthManager.recheck_auth` invalidates the cached volume probe and re-reads the
  authenticated state, so the Web UI's "Re-check auth" button (and tab-open) reflects a just-
  completed manual login without a restart.

Nothing here spawns a container in-process or opens a browser: it only builds a command string and
reads (cached) volume state. The login container itself is run by the user, by hand.
"""
from __future__ import annotations

import threading
from typing import Any, Optional


class CliAgentOAuthManager:
    """Thin stateless surface for the manual-login + re-check flow.

    No live sessions are tracked — the login runs in the user's own terminal, not in-process.
    """

    def build_manual_command(self, harness: str, *, pool: Optional[Any] = None) -> str:
        """The one-time ``docker run -it`` command the user runs in a real terminal to log in.

        Resolves the auth volume (``_resolve_auth_volume``), the container mount target
        (``adapter.auth_volume_target``), the image (``_cli_agent_image``) and the interactive argv
        (``adapter.build_interactive_cmd()`` — the bare binary, NOT the headless ``-p`` launch form),
        then joins them into a single copyable string::

            docker run -it -v <volume>:<target> <image> <binary>

        Raises ``HarnessNotFoundError`` for an unknown harness, and ``ValueError`` when the harness
        declares no OAuth volume/target (api-key harness, or the volume knob is off).
        """
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        from agent_cascade.engine.cli_agent_runner import _cli_agent_image, _resolve_auth_volume
        adapter = get_adapter(harness)
        target = getattr(adapter, 'auth_volume_target', None)
        volume = _resolve_auth_volume(pool, adapter)
        if not volume or not target:
            raise ValueError(
                f"harness '{harness}' has no OAuth credential volume/target "
                '(set cli_agent_agy_auth_volume in Settings → CLI Agents)')
        image = _cli_agent_image(harness, pool)
        interactive = adapter.build_interactive_cmd()
        return ' '.join(['docker', 'run', '-it', '-v', f'{volume}:{target}', image, *interactive])

    def recheck_auth(self, harness: str, *, pool: Optional[Any] = None) -> bool:
        """Re-read whether the OAuth volume is authenticated. True only on a definitive hit.

        Drops the cached probe for this volume first (so the result reflects a manual login that
        just completed), then re-probes. No volume, an indeterminate probe (docker unavailable), or
        an unauthenticated volume all return ``False`` so the UI shows "not authenticated" and the
        user can re-check after finishing the manual login.
        """
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        from agent_cascade.engine.cli_agent_runner import (
            _agy_auth_volume_populated, _cli_agent_image, _invalidate_agy_auth_cache,
            _resolve_auth_volume)
        adapter = get_adapter(harness)
        volume = _resolve_auth_volume(pool, adapter)
        if not volume:
            return False
        target = getattr(adapter, 'auth_volume_target', None) or '/root/.gemini'
        image = _cli_agent_image(harness, pool)
        _invalidate_agy_auth_cache(volume)
        return bool(_agy_auth_volume_populated(volume, image, target=target))


_MANAGER: Optional[CliAgentOAuthManager] = None
_MANAGER_LOCK = threading.Lock()


def get_oauth_manager() -> CliAgentOAuthManager:
    """Process-wide singleton manager (stateless, but kept for a single stable entry point)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = CliAgentOAuthManager()
        return _MANAGER


def reset_oauth_manager() -> None:
    """Test hook: drop the singleton (never called by production code)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None

"""Shared best-effort kill helper for in-flight background tool runners.

Provides a single source of truth for the per-tracker ``kill_all`` calls
that both the WS terminate-tools handler and the snapshot/stack-swap park
path need. Each tracker is wrapped in try/except so one tracker's absence
or failure never aborts the other (and never propagates to the caller).
"""

import logging

logger = logging.getLogger(__name__)


def kill_instance_tools(pool, instance_name: str) -> tuple[int, int]:
    """Kill an instance's in-flight background tool runners (best-effort).

    Calls ``kill_all`` on both the async-shell tracker and the CLI-agent
    tracker, wrapping each in try/except so one tracker's absence or
    failure never aborts the other.

    Returns:
        (shell_count, cli_count) — the number of tasks killed in each tracker.
    """
    shell_count = 0
    cli_count = 0

    shell = getattr(pool, '_async_shell_tracker', None)
    if shell is not None and hasattr(shell, 'kill_all'):
        try:
            shell_count = shell.kill_all(instance_name)
            if shell_count:
                logger.debug(
                    '[kill_instance_tools] killed %d async shell task(s) for %s',
                    shell_count, instance_name,
                )
        except Exception as e:  # noqa: BLE001 — tracker teardown is non-critical
            logger.debug(
                '[kill_instance_tools] async shell kill_all failed for %s: %s',
                instance_name, e,
            )

    cli = getattr(pool, '_cli_agent_tracker', None)
    if cli is not None and hasattr(cli, 'kill_all'):
        try:
            cli_count = cli.kill_all(instance_name)
            if cli_count:
                logger.debug(
                    '[kill_instance_tools] killed %d call_cli_agent run(s) for %s',
                    cli_count, instance_name,
                )
        except Exception as e:  # noqa: BLE001 — tracker teardown is non-critical
            logger.debug(
                '[kill_instance_tools] call_cli_agent kill_all failed for %s: %s',
                instance_name, e,
            )

    return shell_count, cli_count

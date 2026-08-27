"""Wakeup helpers — relaunch an IDLE agent when an agent-to-agent message arrives.

Background (idle-wakeup fix, Fix B): the user-message path wakes agents by
enqueueing the message AND driving ``run_agent_in_pool`` in a thread
(``api_integration_pkg/runner.py``). Agent-to-agent sources (async tool
completion, child dismissal) only enqueued + notified — so an IDLE parent
whose ``run()`` thread had already exited never woke up to process them.

``relaunch_idle_agent`` mirrors the user path for those sources: if the target
instance is IDLE (and the pool is not stopping / instance not terminated), it
spawns a daemon thread that drives ``run_agent_in_pool`` and drains its yields
(background wakeups need no WebSocket forwarding). The engine's atomic
IDLE→RUNNING entry guard (L1 race guard in ``engine/core.py``) makes double
launch safe: a racing second launcher hits the RuntimeError, which is caught
and logged at DEBUG.

Imports of ``run_agent_in_pool`` / ``ExecutionEngine`` are deliberately LOCAL
to the functions below — module-level imports would create circular imports
(``api_integration_pkg.runner`` pulls in the execution engine and pool stack).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from agent_cascade.log import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent_cascade.agent_pool import AgentPool


def _drive_run(pool, instance_name: str) -> None:
    """Thread target: exhaust the run_agent_in_pool generator for one instance.

    Yields are UI state updates; background wakeups silently drain them (the
    plan's default). The L1 race guard in ``engine.run()`` raises RuntimeError
    when a concurrent launcher already owns the instance — that is expected on
    double-launch and must not surface as an error. Any OTHER exception type
    is logged at ERROR so real failures are visible.

    Thread registration mirrors ``run_agent_unified.py``: register under
    ``pool._instance_threads`` before driving (so ``dismiss_instance`` can
    join it) and pop in ``finally`` to avoid leaks.
    """
    # Local import — avoids circular imports at module load time.
    from agent_cascade.api_integration_pkg.runner import run_agent_in_pool

    # Register this thread with the pool so dismiss/stop can find and join it.
    if hasattr(pool, '_instance_threads') and hasattr(pool, '_instance_threads_lock'):
        try:
            with pool._instance_threads_lock:
                pool._instance_threads[instance_name] = threading.current_thread()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[WAKEUP_RELAUNCH] Thread registration failed for '{instance_name}' (non-critical): {e}")

    try:
        try:
            for _ in run_agent_in_pool(pool, instance_name):
                pass  # Drain yields — background wakeup, no WS forwarding needed.
        except RuntimeError as e:
            # Expected on concurrent wakeup: engine.run()'s atomic IDLE→RUNNING
            # transition (L1 race guard) raises when a second launcher races past
            # the pre-check and loses the state race. Not an error — another run
            # owns the instance now.
            logger.debug(
                f"[WAKEUP_RELAUNCH] {instance_name}: concurrent wakeup, "
                f"another run owns the instance (L1 guard): {e}"
            )
        except Exception as e:
            logger.error(f"[WAKEUP_RELAUNCH] relaunch drive failed for '{instance_name}': {e}")
    finally:
        # Clean up thread registration on completion to prevent memory leaks.
        # Dismissed agents have this cleaned up by dismiss_instance(); pop(name, None)
        # is safe if it was already removed there.
        if hasattr(pool, '_instance_threads') and hasattr(pool, '_instance_threads_lock'):
            try:
                with pool._instance_threads_lock:
                    pool._instance_threads.pop(instance_name, None)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"[WAKEUP_RELAUNCH] Thread registration cleanup failed for '{instance_name}' (non-critical): {e}")


def relaunch_idle_agent(pool, instance_name: str) -> bool:
    """Relaunch an IDLE agent so it processes its queued messages.

    Mirrors the user-message path (``runner.py``: enqueue + drive
    ``run_agent_in_pool`` in a thread) for agent-to-agent wakeup sources
    (async tool completion, child dismissal). This is a no-op — returns
    False and spawns nothing — in every case where a live thread or shutdown
    already owns the instance:

    - instance does not exist;
    - pool is stopped / shutting down;
    - instance is terminated;
    - instance state is not IDLE (SLEEPING/RUNNING/COMPLETING have a live
      thread that drains the queue itself).

    The pre-check under ``_state_lock`` is an optimization; the authoritative
    double-launch guard is ``engine.run()``'s atomic IDLE→RUNNING transition,
    whose RuntimeError is caught in the spawned thread (see ``_drive_run``).
    The run-generation mechanism needs no extra work here: ``run()`` captures
    ``pool._run_generation`` at entry and self-aborts if it has advanced.

    Args:
        pool: The AgentPool managing instances.
        instance_name: Name of the instance to relaunch.

    Returns:
        True if a launch thread was spawned, False otherwise (no-op).
    """
    if pool is None or not instance_name:
        return False

    # Stop/termination guards — a queued message during shutdown must not
    # revive a dead agent.
    try:
        if getattr(pool, 'stopped', False):
            logger.debug(
                f"[WAKEUP_RELAUNCH] Skipping relaunch of '{instance_name}': pool is stopped"
            )
            return False
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[WAKEUP_RELAUNCH] stopped-check failed for '{instance_name}' (non-critical): {e}")

    if hasattr(pool, 'is_instance_terminated'):
        try:
            if pool.is_instance_terminated(instance_name):
                logger.debug(
                    f"[WAKEUP_RELAUNCH] Skipping relaunch of '{instance_name}': instance is terminated"
                )
                return False
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[WAKEUP_RELAUNCH] termination-check failed for '{instance_name}' (non-critical): {e}")

    inst = pool.get_instance(instance_name) if hasattr(pool, 'get_instance') else None
    if inst is None:
        return False

    # State gate: only IDLE needs a relaunch. SLEEPING/RUNNING/COMPLETING have
    # a live thread that will drain the queued message itself; TERMINATED must
    # stay dead. Read under _state_lock and re-check right before spawning to
    # close the TOCTOU gap (the L1 guard in run() remains the authoritative
    # double-launch protection).
    try:
        with inst._state_lock:
            if inst.state.name != 'IDLE':
                return False
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[WAKEUP_RELAUNCH] state-check failed for '{instance_name}' (non-critical): {e}")
        return False

    try:
        t = threading.Thread(
            target=_drive_run,
            args=(pool, instance_name),
            name=f"wakeup-relaunch-{instance_name}",
            daemon=True,
        )
        t.start()
    except Exception as e:
        logger.error(f"[WAKEUP_RELAUNCH] Failed to spawn relaunch thread for '{instance_name}': {e}")
        return False

    logger.debug(f"[WAKEUP_RELAUNCH] Spawned relaunch thread for IDLE instance '{instance_name}'")
    return True

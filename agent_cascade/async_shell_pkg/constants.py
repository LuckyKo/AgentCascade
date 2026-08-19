"""Named timing constants for the async shell tracker (moved verbatim from async_shell.py).

Phase 3c pure-move refactor. ``KILL_WAIT_TIMEOUT`` is patched by tests via
``agent_cascade.async_shell_pkg.constants.KILL_WAIT_TIMEOUT``; ``tracker.py`` reads it
through module-attribute access so the patch takes effect.
"""

PROCESS_KILL_SETTLE_DELAY = 0.3     # Allow Windows console to process kill/Ctrl+C signal
DRAIN_THREAD_FLUSH_DELAY = 0.2     # Allow drain threads to flush remaining output after kill
LAUNCH_POLL_INTERVAL = 0.05        # Brief interval between launch completion checks
VIEWER_EXIT_WAIT_TIMEOUT = 1.5     # Allow viewer to flush final output before force-killing on normal completion
KILL_WAIT_TIMEOUT = 5.0            # Give tracking thread time to detect kill flag and terminate process

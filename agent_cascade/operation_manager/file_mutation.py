"""Process-global lock guarding file-mutation commit regions.

edit_file and write_file each contain a small "commit region": the window between
re-resolving the target path and writing it (re-read + staleness guard + backup +
write + ownership record). Two agent threads (or a thread and a worker) can race
through that window on the same file, so it is serialized by one process-global
mutex. The lock is held only for the commit region — never across the unbounded
``request_user_approval`` wait, which stays outside so a parked approval cannot pin
the mutex and stall a second writer.
"""

import threading
from contextlib import contextmanager


class FileMutationQueue:
    """A coarse process-global mutex over file-mutation commit regions.

    One lock guards the whole critical section; it deliberately does not key on the
    resolved path. Cross-file parallelism would only matter inside a short commit
    window, which is negligible in an LLM-turn-dominated system — the section's I/O is
    the diff-build, backup copy and write themselves, so its length grows with file
    size, and a single lock is still the simplest safe design. A keyed ``path -> Lock``
    map is the documented upgrade path for a future multi-file transaction
    (``copy_file``/``move_file``) that needs several files mutated atomically.

    Process-scope caveat: a threading mutex serializes cooperating threads within one
    process only. A second AC instance or an external editor touching the same tree is
    not covered by the lock; the callers' staleness guards (edit_file re-reads and
    compares raw bytes, write_file re-stats mtime+size) are the defense against that.
    """

    def __init__(self):
        self._lock = threading.Lock()

    @contextmanager
    def mutate(self):
        """Guard a commit region: held on entry, released on every exit path.

        The lock is released in ``finally`` so an aborted edit (guard hit, backup or
        write error, or a returned error string) never leaves the mutex pinned.
        """
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()


# One shared instance for the whole process, mirroring how OperationManager itself is
# a single shared instance passed around rather than re-created per call.
_QUEUE = FileMutationQueue()


def mutate():
    """Return a context manager guarding a file-mutation commit region.

    Usage: ``with file_mutation.mutate():`` around re-resolve → guard → write → own.
    """
    return _QUEUE.mutate()

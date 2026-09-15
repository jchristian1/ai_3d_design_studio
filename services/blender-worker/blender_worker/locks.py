"""Project-scoped locking.

Spec 001, Task 6.

A lock guarantees that two worker executions never mutate the same project's
.blend concurrently. Locks are per project_id, so unrelated projects never block
each other.

``ProjectLockProvider`` is the abstraction the worker depends on.
``FileLockProvider`` is the local implementation using ``fcntl.flock``. All
Linux-specific code lives in this module — WorkerExecutor never imports fcntl —
so a distributed lock (Redis, etcd, a database advisory lock) can be substituted
later by providing another provider.

Limitation, stated plainly: an flock is confined to one machine. It is correct for
the single-worker local milestone and will NOT coordinate multiple worker hosts.
That is a deliberate scope boundary, not an oversight.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol


class LockConflictError(RuntimeError):
    """Another holder owns the project lock."""


class ProjectLockProvider(Protocol):
    """Hands out project-scoped mutual exclusion."""

    @contextmanager
    def hold(self, project_id: str, timeout: float = 0.0) -> Iterator[None]:
        """Hold the lock for a project, releasing it on exit.

        Raises LockConflictError if it cannot be acquired within ``timeout``.
        """
        ...


def _safe_segment(value: str) -> str:
    if not value or not value.strip() or value != Path(value).name:
        raise ValueError(f"project_id {value!r} is not a safe path segment")
    return value


class FileLockProvider:
    """Local advisory locking via ``fcntl.flock``.

    One lock file per project under ``<root>/locks/<project_id>.lock``. The lock
    is released when the file description is closed, so a crashed process cannot
    leave a project permanently locked — the kernel cleans up.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def lock_path(self, project_id: str) -> Path:
        return self.root / "locks" / f"{_safe_segment(project_id)}.lock"

    @contextmanager
    def hold(self, project_id: str, timeout: float = 0.0) -> Iterator[None]:
        path = self.lock_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockConflictError(
                            f"project {project_id!r} is locked by another execution"
                        ) from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

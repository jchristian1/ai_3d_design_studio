"""Noticing that this worker is running stale code, and doing something about it.

WHY THIS EXISTS

A Python process holds the code it imported at startup, forever. That is normally
unremarkable, but this worker is the thing that executes design changes, and it sits
behind a browser. So when its source changes, every symptom of the stale process looks
like a product bug instead of a stale process:

* new materials are missing from the library, so the agent truthfully reports that the
  material the user asked for does not exist;
* a fix to the risk classifier has not landed, so the user is asked to approve the same
  harmless operation again;
* a change to the preview renderer does nothing at all.

That failure mode cost three rounds of "it changed but it looks the same", with a
correct implementation sitting on disk the whole time. Nothing in the running system
could report the one fact that mattered: *you are not running this code*.

WHAT IT DOES

The worker fingerprints its own source at startup and re-checks between messages.
When the fingerprint changes it re-executes itself, but only while IDLE — a reload
must never interrupt a mutation that is holding a project lock. It also honours an
explicit request file, so the browser can ask for a reload without waiting for the
watcher and without anyone finding a terminal.

WHY THE WORKER WATCHES ITSELF

The alternative is for the control plane to compare code versions and push a restart,
which assumes it can see the worker's files. It cannot: a worker is a separate machine
by design, connected outbound, and the whole point of that boundary is that the control
plane knows nothing about its filesystem. The worker is the only party that can
honestly answer "has my code changed", so it is the party that answers it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from studio_contracts.worker_control import (
    reload_request_path,
    take_worker_reload_request,
)

logger = logging.getLogger("blender_worker.reload")

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Source trees whose contents change what this worker DOES. Kept explicit rather than
#: fingerprinting the whole repository: the web app and the test suite change constantly
#: and none of it reaches this process, so including them would mean restarting for
#: nothing several times an hour.
WATCHED_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services" / "blender-worker" / "blender_worker",
    REPO_ROOT / "services" / "preview" / "studio_preview",
    REPO_ROOT / "services" / "blender-mcp" / "blender_mcp",
    REPO_ROOT / "packages" / "materials" / "python" / "studio_materials",
    REPO_ROOT / "packages" / "validation" / "python" / "studio_validation",
    REPO_ROOT / "packages" / "contracts" / "python" / "studio_contracts",
    REPO_ROOT / "packages" / "types" / "python" / "studio_types",
    REPO_ROOT / "packages" / "spatial" / "python" / "studio_spatial",
)

#: Set to "0" to keep a worker on its current code no matter what changes on disk.
ENV_AUTO_RELOAD = "STUDIO_WORKER_AUTO_RELOAD"


def _source_files(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in paths:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            # __pycache__ mirrors the sources it was compiled from, so including it
            # would double the work and add nothing.
            if "__pycache__" in path.parts:
                continue
            found.append(path)
    return sorted(found)


def source_fingerprint(paths: Iterable[Path] = WATCHED_PATHS) -> str:
    """Identity of the code on disk right now.

    Built from each file's path, size and modification time rather than its contents:
    stat is enough to notice an edit, and hashing every byte of the source tree on a
    two-second loop would be pointless work. The consequence — an edit that preserves
    both size and mtime goes unnoticed — is not reachable by saving a file.
    """
    digest = hashlib.sha256()
    for path in _source_files(paths):
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - file vanished mid-scan
            continue
        # Relative to the checkout where possible, so the digest does not depend on
        # where the repository happens to live. A watched path outside it — which a
        # test fixture legitimately is — falls back to the absolute path rather than
        # raising, because a diagnostic must not be the thing that crashes.
        try:
            key = str(path.relative_to(REPO_ROOT))
        except ValueError:
            key = str(path)
        digest.update(key.encode("utf-8"))
        digest.update(str(stat.st_size).encode("utf-8"))
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))
    return digest.hexdigest()


def short_fingerprint(fingerprint: Optional[str] = None) -> str:
    """A short, log-friendly and wire-friendly form of the fingerprint."""
    return (fingerprint or source_fingerprint())[:12]


class ReloadWatcher:
    """Decides when this process should restart itself, and does it.

    Deliberately passive: it never restarts on its own schedule. The caller asks
    between messages, at a point where it knows no job is running, which is what makes
    "never interrupt a mutation" a property of the design rather than a hope.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        auto_reload: Optional[bool] = None,
        paths: Iterable[Path] = WATCHED_PATHS,
    ) -> None:
        self.paths = tuple(paths)
        self.runtime_root = Path(runtime_root)
        self.loaded_fingerprint = source_fingerprint(self.paths)
        if auto_reload is None:
            auto_reload = os.environ.get(ENV_AUTO_RELOAD, "1").strip() not in {
                "0",
                "false",
                "no",
            }
        self.auto_reload = bool(auto_reload)

    # -- state -------------------------------------------------------------
    @property
    def request_path(self) -> Path:
        return reload_request_path(self.runtime_root)

    def code_changed(self) -> bool:
        """True when the source on disk differs from what this process imported."""
        return source_fingerprint(self.paths) != self.loaded_fingerprint

    def take_request(self) -> bool:
        """True when a reload was explicitly requested; consumes the request."""
        return take_worker_reload_request(self.runtime_root)

    # -- action ------------------------------------------------------------
    def should_restart(self) -> Optional[str]:
        """The reason to restart now, or ``None``. Safe to call every loop pass."""
        if self.take_request():
            return "a reload was requested from the studio"
        if self.auto_reload and self.code_changed():
            return "the worker's code changed on disk"
        return None

    def restart(self, reason: str) -> None:
        """Replace this process with a fresh one running the current code.

        ``execv`` rather than spawn-and-exit: the process keeps its identity, its
        parent, and its place in the supervisor's process group, so the shell script
        that started the studio still controls it and still stops it with everything
        else. A child would outlive that group and become an orphan holding the
        worker's token.
        """
        logger.warning("restarting: %s", reason)
        # Flush, because execv does not run interpreter shutdown and buffered log
        # lines explaining the restart would be lost exactly when they are wanted.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # pragma: no cover - a closed stream must not block a restart
                pass
        os.execv(sys.executable, [sys.executable, "-m", "blender_worker"])


__all__ = [
    "ENV_AUTO_RELOAD",
    "REPO_ROOT",
    "ReloadWatcher",
    "WATCHED_PATHS",
    "short_fingerprint",
    "source_fingerprint",
]

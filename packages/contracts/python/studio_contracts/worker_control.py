"""Out-of-band control conventions shared by the control plane and a local worker.

Everything else the two exchange travels over the authenticated worker link, whose
vocabulary is a closed enum in ``worker-message.schema.json`` validated at both ends.
This module is for the one thing that deliberately does NOT: asking a worker running on
this machine to reload its own code.

WHY NOT A PROTOCOL MESSAGE

Adding a message type means bumping the wire protocol version, because every message is
validated with ``additionalProperties: false`` and a peer that predates a new type
rejects it. That is the right cost for a product capability and the wrong cost for
developer tooling. A reload request is tooling: it exists because a long-running Python
process holds the code it imported at startup, which is a property of Python, not of the
product.

WHY IT LIVES HERE

The control plane may not import the worker's package — a test enforces that, because the
control plane must stay unable to see Blender — and the worker should not duplicate a
filename the control plane writes. A shared contracts package is the one place both may
depend on, and this IS a contract: two processes agreeing on a file.

SCOPE, HONESTLY

This only works when both sides share a filesystem, which means local development. A
worker on another machine is reached exclusively over the link, and asking it to reload
is not something this mechanism can or should do — for a remote worker, reloading is a
deployment concern.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Optional

#: Environment variable naming the worker's runtime directory. Read identically by both
#: sides so they resolve the same location without either importing the other.
ENV_WORKER_RUNTIME_ROOT: Final = "STUDIO_WORKER_RUNTIME_ROOT"

#: Repository root, derived from this file:
#: packages/contracts/python/studio_contracts/worker_control.py -> four levels up.
_REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: Default runtime directory, matching ``blender_worker.runtime.DEFAULT_RUNTIME_ROOT``.
#: Gitignored, so a fresh checkout needs no configuration.
DEFAULT_WORKER_RUNTIME_ROOT: Final = _REPO_ROOT / "runtime" / "worker"

#: The file a reload request is written to. Created by whoever asks, deleted by the
#: worker when it acts, so a request can never fire twice.
RELOAD_REQUEST_FILENAME: Final = "reload.request"


def worker_runtime_root(configured: Optional[str] = None) -> Path:
    """Resolve the worker's runtime directory the same way on both sides."""
    value = configured if configured is not None else os.environ.get(ENV_WORKER_RUNTIME_ROOT)
    return Path(value) if value else DEFAULT_WORKER_RUNTIME_ROOT


def reload_request_path(runtime_root: Optional[Path] = None) -> Path:
    """Where a reload request is written."""
    return (Path(runtime_root) if runtime_root else worker_runtime_root()) / (
        RELOAD_REQUEST_FILENAME
    )


def request_worker_reload(runtime_root: Optional[Path] = None) -> Path:
    """Write a reload request, and return the file written."""
    path = reload_request_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reload\n", "utf-8")
    return path


def take_worker_reload_request(runtime_root: Optional[Path] = None) -> bool:
    """True when a reload was requested; consumes the request.

    Consuming here rather than after a successful restart is deliberate: a request that
    survived a failed reload would restart the worker forever.
    """
    path = reload_request_path(runtime_root)
    try:
        if not path.is_file():
            return False
        path.unlink()
    except OSError:  # pragma: no cover - racing another reader
        return False
    return True


__all__ = [
    "DEFAULT_WORKER_RUNTIME_ROOT",
    "ENV_WORKER_RUNTIME_ROOT",
    "RELOAD_REQUEST_FILENAME",
    "reload_request_path",
    "request_worker_reload",
    "take_worker_reload_request",
    "worker_runtime_root",
]

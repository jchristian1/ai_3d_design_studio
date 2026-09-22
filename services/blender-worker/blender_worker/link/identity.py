"""Worker identity and link configuration.

Spec 001, Task 8.

Identity and credentials come from the environment, never from source. Nothing
workstation-specific is hard-coded: a different machine sets different environment
variables and needs no code change.

    STUDIO_WORKER_ID       stable worker identity      (required)
    STUDIO_WORKER_TOKEN    pre-shared token            (required, never committed)
    STUDIO_CONTROL_PLANE_URL  e.g. ws://127.0.0.1:8765/ws/workers
    STUDIO_WORKER_GPU_NAME    optional coarse GPU description

The token is read here and handed to the transport at connect time. It is never
written to the journal, never placed in a result, and never logged — the protocol
codec redacts it and a schema conditional forbids it on any message except
``worker_hello``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from blender_mcp.blender_runtime import blender_version, find_blender_executable

#: Version of the worker software (not of the host).
WORKER_SOFTWARE_VERSION = "0.1.0"

ENV_WORKER_ID = "STUDIO_WORKER_ID"
ENV_WORKER_TOKEN = "STUDIO_WORKER_TOKEN"
ENV_CONTROL_PLANE_URL = "STUDIO_CONTROL_PLANE_URL"
ENV_GPU_NAME = "STUDIO_WORKER_GPU_NAME"

DEFAULT_CONTROL_PLANE_URL = "ws://127.0.0.1:8765/ws/workers"


class WorkerIdentityError(RuntimeError):
    """Required worker identity or credentials are missing."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Who this worker is, and how it reaches the control plane.

    ``token`` is deliberately excluded from ``__repr__`` so it cannot reach a log
    through casual string formatting or an exception traceback.
    """

    worker_id: str
    control_plane_url: str
    token: str = field(repr=False)
    gpu_name: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"WorkerIdentity(worker_id={self.worker_id!r})"


def load_worker_identity(
    env: Optional[dict[str, str]] = None,
) -> WorkerIdentity:
    """Read identity from the environment.

    Raises WorkerIdentityError when a required value is missing, rather than
    inventing an identity — an unidentified worker must not connect.
    """
    source = dict(os.environ if env is None else env)

    worker_id = (source.get(ENV_WORKER_ID) or "").strip()
    token = source.get(ENV_WORKER_TOKEN) or ""
    url = (source.get(ENV_CONTROL_PLANE_URL) or DEFAULT_CONTROL_PLANE_URL).strip()

    if not worker_id:
        raise WorkerIdentityError(f"{ENV_WORKER_ID} is not set")
    if not token.strip():
        raise WorkerIdentityError(f"{ENV_WORKER_TOKEN} is not set")

    gpu_name = (source.get(ENV_GPU_NAME) or "").strip() or None
    return WorkerIdentity(
        worker_id=worker_id,
        control_plane_url=url,
        token=token,
        gpu_name=gpu_name,
    )


def describe_capabilities(
    identity: Optional[WorkerIdentity] = None,
    supported_job_types: tuple[str, ...] = ("move_object",),
    max_concurrent_jobs: int = 1,
    probe_blender: bool = True,
) -> dict[str, object]:
    """Build the capabilities advertised at registration.

    Only coarse, non-revealing facts. Deliberately absent: filesystem paths, the
    home directory, environment variables, project locations, and the token. The
    canonical ``worker-capabilities.schema.json`` closes the object, so adding a
    revealing field would fail validation.
    """
    def _code_fingerprint() -> str:
        """Short digest of the worker's own source, or ``unknown``.

        Imported lazily and guarded: a worker must still be able to register when the
        source tree cannot be scanned, because failing to connect over a diagnostic is
        far worse than not having the diagnostic.
        """
        try:
            from ..reload import short_fingerprint

            return short_fingerprint()
        except Exception:  # pragma: no cover - diagnostics must never block registration
            return "unknown"

    available = False
    version: Optional[str] = None
    if probe_blender:
        available = find_blender_executable() is not None
        if available:
            version = blender_version()

    capabilities: dict[str, object] = {
        # The version carries the identity of the CODE this process actually imported,
        # not just the release number. That is what lets the control plane notice a
        # worker running yesterday's build and offer to reload it, and it fits in the
        # existing field rather than requiring a new one — the capabilities schema is
        # deliberately closed, and a wire-vocabulary change for developer tooling would
        # be a poor trade. The digest is derived from source mtimes and sizes, so it
        # reveals nothing about the filesystem.
        "worker_version": f"{WORKER_SOFTWARE_VERSION}+code.{_code_fingerprint()}",
        "blender_available": available,
        "supported_job_types": list(supported_job_types),
        "max_concurrent_jobs": max_concurrent_jobs,
    }
    if version:
        capabilities["blender_version"] = version

    gpu_name = identity.gpu_name if identity else None
    if not gpu_name:
        gpu_name = _detect_gpu_name()
    capabilities["gpu_available"] = bool(gpu_name)
    if gpu_name:
        capabilities["gpu_name"] = gpu_name
    return capabilities


def _detect_gpu_name() -> Optional[str]:
    """Best-effort NVIDIA GPU name via ``nvidia-smi``.

    ``STUDIO_WORKER_GPU_NAME`` always takes precedence; this only fills the gap when the
    operator did not set it, so the studio can show the real accelerator instead of
    "no GPU" on a machine that clearly has one. Kept coarse (the model name only) to match
    the non-revealing spirit of the capabilities object, and entirely best-effort: any
    failure — no nvidia-smi, no driver, a timeout — simply yields ``None``.
    """
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    first = (completed.stdout or "").splitlines()
    name = first[0].strip() if first else ""
    return name or None

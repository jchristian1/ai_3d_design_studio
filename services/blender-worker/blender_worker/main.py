"""The Blender Worker process.

Spec 001. Added in Task 11, because the browser experience cannot be demonstrated
without a worker that can actually be STARTED — every earlier task drove the worker
from test code.

    python -m blender_worker

    Blender workstation                        Control plane
    ┌──────────────────────┐                  ┌──────────────┐
    │ this process         │ ── outbound ───> │ /ws/workers  │
    │  WorkerLinkClient    │      WS          └──────────────┘
    │  WorkerExecutor      │
    │  Blender subprocess  │
    │  PreviewGenerator    │
    └──────────────────────┘

It composes existing pieces and adds no new behaviour: the connection lifecycle is
``WorkerLinkClient`` (Task 8), execution is ``WorkerExecutor`` (Task 6), previews are
``BlenderPreviewGenerator`` + ``LocalArtifactStore`` (Task 10). This module only
wires them from environment configuration and runs the loop.

Configuration (all from the environment; nothing workstation-specific in source):

    STUDIO_WORKER_ID              stable worker identity            (required)
    STUDIO_WORKER_TOKEN           pre-shared token                  (required)
    STUDIO_CONTROL_PLANE_URL      ws://127.0.0.1:8000/ws/workers
    STUDIO_WORKER_GPU_NAME        optional coarse GPU description
    STUDIO_WORKER_PROJECTS_ROOT   directory holding project files
    STUDIO_WORKER_RUNTIME_ROOT    journal, locks, recovery snapshots
    STUDIO_WORKER_ARTIFACT_ROOT   generated preview artifacts

PROJECT RESOLUTION IS THE WORKER'S OWN DECISION
-----------------------------------------------
``project_id`` maps to ``<projects root>/<project_id>.blend`` by convention, through
``MappingProjectRegistry``, which validates that the id is a safe single segment and
that the resolved path stays inside the allowed root. A Job cannot carry a path (the
canonical schema has no path field), and this process never accepts one from the
network — the mapping is built at startup from local configuration only.

Only projects that actually exist on disk are registered, so a request for an
unknown project fails cleanly instead of the worker inventing a file.

THE LOOP
--------
One job at a time, deliberately. ``max_concurrent_jobs`` is 1 and the executor takes
a per-project lock, so two mutations can never interleave on one project. The loop
alternates a bounded ``handle_next`` with a heartbeat, and reconnects with bounded
backoff; every reconnect re-authenticates and reconciles undelivered results from the
durable journal, so a dropped result is reported rather than re-executed.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .blender_ops import SubprocessBlenderOperationExecutor
from .executor import WorkerExecutor
from .journal import FileSystemExecutionStore
from .link.client import READY, BackoffPolicy, WorkerLinkClient
from .link.identity import WorkerIdentityError, load_worker_identity
from .link.websocket_transport import WebSocketWorkerTransport
from .locks import FileLockProvider
from .registry import MappingProjectRegistry
from .runtime import DEFAULT_RUNTIME_ROOT

logger = logging.getLogger("blender_worker")

ENV_PROJECTS_ROOT = "STUDIO_WORKER_PROJECTS_ROOT"
ENV_RUNTIME_ROOT = "STUDIO_WORKER_RUNTIME_ROOT"
ENV_ARTIFACT_ROOT = "STUDIO_WORKER_ARTIFACT_ROOT"
ENV_LOG_LEVEL = "STUDIO_WORKER_LOG_LEVEL"

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Git-ignored default locations, so a fresh checkout works with no configuration.
DEFAULT_PROJECTS_ROOT = REPO_ROOT / "runtime" / "projects"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "runtime" / "artifacts"

#: How long to block waiting for a job offer before sending a heartbeat. Short
#: enough that liveness stays current, long enough that the loop is not a spin.
RECEIVE_TIMEOUT_SECONDS = 2.0

PROJECT_SUFFIX = ".blend"


class _Stopping:
    """Cooperative shutdown flag set by SIGINT/SIGTERM.

    A worker must not be killed mid-mutation if it can be helped: the flag is
    checked between messages, so Ctrl-C after a job has started lets that job finish
    saving and reporting rather than abandoning it.
    """

    def __init__(self) -> None:
        self.requested = False

    def request(self, *_: object) -> None:
        if not self.requested:
            logger.info("shutdown requested; finishing the current message first")
        self.requested = True


def discover_projects(projects_root: Path) -> dict[str, Path]:
    """Map project ids to project files found in the trusted root.

    Convention: ``<root>/<project_id>.blend``. Only existing files are registered,
    so an unknown project_id is refused rather than guessed at.
    """
    if not projects_root.exists():
        return {}
    return {
        path.stem: path
        for path in sorted(projects_root.glob(f"*{PROJECT_SUFFIX}"))
        if path.is_file()
    }


def build_executor(
    projects_root: Path,
    runtime_root: Path,
    artifact_root: Path,
) -> WorkerExecutor:
    """Compose the executor from local configuration."""
    from studio_preview.artifacts import LocalArtifactStore
    from studio_preview.blender_preview import BlenderPreviewGenerator

    projects = discover_projects(projects_root)
    if not projects:
        raise SystemExit(
            f"no project files found in {projects_root}.\n"
            f"Place a .blend there named after its project id, for example "
            f"{projects_root / ('proj_seed' + PROJECT_SUFFIX)}.\n"
            f"For local development:  python -m scripts.bootstrap_local_project"
        )

    logger.info("serving projects: %s", ", ".join(sorted(projects)))

    return WorkerExecutor(
        store=FileSystemExecutionStore(runtime_root),
        locks=FileLockProvider(runtime_root),
        projects=MappingProjectRegistry(projects_root, projects),
        blender=SubprocessBlenderOperationExecutor(),
        recovery_root=runtime_root / "recovery",
        previews=BlenderPreviewGenerator(),
        artifacts=LocalArtifactStore(artifact_root),
    )


def build_client(executor: WorkerExecutor) -> WorkerLinkClient:
    """Build the link client from environment identity."""
    identity = load_worker_identity()
    logger.info(
        "worker %s connecting to %s", identity.worker_id, identity.control_plane_url
    )
    return WorkerLinkClient(
        identity=identity,
        transport=WebSocketWorkerTransport(
            identity.control_plane_url, receive_timeout=RECEIVE_TIMEOUT_SECONDS
        ),
        executor=executor,
        # Probe Blender so the control plane's /health can honestly report whether
        # a Blender-capable worker is connected.
        probe_blender=True,
        backoff=BackoffPolicy(initial_seconds=1.0, multiplier=2.0, max_seconds=30.0),
        sleep=time.sleep,
    )


def register_with_retry(client: WorkerLinkClient, stopping: _Stopping) -> bool:
    """Connect and register, retrying while the control plane is merely absent.

    Startup order between terminals must not matter, and an API restart must not
    require restarting the worker. So a transport failure is retried with the
    client's bounded backoff.

    A REJECTION is different and is not retried: a bad token or an unsupported
    protocol version will fail identically forever, and hammering the control plane
    would only bury the real reason in noise.
    """
    attempt = 0
    while not stopping.requested:
        if client.connect_and_register(timeout=15.0):
            return True

        error = client.last_error or {}
        code = error.get("code")
        if code != "PROVIDER_UNAVAILABLE":
            # Registration was refused on its merits.
            logger.error(
                "the control plane refused this worker: %s",
                error.get("message", "unknown reason"),
            )
            return False

        attempt += 1
        delay = client.backoff.delay_for(attempt)
        logger.warning(
            "control plane not reachable yet; retrying in %.1fs (attempt %d)",
            delay,
            attempt,
        )
        # Sleep in short slices so Ctrl-C stays responsive during a long backoff.
        slept = 0.0
        while slept < delay and not stopping.requested:
            step = min(0.25, delay - slept)
            time.sleep(step)
            slept += step
    return False


def serve(client: WorkerLinkClient, stopping: _Stopping) -> int:
    """Run the worker loop until shutdown is requested."""
    if not register_with_retry(client, stopping):
        return 0 if stopping.requested else 1

    logger.info("registered; waiting for design changes")
    # Report anything the control plane may have missed before this process
    # started. Resending a stored result cannot mutate Blender.
    resent = client.reconcile()
    if resent:
        logger.info("reconciled %d undelivered result(s)", len(resent))

    while not stopping.requested:
        kind = client.handle_next(timeout=RECEIVE_TIMEOUT_SECONDS)

        if client.state not in (READY, "busy"):
            # The link dropped. Reconnecting re-authenticates and reconciles.
            logger.warning("link lost; reconnecting")
            if stopping.requested:
                break
            if not register_with_retry(client, stopping):
                if stopping.requested:
                    break
                logger.error("could not reconnect; giving up")
                return 1
            logger.info("reconnected")
            client.reconcile()
            continue

        if kind is None:
            # Nothing arrived within the timeout: report liveness and keep waiting.
            client.send_heartbeat()

    logger.info("shutting down")
    client.disconnect()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get(ENV_LOG_LEVEL, "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    projects_root = Path(
        os.environ.get(ENV_PROJECTS_ROOT) or DEFAULT_PROJECTS_ROOT
    ).resolve()
    runtime_root = Path(os.environ.get(ENV_RUNTIME_ROOT) or DEFAULT_RUNTIME_ROOT)
    artifact_root = Path(os.environ.get(ENV_ARTIFACT_ROOT) or DEFAULT_ARTIFACT_ROOT)

    try:
        executor = build_executor(projects_root, runtime_root, artifact_root)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        client = build_client(executor)
    except WorkerIdentityError as exc:
        # A worker with no identity must not connect. Say exactly what is missing.
        print(
            f"worker identity is not configured: {exc}\n"
            "Set at least:\n"
            "  export STUDIO_WORKER_ID=worker_local_1\n"
            "  export STUDIO_WORKER_TOKEN=<the same token the API was started with>",
            file=sys.stderr,
        )
        return 2

    stopping = _Stopping()
    signal.signal(signal.SIGINT, stopping.request)
    signal.signal(signal.SIGTERM, stopping.request)

    try:
        return serve(client, stopping)
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        client.disconnect()
        return 0


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "register_with_retry",
    "DEFAULT_PROJECTS_ROOT",
    "ENV_ARTIFACT_ROOT",
    "ENV_PROJECTS_ROOT",
    "ENV_RUNTIME_ROOT",
    "build_client",
    "build_executor",
    "discover_projects",
    "main",
    "serve",
]

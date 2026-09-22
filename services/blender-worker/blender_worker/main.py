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
from typing import Any, Optional

from .blender_ops import SubprocessBlenderOperationExecutor
from .executor import WorkerExecutor
from .journal import FileSystemExecutionStore
from .link.client import READY, BackoffPolicy, WorkerLinkClient
from .link.identity import WorkerIdentityError, load_worker_identity
from .link.websocket_transport import WebSocketWorkerTransport
from .locks import FileLockProvider
from .registry import MappingProjectRegistry
from .reload import ReloadWatcher, short_fingerprint
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
):
    """Compose the executor from local configuration.

    Returns a dispatching executor serving both the Spec 001 ``move_object`` path and
    the ``apply_capabilities`` path that drives Blender through the official MCP.
    """
    from studio_preview.artifacts import LocalArtifactStore
    from studio_preview.blender_preview import BlenderPreviewGenerator

    from .backends.official.backend import OfficialBlenderLabBackend
    from .capability_executor import CapabilityPlanExecutor
    from .dispatch import DispatchingExecutor
    from .provisioning import ProvisioningProjectRegistry

    projects = discover_projects(projects_root)
    if projects:
        logger.info("existing projects: %s", ", ".join(sorted(projects)))
    else:
        logger.info(
            "no project files yet in %s; one will be created when a project is first used",
            projects_root,
        )

    store = FileSystemExecutionStore(runtime_root)
    locks = FileLockProvider(runtime_root)
    # Creates <root>/<project_id>.blend on first use, so a project the user made in the
    # browser needs no manual setup and no worker restart. The control plane decides which
    # projects exist; the worker only locates and creates.
    registry = ProvisioningProjectRegistry(projects_root)
    artifacts = LocalArtifactStore(artifact_root)
    previews = BlenderPreviewGenerator()
    recovery_root = runtime_root / "recovery"

    legacy = WorkerExecutor(
        store=store,
        locks=locks,
        projects=registry,
        blender=SubprocessBlenderOperationExecutor(),
        recovery_root=recovery_root,
        previews=previews,
        artifacts=artifacts,
    )

    # The official MCP server is launched lazily on first use, so a worker still starts
    # (and still serves the Spec 001 path) when the MCP has not been installed yet.
    capabilities = CapabilityPlanExecutor(
        store=store,
        locks=locks,
        projects=registry,
        provider=OfficialBlenderLabBackend(),
        recovery_root=recovery_root,
        previews=previews,
        artifacts=artifacts,
    )

    return DispatchingExecutor(legacy=legacy, capabilities=capabilities)


def build_client(executor: Any) -> WorkerLinkClient:
    """Build the link client from environment identity."""
    identity = load_worker_identity()
    # Advertise what this worker can actually run, so the control plane never offers a
    # job type it would have to reject.
    supported = getattr(executor, "supported_job_types", ("move_object",))
    logger.info(
        "worker %s connecting to %s", identity.worker_id, identity.control_plane_url
    )
    return WorkerLinkClient(
        identity=identity,
        transport=WebSocketWorkerTransport(
            identity.control_plane_url, receive_timeout=RECEIVE_TIMEOUT_SECONDS
        ),
        executor=executor,
        supported_job_types=tuple(supported),
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


def run_worker_loop(
    client: WorkerLinkClient,
    stopping: _Stopping,
    reloader: Optional[Any] = None,
) -> int:
    """Run the worker's inbound loop until shutdown is requested.

    Named a LOOP, not a server: this process only ever reads from a connection it
    opened outbound. Nothing here binds or listens, and the name must not suggest
    otherwise — `serve` reads like `serve_forever`, and the workstation's
    outbound-only guarantee is checked by an audit that greps for exactly that.
    """
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

            # The one safe moment to replace this process. Reaching here means the
            # last receive timed out with the link healthy, so no job is in flight and
            # no project lock is held — a restart cannot abandon a half-saved
            # mutation. Checked only here for exactly that reason.
            if reloader is not None:
                reason = reloader.should_restart()
                if reason:
                    client.disconnect()
                    reloader.restart(reason)

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

    reloader = ReloadWatcher(runtime_root)
    logger.info(
        "running code %s; auto-reload %s",
        short_fingerprint(reloader.loaded_fingerprint),
        "on" if reloader.auto_reload else "off",
    )
    # A request left over from a previous process would restart this brand-new one
    # immediately, so it is cleared before the loop rather than acted on.
    reloader.take_request()

    try:
        return run_worker_loop(client, stopping, reloader)
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
    "run_worker_loop",
]

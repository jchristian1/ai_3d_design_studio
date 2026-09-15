"""The complete local vertical slice, assembled once.

Spec 001, Task 12. TEST TOOLING — not part of any shipped service.

Every real-Blender end-to-end test needs the same stack: a control plane on a
loopback port, a Blender worker connected outbound to it, an isolated copy of the
Task 5 seed project, and an artifact store the two share. This module builds it in
one place so the E2E tests differ only in what they ASSERT.

    ControlPlaneServer (uvicorn + FastAPI)
            ^  real WebSocket
    WorkerLinkClient  ->  WorkerExecutor  ->  Blender  ->  .blend
                                          ->  BlenderPreviewGenerator -> PNG
                                                        ->  LocalArtifactStore

Nothing here is new machinery: it composes the Task 5–11 pieces
(`ControlPlaneServer`, `WorkerLinkClient`, `WorkerExecutor`, `FileLockProvider`,
`MappingProjectRegistry`, `BlenderPreviewGenerator`, `LocalArtifactStore`) and adds
only the waiting helpers a test needs.

The worker's inbound loop is pumped by the calling test rather than by a background
thread. That is deliberate: it keeps every test deterministic — a job is processed
exactly when the test says so — and it is the same code path
`python -m blender_worker` runs, just driven synchronously.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from blender_mcp.blender_runtime import find_blender_executable
from blender_worker.blender_ops import SubprocessBlenderOperationExecutor
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.link.client import BackoffPolicy, WorkerLinkClient
from blender_worker.link.identity import WorkerIdentity
from blender_worker.link.websocket_transport import WebSocketWorkerTransport
from blender_worker.locks import FileLockProvider, ProjectLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_api.settings import Settings
from studio_contracts import worker_protocol as protocol
from studio_preview.artifacts import LocalArtifactStore
from studio_preview.blender_preview import BlenderPreviewGenerator

from .control_plane import ControlPlaneServer
from .seed_project import ensure_seed_project, inspect_blend

DEFAULT_PROJECT_ID = "proj_seed"
DEFAULT_WORKER_ID = "worker_slice_1"

#: Never committed. Each stack invents its own, so no test depends on a shared value.
DEFAULT_TOKEN = "slice-token-not-committed"

MOVE_COMMAND = "Move Cube 50 cm to the right."

#: Small preview, so a full E2E run stays quick.
PREVIEW_WIDTH = 320
PREVIEW_HEIGHT = 180

#: Blender is slow to start; every wait is bounded so a hang fails visibly rather
#: than stalling a suite.
BLENDER_DEADLINE_SECONDS = 300.0


@dataclass
class LocalSliceStack:
    """A running control plane plus a connected Blender worker."""

    server: ControlPlaneServer
    worker: WorkerLinkClient
    executor: WorkerExecutor
    locks: ProjectLockProvider
    artifacts: LocalArtifactStore
    project_path: Path
    project_id: str
    http: Any
    token: str
    runtime_root: Path
    _offers_handled: int = field(default=0, init=False)

    # -- driving the worker ------------------------------------------------

    def pump_until(
        self,
        expected: str = protocol.JOB_OFFER,
        deadline_seconds: float = BLENDER_DEADLINE_SECONDS,
        poll: float = 1.0,
    ) -> bool:
        """Process inbound worker messages until `expected` is handled.

        Bounded by wall-clock time: a deadlock must fail fast and visibly.
        """
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            if self.worker.handle_next(timeout=poll) == expected:
                self._offers_handled += 1
                return True
        return False

    # -- HTTP conveniences -------------------------------------------------

    def submit(self, request_id: str, message: str = MOVE_COMMAND, **overrides: Any):
        """POST /api/chat with a browser-shaped body."""
        body = {
            "request_id": request_id,
            "project_id": self.project_id,
            "session_id": "sess_slice",
            "message": message,
        }
        body.update(overrides)
        return self.http.post("/api/chat", json=body)

    def job_path(self, job_id: str, project_id: Optional[str] = None) -> str:
        return f"/api/projects/{project_id or self.project_id}/jobs/{job_id}"

    def job_status(self, job_id: str, project_id: Optional[str] = None) -> dict:
        response = self.http.get(self.job_path(job_id, project_id))
        assert response.status_code == 200, response.text
        return response.json()

    def await_status(
        self,
        job_id: str,
        expected: str,
        deadline_seconds: float = BLENDER_DEADLINE_SECONDS,
    ) -> dict:
        """Poll the project-scoped job route until it reaches a status."""
        end = time.monotonic() + deadline_seconds
        last: Optional[dict] = None
        while time.monotonic() < end:
            response = self.http.get(self.job_path(job_id))
            if response.status_code == 200:
                last = response.json()
                if last["job_status"] == expected:
                    return last
            time.sleep(0.1)
        raise AssertionError(
            f"job {job_id} never reached {expected!r}; last seen: {last}"
        )

    def await_worker_registered(self, deadline_seconds: float = 30.0) -> None:
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            if self.http.get("/api/workers").json()["registered_workers"] == 1:
                return
            time.sleep(0.05)
        raise AssertionError("the worker never registered with the control plane")

    # -- verification against the SAVED project ---------------------------

    def saved_cube_x(self) -> float:
        """Cube's world X, read from the saved .blend by a FRESH Blender process.

        Reading through an independent process is what makes an assertion about
        durability rather than about worker memory.
        """
        return self.saved_object_x("Cube")

    def saved_object_x(self, name: str) -> float:
        for entry in self.inspect()["digest"]["objects"]:
            if entry["name"] == name:
                return float(entry["world_position_meters"]["x"])
        raise AssertionError(f"no object named {name!r} in the saved project")

    def inspect(self) -> dict:
        """Open the saved .blend in a fresh Blender and return its scene digest."""
        return inspect_blend(self.project_path)

    def saved_object_names(self) -> list[str]:
        return sorted(entry["name"] for entry in self.inspect()["digest"]["objects"])

    # -- artifacts ---------------------------------------------------------

    def stored_artifacts(self) -> tuple:
        return self.artifacts.list_for_project(self.project_id)

    def mutation_count(self) -> int:
        """How many Blender mutations the journal recorded as completed."""
        from blender_worker import phases

        completed = 0
        for record in self.executor.store.undelivered_results():
            if record.phase == phases.COMPLETED:
                completed += 1
        return completed


def build_slice_stack(
    tmp_path: Path,
    project_id: str = DEFAULT_PROJECT_ID,
    token: str = DEFAULT_TOKEN,
    worker_id: str = DEFAULT_WORKER_ID,
    blender: Optional[Any] = None,
    previews: Optional[Any] = None,
    connect: bool = True,
    allowed_origins: tuple[str, ...] = (),
):
    """Build and start the full local slice. Yields a ``LocalSliceStack``.

    Use as a context manager so the server and worker are always torn down::

        with build_slice_stack(tmp_path) as stack:
            ...

    ``blender`` and ``previews`` may be substituted to exercise a failure mode
    through the real ``BlenderOperationExecutor`` / ``PreviewGenerator`` boundaries.
    ``allowed_origins`` configures CORS for tests that drive the API as a browser
    would.
    """
    from contextlib import contextmanager

    @contextmanager
    def _stack():
        if find_blender_executable() is None:
            raise RuntimeError("Blender executable not found")

        # An ISOLATED copy of the Task 5 fixture: the canonical file is never
        # mutated, so every run starts from Cube X = 0.
        projects_root = tmp_path / "projects"
        projects_root.mkdir(exist_ok=True)
        project_path = projects_root / "seed_project.blend"
        shutil.copy2(ensure_seed_project(), project_path)

        runtime_root = tmp_path / "runtime"
        artifact_root = tmp_path / "artifacts"
        artifacts = LocalArtifactStore(artifact_root)
        locks = FileLockProvider(runtime_root)

        executor = WorkerExecutor(
            store=FileSystemExecutionStore(runtime_root),
            locks=locks,
            # The worker owns path resolution; the control plane never sees a path.
            projects=MappingProjectRegistry(projects_root, {project_id: project_path}),
            blender=blender or SubprocessBlenderOperationExecutor(),
            recovery_root=runtime_root / "recovery",
            previews=previews or BlenderPreviewGenerator(),
            artifacts=artifacts,
            preview_width=PREVIEW_WIDTH,
            preview_height=PREVIEW_HEIGHT,
        )

        settings = Settings(
            environment="local",
            worker_token=token,
            project_ids=(project_id,),
            heartbeat_interval_seconds=120.0,
            artifact_root=str(artifact_root),
            allowed_origins=allowed_origins,
        )

        with ControlPlaneServer.build(settings) as server:
            worker = WorkerLinkClient(
                identity=WorkerIdentity(
                    worker_id=worker_id,
                    control_plane_url=server.worker_url,
                    token=token,
                ),
                transport=WebSocketWorkerTransport(
                    server.worker_url, receive_timeout=5.0
                ),
                executor=executor,
                backoff=BackoffPolicy(initial_seconds=0.05, max_seconds=0.5),
                probe_blender=True,
            )
            try:
                with server.client() as http:
                    stack = LocalSliceStack(
                        server=server,
                        worker=worker,
                        executor=executor,
                        locks=locks,
                        artifacts=artifacts,
                        project_path=project_path,
                        project_id=project_id,
                        http=http,
                        token=token,
                        runtime_root=runtime_root,
                    )
                    if connect:
                        assert worker.connect_and_register(timeout=30.0), (
                            f"the worker could not register: {worker.last_error}"
                        )
                        stack.await_worker_registered()
                    yield stack
            finally:
                worker.disconnect()

    return _stack()


__all__ = [
    "BLENDER_DEADLINE_SECONDS",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_TOKEN",
    "DEFAULT_WORKER_ID",
    "MOVE_COMMAND",
    "PREVIEW_HEIGHT",
    "PREVIEW_WIDTH",
    "LocalSliceStack",
    "build_slice_stack",
]

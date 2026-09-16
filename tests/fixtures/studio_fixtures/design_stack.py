"""The whole design studio, assembled offline.

Spec 002. TEST TOOLING — not part of any shipped service.

Everything is the real implementation except the two ends:

    httpx  ->  uvicorn + FastAPI (real routes, real SQLite, real ingest)
                   |
                   |  DesignChatService -> ContextBuilder -> FakeLlmProvider   <- fake #1
                   |  validated proposal -> job -> real WebSocket
                   v
           WorkerLinkClient -> DispatchingExecutor -> CapabilityPlanExecutor
                                                   -> FakeBlenderCapabilityProvider  <- fake #2

Both fakes sit at a Protocol boundary the production code also uses, so what the
tests exercise is the shipping path: real context construction, real argument
validation, real approval gating, real job contracts, real locks and journal, real
scene reporting, real artifact indexing, real HTTP.

The worker's inbound loop is pumped by the calling test rather than by a thread, for
the same reason as the Spec 001 slice: a job is processed exactly when the test says
so, through the same code path ``python -m blender_worker`` runs.
"""

from __future__ import annotations

import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from blender_worker.backends.fake import FakeBlenderCapabilityProvider, FakeObject
from blender_worker.blender_ops import FakeBlenderOperationExecutor
from blender_worker.capability_executor import CapabilityPlanExecutor
from blender_worker.dispatch import DispatchingExecutor
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.link.client import BackoffPolicy, WorkerLinkClient
from blender_worker.link.identity import WorkerIdentity
from blender_worker.link.websocket_transport import WebSocketWorkerTransport
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_agent.providers.fake_llm import FakeLlmProvider
from studio_api.dependencies import build_dependencies
from studio_api.settings import Settings
from studio_api.storage import StudioDatabase
from studio_contracts import worker_protocol as protocol
from studio_preview.artifacts import LocalArtifactStore

from .control_plane import ControlPlaneServer

DEFAULT_PROJECT_ID = "proj_design"
DEFAULT_WORKER_ID = "worker_design_1"
DEFAULT_SESSION_ID = "sess_design"

#: Never committed. Each stack invents its own, so no test depends on a shared value.
DEFAULT_TOKEN = "design-stack-token-not-committed"

#: Nothing here starts a subprocess, so every wait is short. Bounded anyway, so a
#: deadlock fails visibly instead of hanging a suite.
DEADLINE_SECONDS = 30.0


@dataclass
class DesignStack:
    """A running control plane, a connected worker, and both fakes."""

    server: ControlPlaneServer
    worker: WorkerLinkClient
    executor: DispatchingExecutor
    #: The capability backend. ``FakeBlenderCapabilityProvider`` offline; the OFFICIAL
    #: Blender MCP backend in the ``-m mcp`` tier.
    backend: Any
    llm: FakeLlmProvider
    artifacts: LocalArtifactStore
    database: StudioDatabase
    http: Any
    project_id: str
    project_path: Path
    runtime_root: Path
    token: str
    session_id: str = DEFAULT_SESSION_ID
    _pumped: int = field(default=0, init=False)

    # -- the dependency container -----------------------------------------

    @property
    def dependencies(self) -> Any:
        return self.server.dependencies

    @property
    def repositories(self) -> Any:
        return self.server.dependencies.repositories

    # -- driving the worker ------------------------------------------------

    def pump_until(
        self,
        expected: str = protocol.JOB_OFFER,
        deadline_seconds: float = DEADLINE_SECONDS,
        poll: float = 0.5,
    ) -> bool:
        """Handle inbound worker messages until ``expected`` is handled."""
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            if self.worker.handle_next(timeout=poll) == expected:
                self._pumped += 1
                return True
        return False

    # -- HTTP: the browser's surface ---------------------------------------

    def chat(
        self,
        request_id: str,
        message: str,
        *,
        selected_object_id: Optional[str] = None,
        attached_reference_ids: Sequence[str] = (),
        session_id: Optional[str] = None,
    ):
        """POST /api/projects/{id}/design-chat with a browser-shaped body."""
        body: dict[str, Any] = {
            "request_id": request_id,
            "session_id": session_id or self.session_id,
            "message": message,
            "attached_reference_ids": list(attached_reference_ids),
        }
        if selected_object_id is not None:
            body["selected_object_id"] = selected_object_id
        return self.http.post(f"/api/projects/{self.project_id}/design-chat", json=body)

    def decide(self, approval_id: str, *, approved: bool = True):
        return self.http.post(
            f"/api/projects/{self.project_id}/approvals/{approval_id}",
            json={"approved": approved, "session_id": self.session_id},
        )

    def upload(self, filename: str, data: bytes, media_type: str = "application/pdf"):
        return self.http.post(
            f"/api/projects/{self.project_id}/references",
            files={"file": (filename, data, media_type)},
        )

    def workspace(self) -> dict[str, Any]:
        response = self.http.get(f"/api/projects/{self.project_id}/workspace")
        assert response.status_code == 200, response.text
        return response.json()

    def scene(self) -> Optional[dict[str, Any]]:
        response = self.http.get(f"/api/projects/{self.project_id}/scene")
        assert response.status_code == 200, response.text
        return response.json()["scene"]

    def job_status(self, job_id: str) -> dict[str, Any]:
        response = self.http.get(f"/api/projects/{self.project_id}/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def await_status(
        self,
        job_id: str,
        expected: str = "succeeded",
        deadline_seconds: float = DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        end = time.monotonic() + deadline_seconds
        last: Optional[dict[str, Any]] = None
        while time.monotonic() < end:
            last = self.job_status(job_id)
            if last["job_status"] == expected:
                return last
            time.sleep(0.02)
        raise AssertionError(
            f"job {job_id} never reached {expected!r}; last seen: {last}"
        )

    # -- one complete turn -------------------------------------------------

    def run_turn(
        self,
        request_id: str,
        message: str,
        *,
        selected_object_id: Optional[str] = None,
        attached_reference_ids: Sequence[str] = (),
        expected_status: str = "succeeded",
    ) -> dict[str, Any]:
        """Submit a message that is expected to produce a job, and run it.

        Returns the final job status document. Asserts the submission was accepted, so
        a test that means "this should be refused" uses ``chat`` directly instead.
        """
        response = self.chat(
            request_id,
            message,
            selected_object_id=selected_object_id,
            attached_reference_ids=attached_reference_ids,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert job_id, f"no job was created for {message!r}: {response.json()}"
        assert self.pump_until(), "the worker never received the job offer"
        return self.await_status(job_id, expected_status)

    # -- verification against the backend's own state ----------------------

    def object_named(self, name: str) -> FakeObject:
        obj = self.backend.objects.get(name)
        assert obj is not None, (
            f"no object named {name!r}; scene holds {sorted(self.backend.objects)}"
        )
        return obj

    def object_with_id(self, object_id: str) -> FakeObject:
        obj = self.backend.find(object_id=object_id)
        assert obj is not None, (
            f"no object with studio id {object_id!r}; scene holds "
            f"{sorted((o.studio_object_id or o.name) for o in self.backend.objects.values())}"
        )
        return obj

    def cached_object(self, object_id: str) -> Optional[dict[str, Any]]:
        """The object as the CONTROL PLANE last heard about it."""
        snapshot = self.repositories.scenes.get(self.project_id)
        if not snapshot:
            return None
        for entry in snapshot.get("objects") or ():
            if entry.get("studio_object_id") == object_id:
                return entry
        return None

    def invoked_capabilities(self) -> list[str]:
        return [request.capability for request in self.backend.invocations]


def build_design_stack(
    tmp_path: Path,
    *,
    objects: Optional[Mapping[str, FakeObject]] = None,
    project_id: str = DEFAULT_PROJECT_ID,
    token: str = DEFAULT_TOKEN,
    worker_id: str = DEFAULT_WORKER_ID,
    llm: Optional[FakeLlmProvider] = None,
    backend: Optional[Any] = None,
    project_source: Optional[Path] = None,
    connect: bool = True,
) -> Any:
    """Build and start the offline design stack. Use as a context manager::

    with build_design_stack(tmp_path, objects={"Cube": FakeObject(name="Cube")}) as stack:
        ...

    ``backend`` accepts any ``BlenderCapabilityProvider``, which is how the opt-in
    ``-m mcp`` tier runs this same stack against the OFFICIAL Blender MCP and real
    Blender. ``project_source`` is then copied in as the project's starting ``.blend``.
    """

    @contextmanager
    def _stack() -> Iterator[DesignStack]:
        tmp_path.mkdir(parents=True, exist_ok=True)
        projects_root = tmp_path / "projects"
        projects_root.mkdir(exist_ok=True)
        project_path = projects_root / f"{project_id}.blend"
        if project_source is not None:
            shutil.copy2(project_source, project_path)
        else:
            # The capability path never parses the file; it copies it to make a recovery
            # point. Real bytes are the Blender tier's job (tests/mcp, tests/e2e).
            project_path.write_bytes(b"offline design stack placeholder .blend")

        runtime_root = tmp_path / "runtime"
        artifact_root = tmp_path / "artifacts"
        reference_root = tmp_path / "references"

        resolved_backend = backend or FakeBlenderCapabilityProvider(dict(objects or {}))
        resolved_llm = llm or FakeLlmProvider()

        store = FileSystemExecutionStore(runtime_root)
        locks = FileLockProvider(runtime_root)
        registry = MappingProjectRegistry(projects_root, {project_id: project_path})
        artifacts = LocalArtifactStore(artifact_root)

        executor = DispatchingExecutor(
            legacy=WorkerExecutor(
                store=store,
                locks=locks,
                projects=registry,
                blender=FakeBlenderOperationExecutor(),
                recovery_root=runtime_root / "recovery",
                artifacts=artifacts,
            ),
            capabilities=CapabilityPlanExecutor(
                store=store,
                locks=locks,
                projects=registry,
                provider=resolved_backend,
                recovery_root=runtime_root / "recovery",
                artifacts=artifacts,
            ),
        )

        settings = Settings(
            environment="local",
            worker_token=token,
            project_ids=(project_id,),
            heartbeat_interval_seconds=120.0,
            artifact_root=str(artifact_root),
            reference_root=str(reference_root),
            # The design provider is injected below; naming the fake here as well keeps
            # the settings honest about which engine this app is running.
            design_provider="fake_llm",
        )
        # One in-memory database per stack: isolated, and never touching the developer's
        # runtime directory.
        database = StudioDatabase(Path(":memory:"))
        dependencies = build_dependencies(
            settings, design_provider=resolved_llm, database=database
        )

        with ControlPlaneServer.build(settings, dependencies) as server:
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
                backoff=BackoffPolicy(initial_seconds=0.01, max_seconds=0.1),
                # Taken from the executor, exactly as `python -m blender_worker` does:
                # the worker advertises what it can actually run.
                supported_job_types=executor.supported_job_types,
                probe_blender=False,
            )
            try:
                with server.client() as http:
                    stack = DesignStack(
                        server=server,
                        worker=worker,
                        executor=executor,
                        backend=resolved_backend,
                        llm=resolved_llm,
                        artifacts=artifacts,
                        database=database,
                        http=http,
                        project_id=project_id,
                        project_path=project_path,
                        runtime_root=runtime_root,
                        token=token,
                    )
                    if connect:
                        assert worker.connect_and_register(timeout=20.0), (
                            f"the worker could not register: {worker.last_error}"
                        )
                        _await_registered(http)
                    yield stack
            finally:
                worker.disconnect()
                database.close()

    return _stack()


def _await_registered(http: Any, deadline_seconds: float = 20.0) -> None:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if http.get("/api/workers").json()["registered_workers"] == 1:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never registered with the control plane")


__all__ = [
    "DEADLINE_SECONDS",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_SESSION_ID",
    "DEFAULT_TOKEN",
    "DEFAULT_WORKER_ID",
    "DesignStack",
    "FakeObject",
    "build_design_stack",
]

"""The application's dependency container.

Spec 001, Task 9.

One explicit object holds every collaborator the control plane needs. It is built
once in ``create_app`` and reached through ``request.app.state``, so:

  - there is no module-level mutable state and no import-time singleton;
  - a test builds its own container with fakes and needs no monkeypatching;
  - two applications can coexist in one process without sharing state.

    create_app(settings, dependencies=None)
            |
            +-- build_dependencies(settings)   <- the real wiring
            |
            +-- AppDependencies                <- passed explicitly in tests

Substitution points, each a Protocol satisfied by an in-memory implementation
today:

| Concern          | Spec 001                     | Replaced by                    |
|------------------|------------------------------|--------------------------------|
| identity         | DevelopmentIdentityResolver  | real authentication task        |
| projects         | InMemoryProjectRegistry      | PostgreSQL registry            |
| job records      | InMemoryJobRecordStore       | Task 3 JobStore (Redis/Postgres) |
| agent provider   | configured via registry      | AstraProvider / CodexProvider  |
| worker selection | SingleReadyWorkerSelector    | multi-worker scheduler         |
| artifacts        | LocalArtifactStore           | S3 / object storage            |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from studio_agent import JobFactory
from studio_agent.design_provider import DesignAgentProvider
from studio_agent.provider import AgentProvider
from studio_agent.providers.registry import get_design_provider, get_provider

# The ARTIFACT STORE only. The control plane must never acquire a dependency on
# Blender, on subprocess execution, or on the render scripts, so
# BlenderPreviewGenerator is deliberately NOT imported here — generation happens on
# the worker. A test asserts this package imports no Blender implementation.
from studio_preview.artifacts import ArtifactStore, LocalArtifactStore

from .chat_service import ChatService
from .context_builder import ContextBuilder
from .design_chat import DesignChatService
from .ingest import ReferenceIngestService
from .identity import IdentityResolver, default_identity_resolver
from .job_records import InMemoryJobRecordStore, JobRecordStore
from .projects import ProjectRegistry, registry_from_ids
from .reconciliation import JobReconciler
from .scene_grounding import SceneGrounder
from .scene_reporting import SceneReporter
from .settings import Settings
from .storage import (
    ReferenceFileStore,
    SqliteJobRecordStore,
    StudioDatabase,
    StudioRepositories,
)
from .worker_link.gateway import WorkerGateway
from .worker_link.manager import WorkerConnectionManager
from .worker_selection import SingleReadyWorkerSelector, WorkerSelector


@dataclass
class AppDependencies:
    """Everything the routes and services need, assembled explicitly."""

    settings: Settings
    identity_resolver: IdentityResolver
    projects: ProjectRegistry
    store: JobRecordStore
    manager: WorkerConnectionManager
    gateway: WorkerGateway
    provider: AgentProvider
    selector: WorkerSelector
    reconciler: JobReconciler
    chat_service: ChatService
    #: Serves generated preview artifacts (Task 10). Read-only from the control
    #: plane's perspective: the worker writes, the API serves.
    artifacts: ArtifactStore
    #: Durable workspace state: projects, references, facts, conversation, approvals.
    repositories: StudioRepositories
    #: Reference byte storage. Paths live here and never reach a contract.
    reference_files: ReferenceFileStore
    ingest: ReferenceIngestService
    context_builder: ContextBuilder
    #: The design agent, reached only through its abstraction.
    design_provider: DesignAgentProvider
    design_chat: DesignChatService
    #: Keeps the cached scene and artifact index in step with worker reports.
    scene_reporter: SceneReporter
    #: Reads the project before the first turn about it.
    scene_grounder: SceneGrounder
    job_factory: JobFactory = field(default_factory=JobFactory)


def build_dependencies(
    settings: Settings,
    provider: Optional[AgentProvider] = None,
    identity_resolver: Optional[IdentityResolver] = None,
    projects: Optional[ProjectRegistry] = None,
    store: Optional[JobRecordStore] = None,
    manager: Optional[WorkerConnectionManager] = None,
    artifacts: Optional[ArtifactStore] = None,
    database: Optional[StudioDatabase] = None,
    design_provider: Optional[DesignAgentProvider] = None,
    reference_files: Optional[ReferenceFileStore] = None,
) -> AppDependencies:
    """Wire the real Spec 001 dependency graph.

    Each collaborator can be overridden, which is how the integration tests inject
    a shared worker manager or a stub provider without touching the wiring.
    """
    resolved_projects = projects or registry_from_ids(settings.project_ids)

    # Durable workspace state. An unset database_path means EPHEMERAL, so a directly
    # constructed Settings (how tests build an app) gets an isolated in-memory database
    # rather than sharing one file on disk. `load_settings` supplies the durable path,
    # so the production entry point persists.
    resolved_database = database or StudioDatabase(
        Path(settings.database_path) if settings.database_path else Path(":memory:")
    )
    repositories = StudioRepositories(resolved_database)
    resolved_store = store or SqliteJobRecordStore(resolved_database)

    # The root is server-chosen configuration; a request can never influence it.
    resolved_artifacts = artifacts or LocalArtifactStore(
        Path(settings.artifact_root) if settings.artifact_root else None
    )

    resolved_manager = manager or WorkerConnectionManager(
        expected_token=settings.worker_token,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
    )

    reconciler = JobReconciler(store=resolved_store)
    # The gateway notifies the reconciler about validated worker reports, so job
    # records follow the worker without the gateway knowing what a job record is.
    gateway = WorkerGateway(manager=resolved_manager, observers=[reconciler.observe])

    # Resolved by NAME through the provider registry: no concrete provider class is
    # referenced here, so switching engines is configuration.
    resolved_provider = provider or get_provider(settings.agent_provider)

    selector = SingleReadyWorkerSelector(manager=resolved_manager)
    job_factory = JobFactory()

    chat_service = ChatService(
        provider=resolved_provider,
        projects=resolved_projects,
        store=resolved_store,
        selector=selector,
        offer_job=gateway.offer,
        job_factory=job_factory,
    )

    # --- the design workspace -------------------------------------------
    if reference_files is not None:
        resolved_reference_files = reference_files
    elif settings.reference_root:
        resolved_reference_files = ReferenceFileStore(Path(settings.reference_root))
    else:
        # Ephemeral, for the same reason as the database: an unconfigured app must not
        # write uploads into the developer's runtime directory.
        import tempfile

        resolved_reference_files = ReferenceFileStore(
            Path(tempfile.mkdtemp(prefix="studio_references_"))
        )
    ingest = ReferenceIngestService(
        repositories=repositories, files=resolved_reference_files
    )
    context_builder = ContextBuilder(
        repositories=repositories, files=resolved_reference_files
    )
    resolved_design_provider = design_provider or get_design_provider(
        settings.design_provider
    )
    design_chat = DesignChatService(
        provider=resolved_design_provider,
        repositories=repositories,
        context_builder=context_builder,
        projects=resolved_projects,
        store=resolved_store,
        selector=selector,
        offer_job=gateway.offer,
        blender_available=lambda: bool(resolved_manager.live_workers()),
    )

    # The reporter observes worker traffic, so the cached scene and the artifact index
    # follow Blender without the gateway knowing either exists.
    scene_reporter = SceneReporter(repositories=repositories)
    gateway.observers.append(scene_reporter.observe)

    # Reads the project before the first turn about it. Constructed after the gateway
    # because it dispatches through it, and given to the chat service so a turn is never
    # planned against an unread project.
    #
    # `scene_grounding_timeout_seconds` is the master switch. Zero — the default for a
    # directly constructed Settings, which is the shape tests use — means the platform
    # does not read projects on its own, so a test's Blender stays untouched until the
    # test asks for something. `load_settings` turns it on for the real entry point.
    scene_grounder = SceneGrounder(
        repositories=repositories,
        store=resolved_store,
        selector=selector,
        offer_job=gateway.offer,
        timeout_seconds=settings.scene_grounding_timeout_seconds,
        project_ids=tuple(settings.project_ids),
    )
    if settings.scene_grounding_timeout_seconds > 0:
        design_chat.ground_scene = scene_grounder.ensure
        # Read the projects as soon as a worker connects, so the first message a user
        # sends is already grounded and no request has to wait for Blender.
        gateway.observers.append(scene_grounder.observe)

    return AppDependencies(
        settings=settings,
        identity_resolver=identity_resolver or default_identity_resolver(settings),
        projects=resolved_projects,
        store=resolved_store,
        manager=resolved_manager,
        gateway=gateway,
        provider=resolved_provider,
        selector=selector,
        reconciler=reconciler,
        chat_service=chat_service,
        artifacts=resolved_artifacts,
        repositories=repositories,
        reference_files=resolved_reference_files,
        ingest=ingest,
        context_builder=context_builder,
        design_provider=resolved_design_provider,
        design_chat=design_chat,
        scene_reporter=scene_reporter,
        scene_grounder=scene_grounder,
        job_factory=job_factory,
    )


__all__ = ["AppDependencies", "build_dependencies"]

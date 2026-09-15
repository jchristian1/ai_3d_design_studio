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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from studio_agent import JobFactory
from studio_agent.provider import AgentProvider
from studio_agent.providers.registry import get_provider

from .chat_service import ChatService
from .identity import IdentityResolver, default_identity_resolver
from .job_records import InMemoryJobRecordStore, JobRecordStore
from .projects import ProjectRegistry, registry_from_ids
from .reconciliation import JobReconciler
from .settings import Settings
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
    job_factory: JobFactory = field(default_factory=JobFactory)


def build_dependencies(
    settings: Settings,
    provider: Optional[AgentProvider] = None,
    identity_resolver: Optional[IdentityResolver] = None,
    projects: Optional[ProjectRegistry] = None,
    store: Optional[JobRecordStore] = None,
    manager: Optional[WorkerConnectionManager] = None,
) -> AppDependencies:
    """Wire the real Spec 001 dependency graph.

    Each collaborator can be overridden, which is how the integration tests inject
    a shared worker manager or a stub provider without touching the wiring.
    """
    resolved_projects = projects or registry_from_ids(settings.project_ids)
    resolved_store = store or InMemoryJobRecordStore()

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
        job_factory=job_factory,
    )


__all__ = ["AppDependencies", "build_dependencies"]

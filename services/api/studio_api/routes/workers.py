"""GET /api/workers — development and debugging view of connected workers.

Spec 001, Task 9.

Everything returned here is information the worker CHOSE to advertise at
registration, projected through an explicit allow-list model.

Deliberately absent, and unable to appear:

  - the pre-shared token — ``WorkerConnectionManager`` verifies it at hello and
    never stores it, so there is nothing to leak (Task 8);
  - filesystem paths, the home directory, project locations — the worker's
    capabilities schema is closed and contains no path field;
  - environment variables and other secrets — never collected in the first place.

``WorkerCapabilitiesModel`` is an allow-list projection: unknown keys are dropped
rather than echoed, so even a future capability field cannot appear here without
being added deliberately.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models import (
    WorkerCapabilitiesModel,
    WorkerModel,
    WorkersResponseModel,
)
from ..worker_link.manager import HEALTHY
from .support import AppDependencies, get_dependencies

router = APIRouter(prefix="/api", tags=["workers"])


@router.get(
    "/workers",
    response_model=WorkersResponseModel,
    summary="Connected Blender workers",
)
async def list_workers(
    dependencies: AppDependencies = Depends(get_dependencies),
) -> WorkersResponseModel:
    snapshots = dependencies.gateway.worker_snapshots()

    workers = [
        WorkerModel(
            worker_id=snapshot["worker_id"],
            connection_id=snapshot["connection_id"],
            protocol_version=snapshot["protocol_version"],
            liveness=snapshot["liveness"],
            worker_state=snapshot["worker_state"],
            connected=snapshot["connected"],
            current_job_id=snapshot["current_job_id"],
            pending_offers=snapshot["pending_offers"],
            # Allow-list projection: only the declared capability fields survive.
            capabilities=WorkerCapabilitiesModel(**snapshot.get("capabilities", {})),
        )
        for snapshot in snapshots
    ]

    return WorkersResponseModel(
        workers=workers,
        registered_workers=len(workers),
        ready_workers=sum(1 for worker in workers if worker.liveness == HEALTHY),
    )


__all__ = ["router"]

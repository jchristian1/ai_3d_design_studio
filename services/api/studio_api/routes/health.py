"""GET /health — control-plane process health.

Spec 001, Task 9.

Reports what this process can actually know. ``api: healthy`` means the control
plane is serving; it says NOTHING about Blender.

Blender health is reported separately and is never inferred from the API being
alive: ``blender_capable_workers`` counts connected workers that advertised a
usable Blender. When it is 0, chat submissions will fail with 503 even though
``api`` is healthy — and that is the honest answer, not a contradiction.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models import HealthModel
from ..worker_link.manager import HEALTHY
from .support import AppDependencies, get_dependencies

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthModel, summary="Control-plane health")
async def health(
    dependencies: AppDependencies = Depends(get_dependencies),
) -> HealthModel:
    snapshots = dependencies.gateway.worker_snapshots()
    return HealthModel(
        api="healthy",
        environment=dependencies.settings.environment,
        registered_workers=len(snapshots),
        ready_workers=sum(1 for s in snapshots if s["liveness"] == HEALTHY),
        blender_capable_workers=sum(
            1
            for s in snapshots
            if bool(s.get("capabilities", {}).get("blender_available"))
        ),
    )


__all__ = ["router"]

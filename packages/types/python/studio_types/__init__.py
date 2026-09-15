"""Shared data types for AI 3D Design Studio — Python representation.

Canonical unit is METERS (see .kiro/steering/blender.md).

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation  (packages/types/src/index.ts)
             +--> Python representation      (this module)

Canonical schemas:
    Vec3 -> vec3.schema.json
    Job  -> job.schema.json
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobType = Literal["chat"]

JOB_STATUSES: tuple[JobStatus, ...] = ("queued", "running", "succeeded", "failed")
JOB_TYPES: tuple[JobType, ...] = ("chat",)


@dataclass(frozen=True)
class Vec3:
    """A 3D vector in canonical meters."""

    x: float
    y: float
    z: float


@dataclass
class Job:
    """A unit of work explicitly bound to a project.

    Every job MUST identify its project (see .kiro/steering/security.md).
    """

    id: str
    project_id: str
    session_id: str
    type: JobType
    status: JobStatus
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    result: Optional[Any] = None

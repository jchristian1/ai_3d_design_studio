"""Shared data types for AI 3D Design Studio — Python representation.

Canonical unit is METERS (see .kiro/steering/blender.md).

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation  (packages/types/src/index.ts)
             +--> Python representation      (this module)

Canonical schemas:
    Vec3          -> vec3.schema.json
    Job           -> job.schema.json
    LengthUnit    -> length-unit.schema.json
    Measurement   -> measurement.schema.json
    Axis          -> axis.schema.json
    Direction     -> direction.schema.json
    AxisDirection -> axis-direction.schema.json
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


# ---------------------------------------------------------------------------
# Spatial vocabulary (Spec 001, Task 2)
# ---------------------------------------------------------------------------

#: Units accepted at an input boundary. Meters is the canonical internal unit;
#: centimeters exist so user-facing measurements are converted once, at the edge.
#: Canonical schema: length-unit.schema.json
LengthUnit = Literal["m", "cm"]

#: A Blender WORLD-SPACE axis. World-space only for this milestone;
#: camera-relative interpretation is explicitly deferred.
#: Canonical schema: axis.schema.json
Axis = Literal["x", "y", "z"]

#: A named WORLD-SPACE direction. Not camera-relative.
#: Canonical schema: direction.schema.json
Direction = Literal["right", "left", "forward", "back", "up", "down"]

#: +1 along the axis, -1 against it.
AxisSign = Literal[1, -1]

LENGTH_UNITS: tuple[LengthUnit, ...] = ("m", "cm")
AXES: tuple[Axis, ...] = ("x", "y", "z")
DIRECTIONS: tuple[Direction, ...] = (
    "right",
    "left",
    "forward",
    "back",
    "up",
    "down",
)
AXIS_SIGNS: tuple[AxisSign, ...] = (-1, 1)


@dataclass(frozen=True)
class Measurement:
    """A distance with an explicit unit, before conversion to canonical meters.

    Canonical schema: ``measurement.schema.json``
    """

    value: float
    unit: LengthUnit


@dataclass(frozen=True)
class AxisDirection:
    """The resolved result of interpreting a Direction: axis plus sign.

    Carries no distance and never touches Blender.

    Canonical schema: ``axis-direction.schema.json``
    """

    axis: Axis
    sign: AxisSign

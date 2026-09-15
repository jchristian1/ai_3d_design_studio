"""Shared data types for AI 3D Design Studio — Python representation.

Canonical unit is METERS (see .kiro/steering/blender.md).

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation  (packages/types/src/index.ts)
             +--> Python representation      (this module)

Canonical schemas:
    Vec3              -> vec3.schema.json
    Job               -> job.schema.json
    JobType           -> job-type.schema.json
    JobClaim          -> job-claim.schema.json
    ObjectRef         -> object-ref.schema.json
    MoveObjectPayload -> move-object-payload.schema.json
    LengthUnit        -> length-unit.schema.json
    Measurement       -> measurement.schema.json
    Axis              -> axis.schema.json
    Direction         -> direction.schema.json
    AxisDirection     -> axis-direction.schema.json
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

JobStatus = Literal["queued", "claimed", "running", "succeeded", "failed"]

#: The operation a Job carries. This is the discriminator of the job union:
#: each job_type binds to exactly one payload type.
#:
#: Reserved for future milestones (each needs a payload schema + a conditional in
#: job.schema.json before being added): resize_object, set_material, create_wall,
#: create_opening, set_light, create_camera, render_preview, save_version.
#:
#: Canonical schema: job-type.schema.json
JobType = Literal["move_object"]

JOB_STATUSES: tuple[JobStatus, ...] = (
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
)
JOB_TYPES: tuple[JobType, ...] = ("move_object",)

#: Statuses in which a worker owns the job.
OWNED_JOB_STATUSES: tuple[JobStatus, ...] = ("claimed", "running")

#: Statuses from which no further transition is allowed.
TERMINAL_JOB_STATUSES: tuple[JobStatus, ...] = ("succeeded", "failed")


@dataclass(frozen=True)
class Vec3:
    """A 3D vector in canonical meters."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class ObjectRef:
    """A reference to a scene object.

    At least one of ``object_id`` or ``name`` must be present. Prefer the stable
    machine-readable ``object_id`` (see .kiro/steering/blender.md); ``name`` is
    accepted for simple seed scenes such as the Spec 001 "Cube".

    Canonical schema: ``object-ref.schema.json``
    """

    object_id: Optional[str] = None
    name: Optional[str] = None


@dataclass(frozen=True)
class MoveObjectPayload:
    """A fully resolved translation.

    There is no unit field and no direction field, so unresolved language
    ("50 cm", "right") is unrepresentable.

    Canonical schema: ``move-object-payload.schema.json``
    """

    target: ObjectRef
    delta_meters: Vec3


@dataclass(frozen=True)
class JobClaim:
    """Worker ownership metadata, set when a worker claims a job.

    Canonical schema: ``job-claim.schema.json``
    """

    worker_id: str
    claimed_at: str
    lease_expires_at: str


@dataclass(frozen=True)
class RequestOrigin:
    """The originating user/API submission a job came from.

    This is the source of MUTATION IDENTITY. A retry reuses the same
    ``request_id`` (so it executes once); a genuinely new command gets a new
    ``request_id`` (so it executes again) even when the resolved payload is
    byte-identical.

    Canonical schema: ``request-origin.schema.json``
    """

    request_id: str
    #: Zero-based position of this operation within the originating request.
    operation_index: int


@dataclass
class Job:
    """The durable unit of work carrying a fully resolved operation.

    ``job_type`` discriminates ``payload``: each type binds to exactly one
    payload dataclass, so a Job is never an untyped dictionary.

    ``project_id`` is mandatory: a worker must never modify another project's
    Blender file (see .kiro/steering/security.md).

    Two identities are distinguished:
      - ``job_id`` identifies THIS job record, and is what a worker deduplicates
        duplicate queue deliveries on.
      - ``idempotency_key`` is the mutation identity derived from ``origin``.

    Canonical schema: ``job.schema.json``
    """

    job_id: str
    job_type: JobType
    project_id: str
    session_id: str
    user_id: str
    payload: Any
    #: Where this job came from; the basis of its idempotency identity.
    origin: RequestOrigin
    status: JobStatus
    #: Mutation identity: derived from project + origin, NOT from payload.
    idempotency_key: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    #: Diagnostics only: canonical hash of (job_type, payload).
    content_fingerprint: Optional[str] = None
    claim: Optional[JobClaim] = None
    error: Optional[Any] = None
    result: Optional[Any] = None


#: Maps each job_type to its payload dataclass. Extend alongside JobType.
JOB_PAYLOAD_BY_TYPE: dict[str, type] = {"move_object": MoveObjectPayload}


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

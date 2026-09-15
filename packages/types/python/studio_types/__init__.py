"""Shared data types for AI 3D Design Studio — Python representation.

Canonical unit is METERS (see .kiro/steering/blender.md).

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation  (packages/types/src/index.ts)
             +--> Python representation      (this module)

Canonical schemas:
    Vec3              -> vec3.schema.json
    EulerRadians      -> euler-radians.schema.json
    Scale3            -> scale3.schema.json
    Job               -> job.schema.json
    JobType           -> job-type.schema.json
    JobClaim          -> job-claim.schema.json
    ObjectRef         -> object-ref.schema.json
    MoveObjectPayload -> move-object-payload.schema.json
    InspectScenePayload -> inspect-scene-payload.schema.json
    SceneSnapshot     -> scene-snapshot.schema.json
    SceneObject       -> scene-object.schema.json
    SceneUnits        -> scene-units.schema.json
    MaterialSummary   -> material-summary.schema.json
    MaterialColor     -> material-color.schema.json
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
#: Each type also carries a CLASSIFICATION (see MUTATING_JOB_TYPES /
#: READ_JOB_TYPES): ``move_object`` mutates, ``inspect_scene`` only reads.
#:
#: Reserved for future milestones (each needs a payload schema + a conditional in
#: job.schema.json before being added): rotate_object, scale_object,
#: set_object_dimensions, set_material_color, create_wall, create_opening,
#: set_light, create_camera, render_preview, save_version.
#:
#: Canonical schema: job-type.schema.json
JobType = Literal["move_object", "inspect_scene"]

JOB_STATUSES: tuple[JobStatus, ...] = (
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
)
JOB_TYPES: tuple[JobType, ...] = ("move_object", "inspect_scene")

#: Job types that CHANGE the project. These take an exclusive project lock, write
#: a recovery point, persist their plan before mutating, verify from the saved
#: file, save durably, generate a preview, and carry a derived mutation identity.
MUTATING_JOB_TYPES: tuple[JobType, ...] = ("move_object",)

#: Job types that only READ the project. No write lock, no recovery point, no
#: save, no preview; naturally idempotent and therefore cacheable. A read that
#: took a write lock and rendered a preview would be both slow and wrong, which
#: is why the classification is contract vocabulary rather than a worker detail.
READ_JOB_TYPES: tuple[JobType, ...] = ("inspect_scene",)


def is_mutating_job_type(job_type: str) -> bool:
    """True when executing this job type changes the project."""
    return job_type in MUTATING_JOB_TYPES

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


@dataclass(frozen=True)
class InspectScenePayload:
    """The payload of an ``inspect_scene`` read job: deliberately empty.

    Everything the read needs is already trusted job identity — the project is
    ``job.project_id``, which the worker resolves to a location through its own
    registry. There is no selector, no filter, and no field a model could
    populate.

    Canonical schema: ``inspect-scene-payload.schema.json``
    """


#: Maps each job_type to its payload dataclass. Extend alongside JobType.
JOB_PAYLOAD_BY_TYPE: dict[str, type] = {
    "move_object": MoveObjectPayload,
    "inspect_scene": InspectScenePayload,
}


# ---------------------------------------------------------------------------
# MCP execution contracts (Spec 001, Task 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveObjectPlan:
    """A retry-safe execution plan for one move_object mutation.

    Carries absolute world-space endpoints, not just a relative delta, so
    execution can tell "not yet applied" from "already applied" from "the scene
    changed underneath us". ``desired_after_meters`` must equal
    ``expected_before_meters + delta_meters``.

    Canonical schema: ``move-object-plan.schema.json``
    """

    job_id: str
    target: ObjectRef
    expected_before_meters: Vec3
    delta_meters: Vec3
    desired_after_meters: Vec3


# ---------------------------------------------------------------------------
# Generated artifacts (Spec 001, Task 10)
# ---------------------------------------------------------------------------

#: The kind of generated artifact a project version can carry.
#:
#: Spec 001 produces only ``preview_image``: a fast, deterministic still image
#: whose whole purpose is to prove a requested change is visible. Reserved for
#: later milestones, each needing its own media_type handling: ``render_image``
#: (final Cycles render), ``glb_scene`` (interactive browser preview),
#: ``viewport_stream`` (live viewport).
#:
#: Canonical schema: artifact-type.schema.json
ArtifactType = Literal["preview_image"]

ARTIFACT_TYPES: tuple[ArtifactType, ...] = ("preview_image",)

#: The one artifact type Spec 001 generates, named so callers do not repeat the
#: string literal.
PREVIEW_IMAGE: ArtifactType = "preview_image"


@dataclass(frozen=True)
class PreviewArtifact:
    """A reference to one durably stored generated artifact.

    This is what lets a browser see the result of a Blender mutation without the
    control plane, the browser, or the job ever learning a filesystem path.

    NOTE WHAT IS ABSENT: no path, no directory, no filename, no URL.
    ``(project_id, artifact_id)`` is the complete address. The HTTP layer projects
    that into a logical URL, because a worker must not know the control plane's
    route shape and the same artifact is addressed differently in different
    deployments (local filesystem now, object storage later).

    ``checksum`` is for corruption detection, test verification, and later caching
    or version identity. It is explicitly NOT an access credential: authorization
    is always project scope.

    Canonical schema: ``preview-artifact.schema.json``
    """

    artifact_id: str
    project_id: str
    artifact_type: ArtifactType
    media_type: str
    created_at: str
    width: int
    height: int
    size_bytes: int
    #: ``sha256:<64 hex chars>`` of the stored bytes.
    checksum: str
    #: The job whose verified mutation this artifact depicts. Absent for an
    #: artifact not produced by a job, such as a baseline preview.
    job_id: Optional[str] = None
    #: Coarse description of what produced it, e.g. BLENDER_WORKBENCH.
    engine: Optional[str] = None


@dataclass(frozen=True)
class MoveObjectResult:
    """The structured outcome of a move_object execution.

    Machine-readable by design: callers decide what happened from ``applied``,
    ``already_applied``, ``verified`` and ``error``, never from prose.

    Canonical schema: ``move-object-result.schema.json``
    """

    job_id: str
    target: ObjectRef
    requested_delta_meters: Vec3
    applied: bool
    already_applied: bool
    verified: bool
    resolved_object: Optional[ObjectRef] = None
    previous_position_meters: Optional[Vec3] = None
    final_position_meters: Optional[Vec3] = None
    error: Optional[Any] = None


# ---------------------------------------------------------------------------
# Spatial vocabulary (Spec 001, Task 2)
# ---------------------------------------------------------------------------

#: Units accepted at an input boundary. Meters is the canonical internal unit;
#: centimeters exist so user-facing measurements are converted once, at the edge.
#: Canonical schema: length-unit.schema.json
LengthUnit = Literal["m", "cm"]

#: Angular units accepted at an input boundary. RADIANS is the canonical internal
#: unit (Blender's rotation_euler is radians, so nothing converts at the Blender
#: boundary); degrees exist so a user-facing value is converted once, at the edge.
#: These are the NORMALIZED wire values: human spellings ("degrees", "°") are
#: accepted as tokens by studio_spatial and collapse to these before crossing a
#: boundary.
#: Canonical schema: angle-unit.schema.json
AngleUnit = Literal["rad", "deg"]

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
ANGLE_UNITS: tuple[AngleUnit, ...] = ("rad", "deg")
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
class AngleMeasurement:
    """An angle with an EXPLICIT unit, before conversion to canonical radians.

    The unit is mandatory: a bare number is ambiguous, and guessing "probably
    degrees" is the class of mistake that puts a 45-radian rotation into a scene.

    An INPUT-boundary shape only. Once interpreted, an angle travels as a plain
    number of radians in a named field (``rotation_euler_radians``), so an
    AngleMeasurement never reaches the worker — the same discipline that keeps
    :class:`Measurement` out of a Job payload.

    Direction of rotation is carried by the SIGN of ``value``, and the value is
    deliberately NOT reduced modulo a full turn: conversion and normalization are
    different concerns.

    Canonical schema: ``angle-measurement.schema.json``
    """

    value: float
    unit: AngleUnit


@dataclass(frozen=True)
class AxisDirection:
    """The resolved result of interpreting a Direction: axis plus sign.

    Carries no distance and never touches Blender.

    Canonical schema: ``axis-direction.schema.json``
    """

    axis: Axis
    sign: AxisSign


# ---------------------------------------------------------------------------
# Scene description (Spec 002, Task 1)
# ---------------------------------------------------------------------------

#: Blender's scene unit system. A closed set: Blender's RNA enum for it is static
#: and complete. (``length_unit`` deliberately is NOT closed — see SceneUnits.)
#:
#: Canonical schema: scene-units.schema.json
UnitSystem = Literal["NONE", "METRIC", "IMPERIAL"]

UNIT_SYSTEMS: tuple[UnitSystem, ...] = ("NONE", "METRIC", "IMPERIAL")


@dataclass(frozen=True)
class EulerRadians:
    """An XYZ Euler rotation in canonical RADIANS.

    A separate type from :class:`Vec3` (canonical meters) on purpose: reusing the
    meters vector for an angle would state a unit it does not mean. Radians are
    canonical because Blender's ``rotation_euler`` is radians, so nothing is
    converted at the Blender boundary; degrees are a language-edge unit converted
    exactly once in ``packages/spatial``.

    Canonical schema: ``euler-radians.schema.json``
    """

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Scale3:
    """A per-axis UNITLESS transform scale.

    Distinct from ``dimensions_meters``, which is physical size. "Make it 20%
    smaller" is size intent; "set its scale to 0.8" is transform-scale intent.

    Canonical schema: ``scale3.schema.json``
    """

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class MaterialColor:
    """A canonical colour: linear sRGB with straight alpha, channels in [0, 1].

    Linear sRGB because that is what Blender's ``base_color`` expects, so there is
    no colour-space conversion at the Blender boundary. A colour NAME is never
    representable: names are interpreted above the worker and arrive as numbers.

    Canonical schema: ``material-color.schema.json``
    """

    r: float
    g: float
    b: float
    a: float


@dataclass(frozen=True)
class MaterialSummary:
    """A BASIC description of an object's first material.

    Enough to answer "what colour is it?" and to plan a colour change, and no
    more: Spec 002 is limited to base colour, so no node tree, no texture path and
    no image reference is representable.

    ``base_color`` is absent when the material exposes no summarisable colour (a
    procedural or node-driven material). That is NOT the same as black.

    Canonical schema: ``material-summary.schema.json``
    """

    name: str
    base_color: Optional[MaterialColor] = None


@dataclass(frozen=True)
class SceneUnits:
    """The OBSERVED unit configuration of a Blender scene.

    Note the deliberate difference from :data:`LengthUnit`, which is the
    input-boundary vocabulary ("m", "cm") user language is converted from. This is
    Blender's own scene setting ("METERS"): a different concept that shares a word.

    ``length_unit`` is a plain string rather than a closed enum because Blender's
    RNA enum for it is DYNAMIC — its members depend on the selected system and
    introspection reports only a placeholder — so a hard-coded list would reject a
    project we can legitimately read.

    Canonical schema: ``scene-units.schema.json``
    """

    unit_system: str
    length_unit: str
    scale_length: float


@dataclass(frozen=True)
class SceneObject:
    """One object as the platform is willing to DESCRIBE it to a language model.

    Authoritative and read-only: produced from a real Blender scene, never
    model-authored.

    NOTE WHAT IS ABSENT: no .blend path, no filepath, no filename, no hostname, no
    worker id, no token, no Blender pointer, no data-block reference, no
    script/expression field, and no free-form metadata bag. The canonical schema
    declares ``additionalProperties: false``, so the absence is structural.

    Canonical schema: ``scene-object.schema.json``
    """

    name: str
    object_type: str
    world_position_meters: Vec3
    dimensions_meters: Vec3
    rotation_euler_radians: EulerRadians
    scale: Scale3
    visible: bool
    #: Stable machine-readable identifier; absent when the object has none.
    studio_object_id: Optional[str] = None
    #: Basic summary of the first material; absent when there is none.
    material: Optional[MaterialSummary] = None


@dataclass(frozen=True)
class SceneSnapshot:
    """An authoritative, read-only, SAFE description of a project's scene.

    ``captured_at`` is INFORMATIONAL and is deliberately excluded from
    ``scene_version``: two reads of an unchanged scene must produce an identical
    version. ``scene_version`` is likewise excluded from its own input. The digest
    input is an explicit versioned allow-list projection, not "this document minus
    a deny-list" — see ``studio_contracts.scene``.

    Canonical schema: ``scene-snapshot.schema.json``
    """

    project_id: str
    scene_version: str
    units: SceneUnits
    objects: tuple[SceneObject, ...]
    captured_at: str

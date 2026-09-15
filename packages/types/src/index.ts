/**
 * Shared data types for AI 3D Design Studio — TypeScript representation.
 *
 * Canonical unit is METERS (see .kiro/steering/blender.md).
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
 *            |
 *            +--> TypeScript representation  (this file)
 *            +--> Python representation      (studio_types)
 *
 * Canonical schemas:
 *   Vec3              -> vec3.schema.json
 *   Job               -> job.schema.json
 *   JobType           -> job-type.schema.json
 *   JobClaim          -> job-claim.schema.json
 *   ObjectRef         -> object-ref.schema.json
 *   MoveObjectPayload -> move-object-payload.schema.json
 *   LengthUnit        -> length-unit.schema.json
 *   Measurement       -> measurement.schema.json
 *   Axis              -> axis.schema.json
 *   Direction         -> direction.schema.json
 *   AxisDirection     -> axis-direction.schema.json
 */

/** A 3D vector in canonical meters. */
export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

/** Lifecycle states for a Job (see .kiro/steering/architecture.md). */
export type JobStatus = "queued" | "claimed" | "running" | "succeeded" | "failed";

/**
 * The operation a Job carries. This is the discriminator of the job union:
 * each job_type binds to exactly one payload type.
 *
 * Each type also carries a CLASSIFICATION (see MUTATING_JOB_TYPES /
 * READ_JOB_TYPES): `move_object` mutates, `inspect_scene` only reads.
 *
 * Reserved for future milestones (each needs a payload schema + a conditional in
 * job.schema.json before being added): rotate_object, scale_object,
 * set_object_dimensions, set_material_color, create_wall, create_opening,
 * set_light, create_camera, render_preview, save_version.
 *
 * Canonical schema: job-type.schema.json
 */
export type JobType = "move_object" | "inspect_scene";

/**
 * A reference to a scene object. At least one of object_id or name is present.
 * Canonical schema: object-ref.schema.json
 */
export interface ObjectRef {
  object_id?: string;
  name?: string;
}

/**
 * A fully resolved translation. There is no unit field and no direction field,
 * so unresolved language ("50 cm", "right") is unrepresentable.
 * Canonical schema: move-object-payload.schema.json
 */
export interface MoveObjectPayload {
  target: ObjectRef;
  delta_meters: Vec3;
}

/**
 * The payload of an `inspect_scene` read job: deliberately EMPTY.
 *
 * Everything the read needs is already trusted job identity — the project is
 * `job.project_id`, which the worker resolves to a location through its own
 * registry. There is no selector, no filter, and no field a model could populate.
 *
 * Canonical schema: inspect-scene-payload.schema.json
 */
export type InspectScenePayload = Record<string, never>;

/** Maps each job_type to its payload type. Extend alongside JobType. */
export interface JobPayloadByType {
  move_object: MoveObjectPayload;
  inspect_scene: InspectScenePayload;
}

/**
 * Worker ownership metadata, set when a worker claims a job.
 * Canonical schema: job-claim.schema.json
 */
export interface JobClaim {
  worker_id: string;
  claimed_at: string;
  lease_expires_at: string;
}

/**
 * The originating user/API submission a job came from.
 *
 * This is the source of MUTATION IDENTITY. A retry reuses the same request_id
 * (so it executes once); a genuinely new command gets a new request_id (so it
 * executes again) even when the resolved payload is byte-identical.
 *
 * Canonical schema: request-origin.schema.json
 */
export interface RequestOrigin {
  request_id: string;
  /** Zero-based position of this operation within the originating request. */
  operation_index: number;
}

/**
 * The durable unit of work carrying a fully resolved operation toward the
 * Blender Worker.
 *
 * Generic over job_type so `payload` is typed per operation rather than being an
 * untyped dictionary. `Job` (the default) is the discriminated union over all
 * job types.
 *
 * project_id is mandatory: a worker must never modify another project's Blender
 * file (see .kiro/steering/security.md).
 *
 * Two identities are distinguished:
 *   - `job_id` identifies THIS job record, and is what a worker deduplicates
 *     duplicate queue deliveries on.
 *   - `idempotency_key` is the mutation identity derived from `origin`.
 *
 * Canonical schema: job.schema.json
 */
export interface JobOf<T extends JobType> {
  job_id: string;
  job_type: T;
  project_id: string;
  session_id: string;
  user_id: string;
  payload: JobPayloadByType[T];
  /** Where this job came from; the basis of its idempotency identity. */
  origin: RequestOrigin;
  status: JobStatus;
  created_at: string; // ISO-8601 timestamp
  /** Mutation identity: derived from project + origin, NOT from payload. */
  idempotency_key: string;
  /** Diagnostics only: canonical hash of (job_type, payload). */
  content_fingerprint?: string;
  claim?: JobClaim;
  error?: { code: string; message: string };
  result?: unknown;
}

export type MoveObjectJob = JobOf<"move_object">;
export type InspectSceneJob = JobOf<"inspect_scene">;

/** The discriminated union over every supported job type. */
export type Job = MoveObjectJob | InspectSceneJob;

export const JOB_STATUSES: readonly JobStatus[] = [
  "queued",
  "claimed",
  "running",
  "succeeded",
  "failed",
] as const;

export const JOB_TYPES: readonly JobType[] = [
  "move_object",
  "inspect_scene",
] as const;

/**
 * Job types that CHANGE the project. These take an exclusive project lock, write
 * a recovery point, persist their plan before mutating, verify from the saved
 * file, save durably, generate a preview, and carry a derived mutation identity.
 */
export const MUTATING_JOB_TYPES: readonly JobType[] = ["move_object"] as const;

/**
 * Job types that only READ the project. No write lock, no recovery point, no
 * save, no preview; naturally idempotent and therefore cacheable. A read that
 * took a write lock and rendered a preview would be both slow and wrong, which is
 * why the classification is contract vocabulary rather than a worker detail.
 */
export const READ_JOB_TYPES: readonly JobType[] = ["inspect_scene"] as const;

/** True when executing this job type changes the project. */
export function isMutatingJobType(jobType: string): boolean {
  return (MUTATING_JOB_TYPES as readonly string[]).includes(jobType);
}

/** Statuses in which a worker owns the job. */
export const OWNED_JOB_STATUSES: readonly JobStatus[] = [
  "claimed",
  "running",
] as const;

/** Statuses from which no further transition is allowed. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
  "succeeded",
  "failed",
] as const;

// ---------------------------------------------------------------------------
// Generated artifacts (Spec 001, Task 10)
// ---------------------------------------------------------------------------

/**
 * The kind of generated artifact a project version can carry.
 *
 * Spec 001 produces only `preview_image`: a fast, deterministic still image
 * whose whole purpose is to prove a requested change is visible. Reserved for
 * later milestones, each needing its own media_type handling: `render_image`
 * (final Cycles render), `glb_scene` (interactive browser preview),
 * `viewport_stream` (live viewport).
 *
 * Canonical schema: artifact-type.schema.json
 */
export type ArtifactType = "preview_image";

export const ARTIFACT_TYPES: readonly ArtifactType[] = [
  "preview_image",
] as const;

/** The one artifact type Spec 001 generates. */
export const PREVIEW_IMAGE: ArtifactType = "preview_image";

/**
 * A reference to one durably stored generated artifact.
 *
 * This is what lets a browser see the result of a Blender mutation without the
 * control plane, the browser, or the job ever learning a filesystem path.
 *
 * NOTE WHAT IS ABSENT: no path, no directory, no filename, no URL.
 * `(project_id, artifact_id)` is the complete address. The HTTP layer projects
 * that into a logical URL, because a worker must not know the control plane's
 * route shape and the same artifact is addressed differently in different
 * deployments (local filesystem now, object storage later).
 *
 * `checksum` is for corruption detection, test verification, and later caching
 * or version identity. It is explicitly NOT an access credential: authorization
 * is always project scope.
 *
 * Canonical schema: preview-artifact.schema.json
 */
export interface PreviewArtifact {
  artifact_id: string;
  project_id: string;
  artifact_type: ArtifactType;
  media_type: string;
  created_at: string;
  width: number;
  height: number;
  size_bytes: number;
  /** `sha256:<64 hex chars>` of the stored bytes. */
  checksum: string;
  /**
   * The job whose verified mutation this artifact depicts. Absent for an
   * artifact not produced by a job, such as a baseline preview.
   */
  job_id?: string;
  /** Coarse description of what produced it, e.g. BLENDER_WORKBENCH. */
  engine?: string;
}

// ---------------------------------------------------------------------------
// Spatial vocabulary (Spec 001, Task 2)
// ---------------------------------------------------------------------------

/**
 * Units accepted at an input boundary. Meters is the canonical internal unit;
 * centimeters exist so user-facing measurements are converted once, at the edge.
 * Canonical schema: length-unit.schema.json
 */
export type LengthUnit = "m" | "cm";

/**
 * A distance with an explicit unit, before conversion to canonical meters.
 * Canonical schema: measurement.schema.json
 */
export interface Measurement {
  value: number;
  unit: LengthUnit;
}

/**
 * Angular units accepted at an input boundary. RADIANS is the canonical internal
 * unit (Blender's rotation_euler is radians, so nothing converts at the Blender
 * boundary); degrees exist so a user-facing value is converted once, at the edge.
 * These are the NORMALIZED wire values: human spellings ("degrees", "°") are
 * accepted as tokens by @studio/spatial and collapse to these before crossing a
 * boundary.
 * Canonical schema: angle-unit.schema.json
 */
export type AngleUnit = "rad" | "deg";

/**
 * An angle with an EXPLICIT unit, before conversion to canonical radians.
 *
 * The unit is mandatory: a bare number is ambiguous. An INPUT-boundary shape only —
 * once interpreted, an angle travels as a plain number of radians in a named field
 * (`rotation_euler_radians`), so an AngleMeasurement never reaches the worker.
 *
 * Direction of rotation is carried by the SIGN of `value`, and the value is
 * deliberately NOT reduced modulo a full turn.
 *
 * Canonical schema: angle-measurement.schema.json
 */
export interface AngleMeasurement {
  value: number;
  unit: AngleUnit;
}

/**
 * A Blender WORLD-SPACE axis. World-space only for this milestone;
 * camera-relative interpretation is explicitly deferred.
 * Canonical schema: axis.schema.json
 */
export type Axis = "x" | "y" | "z";

/**
 * A named WORLD-SPACE direction. Not camera-relative.
 * Canonical schema: direction.schema.json
 */
export type Direction = "right" | "left" | "forward" | "back" | "up" | "down";

/** +1 along the axis, -1 against it. */
export type AxisSign = 1 | -1;

/**
 * The resolved result of interpreting a Direction: axis plus sign.
 * Carries no distance and never touches Blender.
 * Canonical schema: axis-direction.schema.json
 */
export interface AxisDirection {
  axis: Axis;
  sign: AxisSign;
}

export const LENGTH_UNITS: readonly LengthUnit[] = ["m", "cm"] as const;

export const ANGLE_UNITS: readonly AngleUnit[] = ["rad", "deg"] as const;

export const AXES: readonly Axis[] = ["x", "y", "z"] as const;

export const DIRECTIONS: readonly Direction[] = [
  "right",
  "left",
  "forward",
  "back",
  "up",
  "down",
] as const;

export const AXIS_SIGNS: readonly AxisSign[] = [-1, 1] as const;


// ---------------------------------------------------------------------------
// Scene description (Spec 002, Task 1)
// ---------------------------------------------------------------------------

/**
 * Blender's scene unit system. A closed set: Blender's RNA enum for it is static
 * and complete. (`length_unit` deliberately is NOT closed — see SceneUnits.)
 *
 * Canonical schema: scene-units.schema.json
 */
export type UnitSystem = "NONE" | "METRIC" | "IMPERIAL";

export const UNIT_SYSTEMS: readonly UnitSystem[] = [
  "NONE",
  "METRIC",
  "IMPERIAL",
] as const;

/**
 * An XYZ Euler rotation in canonical RADIANS.
 *
 * A separate type from `Vec3` (canonical meters) on purpose: reusing the meters
 * vector for an angle would state a unit it does not mean. Radians are canonical
 * because Blender's `rotation_euler` is radians, so nothing is converted at the
 * Blender boundary; degrees are a language-edge unit converted exactly once in
 * packages/spatial.
 *
 * Canonical schema: euler-radians.schema.json
 */
export interface EulerRadians {
  x: number;
  y: number;
  z: number;
}

/**
 * A per-axis UNITLESS transform scale. Distinct from `dimensions_meters`, which
 * is physical size: "make it 20% smaller" is size intent, "set its scale to 0.8"
 * is transform-scale intent.
 *
 * Canonical schema: scale3.schema.json
 */
export interface Scale3 {
  x: number;
  y: number;
  z: number;
}

/**
 * A canonical colour: linear sRGB with straight alpha, channels in [0, 1].
 *
 * Linear sRGB because that is what Blender's `base_color` expects, so there is no
 * colour-space conversion at the Blender boundary. A colour NAME is never
 * representable: names are interpreted above the worker and arrive as numbers.
 *
 * Canonical schema: material-color.schema.json
 */
export interface MaterialColor {
  r: number;
  g: number;
  b: number;
  a: number;
}

/**
 * A BASIC description of an object's first material — enough to answer "what
 * colour is it?" and to plan a colour change, and no more. Spec 002 is limited to
 * base colour, so no node tree, no texture path and no image reference is
 * representable.
 *
 * `base_color` is absent when the material exposes no summarisable colour (a
 * procedural or node-driven material). That is NOT the same as black.
 *
 * Canonical schema: material-summary.schema.json
 */
export interface MaterialSummary {
  name: string;
  base_color?: MaterialColor;
}

/**
 * The OBSERVED unit configuration of a Blender scene.
 *
 * Note the deliberate difference from `LengthUnit`, which is the input-boundary
 * vocabulary ("m", "cm") user language is converted from. This is Blender's own
 * scene setting ("METERS"): a different concept that shares a word.
 *
 * `length_unit` is a plain string rather than a closed union because Blender's RNA
 * enum for it is DYNAMIC — its members depend on the selected system and
 * introspection reports only a placeholder — so a hard-coded list would reject a
 * project we can legitimately read.
 *
 * Canonical schema: scene-units.schema.json
 */
export interface SceneUnits {
  unit_system: string;
  length_unit: string;
  scale_length: number;
}

/**
 * One object as the platform is willing to DESCRIBE it to a language model.
 * Authoritative and read-only: produced from a real Blender scene, never
 * model-authored.
 *
 * NOTE WHAT IS ABSENT: no .blend path, no filepath, no filename, no hostname, no
 * worker id, no token, no Blender pointer, no data-block reference, no
 * script/expression field, and no free-form metadata bag. The canonical schema
 * declares `additionalProperties: false`, so the absence is structural.
 *
 * Canonical schema: scene-object.schema.json
 */
export interface SceneObject {
  name: string;
  object_type: string;
  world_position_meters: Vec3;
  dimensions_meters: Vec3;
  rotation_euler_radians: EulerRadians;
  scale: Scale3;
  visible: boolean;
  /** Stable machine-readable identifier; absent when the object has none. */
  studio_object_id?: string;
  /** Basic summary of the first material; absent when there is none. */
  material?: MaterialSummary;
}

/**
 * An authoritative, read-only, SAFE description of a project's scene.
 *
 * `captured_at` is INFORMATIONAL and is deliberately excluded from
 * `scene_version`: two reads of an unchanged scene must produce an identical
 * version. `scene_version` is likewise excluded from its own input. The digest
 * input is an explicit versioned allow-list projection, not "this document minus a
 * deny-list" — see packages/contracts/src/scene.ts.
 *
 * Canonical schema: scene-snapshot.schema.json
 */
export interface SceneSnapshot {
  project_id: string;
  scene_version: string;
  units: SceneUnits;
  objects: SceneObject[];
  captured_at: string;
}

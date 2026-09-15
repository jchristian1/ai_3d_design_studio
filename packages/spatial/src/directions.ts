/**
 * Deterministic world-space direction mapping — TypeScript representation.
 *
 * Spec 001, Task 2.
 *
 * Convention for this milestone (fixed, total, and table-driven):
 *
 *     right   -> +X        left    -> -X
 *     forward -> +Y        back    -> -Y
 *     up      -> +Z        down    -> -Z
 *
 * "right" means Blender WORLD-SPACE +X, as required by Spec 001.
 *
 * Scope and deliberate non-goals:
 *   - Camera-relative interpretation is explicitly DEFERRED. Nothing here reads
 *     a camera, a view matrix, or any scene state, so the mapping cannot become
 *     view-dependent by accident.
 *   - Direction interpretation is kept independent of Blender execution: this
 *     module produces vocabulary and plain numbers, never bpy calls. The worker
 *     applies the result (Task 4); it is not applied here.
 *   - No natural-language parsing. Mapping the phrase "to the right" onto the
 *     `right` token is the agent's job (Task 6).
 *
 * Canonical schemas: direction.schema.json, axis-direction.schema.json
 */

import type { ChatError } from "@studio/contracts";
import type {
  Axis,
  AxisDirection,
  Direction,
  Measurement,
  Vec3,
} from "@studio/types";
import { DIRECTIONS } from "@studio/types";

import { toMeters } from "./units.ts";

/**
 * The complete, canonical direction table. Frozen so the mapping cannot be
 * mutated at runtime by a caller.
 */
export const DIRECTION_TO_AXIS: Readonly<Record<Direction, AxisDirection>> =
  Object.freeze({
    right: Object.freeze({ axis: "x", sign: 1 }),
    left: Object.freeze({ axis: "x", sign: -1 }),
    forward: Object.freeze({ axis: "y", sign: 1 }),
    back: Object.freeze({ axis: "y", sign: -1 }),
    up: Object.freeze({ axis: "z", sign: 1 }),
    down: Object.freeze({ axis: "z", sign: -1 }),
  }) as Readonly<Record<Direction, AxisDirection>>;

export const DIRECTION_MESSAGES = {
  UNSUPPORTED_DIRECTION: `unsupported direction; expected one of: ${DIRECTIONS.join(", ")}`,
  NEGATIVE_MAGNITUDE:
    "distance must be a non-negative magnitude; express direction with the direction token, not a negative distance",
} as const;

export type DirectionResult =
  | { ok: true; axisDirection: AxisDirection }
  | { ok: false; error: ChatError };

export type DeltaResult =
  | { ok: true; delta: Vec3; axis: Axis; meters: number }
  | { ok: false; error: ChatError };

/** True when the value is one of the canonical direction tokens. */
export function isDirection(value: unknown): value is Direction {
  return typeof value === "string" && DIRECTIONS.includes(value as Direction);
}

/** Resolve a named world-space direction to an axis and sign. */
export function resolveDirection(direction: unknown): DirectionResult {
  if (!isDirection(direction)) {
    return {
      ok: false,
      error: {
        code: "VALIDATION_ERROR",
        message: DIRECTION_MESSAGES.UNSUPPORTED_DIRECTION,
      },
    };
  }
  return { ok: true, axisDirection: DIRECTION_TO_AXIS[direction] };
}

/** The zero delta, in canonical meters. */
export function zeroDelta(): Vec3 {
  return { x: 0, y: 0, z: 0 };
}

/**
 * Compose a direction and a distance MAGNITUDE into a world-space delta in meters.
 *
 * This is the shape the MCP `move_object` tool consumes (Task 3), produced
 * without any Blender involvement. Only the resolved axis is non-zero.
 *
 * Direction is carried by the direction token ONLY. The measurement must be a
 * non-negative magnitude, so direction is never encoded twice:
 *
 *     directionDeltaMeters("left",  25 cm) -> { x: -0.25, y: 0, z: 0 }
 *     directionDeltaMeters("left", -25 cm) -> VALIDATION_ERROR
 *
 * Zero is a valid magnitude and yields a zero delta.
 *
 * This restriction lives at the composition boundary only. The generic
 * conversion utilities in ./units.ts remain signed on purpose: -25 cm is a
 * legitimate signed measurement in contexts such as coordinates and offsets.
 */
export function directionDeltaMeters(
  direction: unknown,
  measurement: unknown,
): DeltaResult {
  const resolved = resolveDirection(direction);
  if (!resolved.ok) return { ok: false, error: resolved.error };

  const converted = toMeters(measurement as Measurement);
  if (!converted.ok) return { ok: false, error: converted.error };

  // Reject a negative magnitude only here, never in the generic converter.
  if (converted.meters < 0) {
    return {
      ok: false,
      error: {
        code: "VALIDATION_ERROR",
        message: DIRECTION_MESSAGES.NEGATIVE_MAGNITUDE,
      },
    };
  }

  const { axis, sign } = resolved.axisDirection;
  const signed = converted.meters * sign;
  // Normalize -0 so a zero-distance move is sign-stable across languages.
  const magnitude = signed === 0 ? 0 : signed;

  const delta = zeroDelta();
  delta[axis] = magnitude;

  return { ok: true, delta, axis, meters: magnitude };
}

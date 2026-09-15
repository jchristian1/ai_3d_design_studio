/**
 * @studio/spatial — deterministic spatial utilities (Spec 001, Task 2).
 *
 * Two concerns, both pure:
 *   units.ts       length-unit conversion into canonical meters
 *   directions.ts  named world-space direction -> axis + sign
 *
 * Everything here is deterministic, independently testable, free of Blender
 * calls, and free of natural-language/AI reasoning. The behaviour is pinned by a
 * shared language-neutral corpus (./spatial-cases.json) that the Python
 * representation (studio_spatial) executes as well.
 *
 * Direction of truth for the vocabulary these functions speak:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL
 *            |
 *            +--> TypeScript representation (this package)
 *            +--> Python representation (studio_spatial)
 */

export {
  CM_PER_METER,
  CONVERSION_MESSAGES,
  cmToMeters,
  convertToMeters,
  metersToMeters,
  toMeters,
} from "./units.ts";
export type { ConversionResult } from "./units.ts";

export {
  DIRECTION_MESSAGES,
  DIRECTION_TO_AXIS,
  directionDeltaMeters,
  isDirection,
  resolveDirection,
  zeroDelta,
} from "./directions.ts";
export type { DeltaResult, DirectionResult } from "./directions.ts";

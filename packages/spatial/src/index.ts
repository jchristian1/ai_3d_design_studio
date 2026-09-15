/**
 * @studio/spatial — deterministic spatial and value utilities.
 *
 * Concerns, all pure:
 *   units.ts       length-unit conversion into canonical METRES   (Spec 001, Task 2)
 *   directions.ts  named world-space direction -> axis + sign     (Spec 001, Task 2)
 *   angles.ts      angle-unit conversion into canonical RADIANS   (Spec 002, Task 2)
 *   sizing.ts      normalized resize intent -> absolute factor    (Spec 002, Task 2)
 *   colors.ts      canonical linear sRGB colour + tiny palette    (Spec 002, Task 2)
 *
 * Everything here is deterministic, independently testable, free of Blender calls,
 * and free of natural-language/AI reasoning. This package is also free of scene
 * state, camera/view state, provider code, HTTP, the filesystem and the environment
 * (structure.md), which is what lets one conversion site serve every service.
 *
 * This is the ONE place unit and value conversion lives. cm->m, deg->rad,
 * percent->factor and encoded sRGB->linear each exist exactly once, here; a source
 * guard (tests/spatial/test_conversion_site_guard.py and
 * ./conversion-site-guard.test.ts) fails if a second one appears anywhere in runtime
 * code.
 *
 * Conversion is not normalization: nothing here reduces an angle modulo 2π, clamps a
 * colour into range, or clips a resize into a "reasonable" band. Those are different
 * decisions and belong to whoever is entitled to make them.
 *
 * The behaviour is pinned by a shared language-neutral corpus
 * (./spatial-cases.json) that the Python representation (studio_spatial) executes as
 * well.
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

export {
  ANGLE_MESSAGES,
  ANGLE_UNIT_ALIASES,
  DEGREES_PER_HALF_TURN,
  RADIANS_PER_DEGREE,
  convertToRadians,
  degreesToRadians,
  isFiniteAngle,
  normalizeAngleUnit,
  radiansToDegrees,
  radiansToRadians,
  toRadians,
} from "./angles.ts";
export type { AngleResult } from "./angles.ts";

export {
  MAX_REDUCTION_PERCENT,
  PERCENT_WHOLE,
  SIZE_DIRECTIONS,
  SIZING_MESSAGES,
  isSizeDirection,
  isValidSizeFactor,
  resizeFactor,
  validateSizeFactor,
} from "./sizing.ts";
export type { SizeDirection, SizeFactorResult } from "./sizing.ts";

export {
  COLOR_CHANNELS,
  COLOR_MESSAGES,
  COLOR_NAME_ALIASES,
  EIGHT_BIT_MAX,
  NAMED_COLORS,
  PALETTE_PROVENANCE_HEX,
  SRGB_ALPHA,
  SRGB_EXPONENT,
  SRGB_LINEAR_SLOPE,
  SRGB_LINEAR_THRESHOLD,
  SRGB_SCALE,
  TRANSFER_CHANNELS,
  colorFromSrgb8Bit,
  isNamedColor,
  linearChannelToSrgbEncoded,
  normalizeColorName,
  resolveNamedColor,
  srgb8BitToLinear,
  srgbEncodedChannelToLinear,
  validateMaterialColor,
} from "./colors.ts";
export type { ColorResult } from "./colors.ts";

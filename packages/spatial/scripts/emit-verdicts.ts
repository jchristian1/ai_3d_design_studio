/**
 * Emit the TypeScript result for every case in the shared spatial corpus.
 *
 * Used by tests/spatial/test_cross_language_parity.py to prove the TypeScript
 * and Python spatial utilities produce IDENTICAL numeric results, error codes,
 * and error messages. Output is a stable, sorted JSON array on stdout.
 *
 * Numbers are emitted as their exact 17-significant-digit representation so
 * float parity is compared bit-for-bit rather than via a lossy decimal string.
 */

import {
  directionDeltaMeters,
  resizeFactor,
  resolveDirection,
  resolveNamedColor,
  srgbEncodedChannelToLinear,
  toMeters,
  toRadians,
  validateMaterialColor,
  validateSizeFactor,
} from "../src/index.ts";
import {
  decodeColor,
  decodeValue,
  loadSpatialCases,
} from "../src/test-support.ts";

const cases = loadSpatialCases();

/** Exact, round-trippable encoding of a double. */
function encodeNumber(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  // Object.is distinguishes -0 from 0; normalization should prevent -0 entirely.
  if (Object.is(value, -0)) return "-0";
  return value.toPrecision(17);
}

interface Verdict {
  group: string;
  case: string;
  ok: boolean;
  meters?: string;
  axis?: string;
  sign?: number;
  delta?: Record<string, string>;
  error_code?: string;
  error_message?: string;
}

const verdicts: Verdict[] = [];

for (const c of [
  ...cases.conversion.valid,
  ...cases.conversion.invalid,
]) {
  const r = toMeters({ value: decodeValue(c.value), unit: c.unit });
  verdicts.push({
    group: "conversion",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { meters: encodeNumber(r.meters) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of cases.conversion.malformed) {
  const r = toMeters(decodeValue(c.measurement));
  verdicts.push({
    group: "conversion-malformed",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { meters: encodeNumber(r.meters) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of [...cases.directions.valid, ...cases.directions.invalid]) {
  const r = resolveDirection(decodeValue(c.direction));
  verdicts.push({
    group: "direction",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { axis: r.axisDirection.axis, sign: r.axisDirection.sign }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of [...cases.deltas.valid, ...cases.deltas.invalid]) {
  const r = directionDeltaMeters(c.direction, {
    value: decodeValue(c.value),
    unit: c.unit,
  });
  verdicts.push({
    group: "delta",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? {
          axis: r.axis,
          meters: encodeNumber(r.meters),
          delta: {
            x: encodeNumber(r.delta.x),
            y: encodeNumber(r.delta.y),
            z: encodeNumber(r.delta.z),
          },
        }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

// --- Spec 002 Task 2: angles -------------------------------------------------
//
// Angle conversion is one multiplication by a single constant, so parity is
// asserted on the EXACT bits, exactly like length conversion.
for (const c of [...cases.angles.valid, ...cases.angles.invalid]) {
  const r = toRadians({ value: decodeValue(c.value), unit: c.unit });
  verdicts.push({
    group: "angle",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { radians: encodeNumber(r.radians) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of cases.angles.malformed) {
  const r = toRadians(decodeValue(c.measurement));
  verdicts.push({
    group: "angle-malformed",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { radians: encodeNumber(r.radians) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

// --- Spec 002 Task 2: resize -------------------------------------------------
//
// One correctly-rounded division by 100, so exact-bit parity again.
for (const c of [...cases.resize.valid, ...cases.resize.invalid]) {
  const r = resizeFactor(decodeValue(c.percent), c.direction);
  verdicts.push({
    group: "resize",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { factor: encodeNumber(r.factor) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of [...cases.resize.factors.valid, ...cases.resize.factors.invalid]) {
  const r = validateSizeFactor(decodeValue(c.value));
  verdicts.push({
    group: "size-factor",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? { factor: encodeNumber(r.factor) }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

// --- Spec 002 Task 2: colour -------------------------------------------------
//
// Validation verdicts and PALETTE values are compared exactly: the palette is
// authored in canonical linear literals, so no `pow` is involved. Transfer-function
// output is compared by the Python side within the corpus tolerance, because `pow`
// is not guaranteed bit-identical across libm implementations.
for (const c of [...cases.colors.valid, ...cases.colors.invalid]) {
  const r = validateMaterialColor(decodeColor(c.color));
  verdicts.push({
    group: "color",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? {
          color: {
            r: encodeNumber(r.color.r),
            g: encodeNumber(r.color.g),
            b: encodeNumber(r.color.b),
            a: encodeNumber(r.color.a),
          },
        }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of [...cases.colors.named, ...cases.colors.unnamed]) {
  const r = resolveNamedColor(c.input);
  verdicts.push({
    group: "named-color",
    case: c.name,
    ok: r.ok,
    ...(r.ok
      ? {
          color: {
            r: encodeNumber(r.color.r),
            g: encodeNumber(r.color.g),
            b: encodeNumber(r.color.b),
            a: encodeNumber(r.color.a),
          },
        }
      : { error_code: r.error.code, error_message: r.error.message }),
  });
}

for (const c of cases.colors.transfer.cases) {
  const linear = srgbEncodedChannelToLinear(c.encoded);
  verdicts.push({
    group: "srgb-transfer",
    case: c.name,
    ok: linear !== undefined,
    ...(linear === undefined ? {} : { linear: encodeNumber(linear) }),
  });
}

for (const c of cases.colors.transfer.rejected) {
  verdicts.push({
    group: "srgb-transfer-rejected",
    case: c.name,
    ok: srgbEncodedChannelToLinear(decodeValue(c.encoded)) !== undefined,
  });
}

/**
 * Deterministic codepoint ordering.
 *
 * `localeCompare` is locale-sensitive (it orders "base" before "C:") and would
 * disagree with Python's default string sort, producing false parity failures.
 */
function byKey(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

verdicts.sort((a, b) => byKey(`${a.group}::${a.case}`, `${b.group}::${b.case}`));

process.stdout.write(JSON.stringify(verdicts, null, 2));

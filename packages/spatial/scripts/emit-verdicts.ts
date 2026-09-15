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
  resolveDirection,
  toMeters,
} from "../src/index.ts";
import { decodeValue, loadSpatialCases } from "../src/test-support.ts";

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

verdicts.sort((a, b) =>
  `${a.group}::${a.case}`.localeCompare(`${b.group}::${b.case}`),
);

process.stdout.write(JSON.stringify(verdicts, null, 2));

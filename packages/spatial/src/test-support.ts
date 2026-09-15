/**
 * Test support: load the shared language-neutral spatial corpus.
 *
 * JSON cannot encode NaN or Infinity, so the corpus uses string sentinels which
 * each language decodes into its own non-finite values. Both representations
 * therefore exercise identical inputs.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const CASES_PATH = join(HERE, "..", "spatial-cases.json");

export interface ConversionCase {
  name: string;
  value?: unknown;
  unit?: string;
  meters?: number;
  measurement?: unknown;
  error_code?: string;
  error_key?: string;
}

export interface DirectionCase {
  name: string;
  direction: unknown;
  axis?: string;
  sign?: number;
  error_code?: string;
}

export interface DeltaCase {
  name: string;
  direction: unknown;
  value?: unknown;
  unit?: string;
  delta?: { x: number; y: number; z: number };
  axis?: string;
  meters?: number;
  error_code?: string;
  error_key?: string;
}

/** Spec 002 Task 2: angle conversion. */
export interface AngleCase {
  name: string;
  value?: unknown;
  unit?: unknown;
  radians?: number;
  measurement?: unknown;
  error_code?: string;
  error_key?: string;
}

/** Spec 002 Task 2: normalized resize intent -> absolute factor. */
export interface ResizeCase {
  name: string;
  percent?: unknown;
  direction?: unknown;
  factor?: number;
  error_code?: string;
  error_key?: string;
}

/** Spec 002 Task 2: explicit size-factor validation. */
export interface SizeFactorCase {
  name: string;
  value: unknown;
  factor?: number;
  error_code?: string;
  error_key?: string;
}

/** Spec 002 Task 2: canonical colour validation and palette lookup. */
export interface ColorCase {
  name: string;
  color?: unknown;
  input?: unknown;
  error_code?: string;
  error_key?: string;
}

/** Spec 002 Task 2: encoded sRGB -> linear reference vectors. */
export interface TransferCase {
  name: string;
  encoded: unknown;
  linear?: number;
}

export interface SpatialCases {
  conversion: {
    valid: ConversionCase[];
    invalid: ConversionCase[];
    malformed: ConversionCase[];
  };
  directions: { valid: DirectionCase[]; invalid: DirectionCase[] };
  deltas: { valid: DeltaCase[]; invalid: DeltaCase[] };
  angles: { valid: AngleCase[]; invalid: AngleCase[]; malformed: AngleCase[] };
  resize: {
    valid: ResizeCase[];
    invalid: ResizeCase[];
    factors: { valid: SizeFactorCase[]; invalid: SizeFactorCase[] };
  };
  colors: {
    valid: ColorCase[];
    invalid: ColorCase[];
    named: (ColorCase & { color: Record<string, number> })[];
    unnamed: ColorCase[];
    transfer: {
      tolerance: number;
      cases: TransferCase[];
      rejected: TransferCase[];
    };
  };
}

export function loadSpatialCases(): SpatialCases {
  return JSON.parse(readFileSync(CASES_PATH, "utf8")) as SpatialCases;
}

/** Decode corpus sentinels into real non-finite values. */
export function decodeValue(value: unknown): unknown {
  if (value === "__NAN__") return NaN;
  if (value === "__INF__") return Infinity;
  if (value === "__NEG_INF__") return -Infinity;
  return value;
}

/**
 * Decode sentinels inside a colour object.
 *
 * A colour case carries its non-finite channel as a sentinel, so the object has to
 * be walked rather than passed through `decodeValue`, which only handles scalars.
 */
export function decodeColor(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return decodeValue(value);
  }
  const out: Record<string, unknown> = {};
  for (const [key, channel] of Object.entries(value as Record<string, unknown>)) {
    out[key] = decodeValue(channel);
  }
  return out;
}

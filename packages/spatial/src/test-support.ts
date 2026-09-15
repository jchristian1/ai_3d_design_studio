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

export interface SpatialCases {
  conversion: {
    valid: ConversionCase[];
    invalid: ConversionCase[];
    malformed: ConversionCase[];
  };
  directions: { valid: DirectionCase[]; invalid: DirectionCase[] };
  deltas: { valid: DeltaCase[]; invalid: DeltaCase[] };
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

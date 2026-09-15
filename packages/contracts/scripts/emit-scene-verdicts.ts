/**
 * Emit the TypeScript result for every vector in the shared scene corpus.
 *
 * Used by tests/contracts/test_scene_digest_cross_language_parity.py to prove the
 * two implementations of the AUTHORITATIVE scene-version digest agree exactly:
 * the same canonical JSON bytes and the same hex digest. A one-sided change to
 * quantisation, ordering, key sorting, escaping, or projection membership fails
 * there even if it slips past the per-language tests.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  SCENE_DIGEST_VERSION,
  canonicalDigestJson,
  computeSceneVersion,
  formatDigestNumber,
  objectSortKey,
  sceneDigestProjection,
} from "../src/index.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const cases = JSON.parse(
  readFileSync(join(HERE, "..", "scene-cases.json"), "utf8"),
) as {
  numbers: {
    cases: { name: string; value: unknown }[];
    rejected: { name: string; value: unknown }[];
  };
  scenes: { name: string; scene: unknown }[];
  rejected_scenes: { name: string; scene: unknown }[];
};

interface Verdict {
  group: string;
  case: string;
  [key: string]: unknown;
}

const verdicts: Verdict[] = [];

verdicts.push({
  group: "digest_version",
  case: "constant",
  value: SCENE_DIGEST_VERSION,
});

for (const c of cases.numbers.cases) {
  verdicts.push({
    group: "number",
    case: c.name,
    formatted: formatDigestNumber(c.value),
  });
}

for (const c of cases.numbers.rejected) {
  let threw = false;
  try {
    formatDigestNumber(c.value);
  } catch {
    threw = true;
  }
  verdicts.push({ group: "number_rejected", case: c.name, threw });
}

for (const c of cases.scenes) {
  verdicts.push({
    group: "scene",
    case: c.name,
    canonical_json: canonicalDigestJson(sceneDigestProjection(c.scene)),
    scene_version: computeSceneVersion(c.scene),
    sort_keys: ((c.scene as Record<string, unknown>).objects as unknown[]).map((o) =>
      objectSortKey(o),
    ),
  });
}

for (const c of cases.rejected_scenes) {
  let threw = false;
  try {
    computeSceneVersion(c.scene);
  } catch {
    threw = true;
  }
  verdicts.push({ group: "scene_rejected", case: c.name, threw });
}

/**
 * Deterministic codepoint ordering. `localeCompare` is locale-sensitive and would
 * disagree with Python's default string sort, producing false parity failures.
 */
function byKey(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

verdicts.sort((a, b) => byKey(`${a.group}::${a.case}`, `${b.group}::${b.case}`));

process.stdout.write(JSON.stringify(verdicts, null, 2));

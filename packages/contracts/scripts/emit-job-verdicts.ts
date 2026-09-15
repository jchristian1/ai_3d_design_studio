/**
 * Emit the TypeScript result for every case in the shared job corpus and the
 * canonical job conformance corpus.
 *
 * Used by tests/jobs/test_jobs_cross_language_parity.py to prove the TypeScript
 * and Python job layers agree on canonical encodings, derived idempotency keys,
 * lifecycle transitions, and job validation verdicts.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  SCHEMA_DIR,
  SCHEMA_FILES,
  canTransition,
  canonicalize,
  classifyDelivery,
  deriveContentFingerprint,
  deriveIdempotencyKey,
  validateJob,
} from "../src/index.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const jobCases = JSON.parse(
  readFileSync(join(HERE, "..", "job-cases.json"), "utf8"),
) as any;
const conformance = JSON.parse(
  readFileSync(join(SCHEMA_DIR, "conformance-cases.json"), "utf8"),
) as any;

interface Verdict {
  group: string;
  case: string;
  [key: string]: unknown;
}

const verdicts: Verdict[] = [];

// Canonical encodings must be byte-identical.
for (const c of jobCases.canonicalize.cases) {
  verdicts.push({
    group: "canonicalize",
    case: c.name,
    encoded: canonicalize(c.value),
  });
}

// Non-finite values must be refused in both languages.
for (const c of jobCases.canonicalize.rejected) {
  const value =
    c.value === "__NAN__"
      ? NaN
      : c.value === "__INF__"
        ? Infinity
        : -Infinity;
  let threw = false;
  try {
    canonicalize(value);
  } catch {
    threw = true;
  }
  verdicts.push({ group: "canonicalize-rejected", case: c.name, threw });
}

// Derived idempotency keys must match exactly (origin-based).
const base = jobCases.idempotency.base;
verdicts.push({
  group: "idempotency",
  case: "base",
  key: deriveIdempotencyKey(base),
});
for (const v of jobCases.idempotency.different_key_variants) {
  verdicts.push({
    group: "idempotency",
    case: v.name,
    key: deriveIdempotencyKey({ ...base, [v.field]: v.value }),
  });
}
for (const v of jobCases.idempotency.rejected) {
  let threw = false;
  try {
    deriveIdempotencyKey({ ...base, [v.field]: v.value });
  } catch {
    threw = true;
  }
  verdicts.push({ group: "idempotency-rejected", case: v.name, threw });
}

// Diagnostic content fingerprints must match exactly.
const fp = jobCases.content_fingerprint;
verdicts.push({
  group: "fingerprint",
  case: "base",
  fingerprint: deriveContentFingerprint(fp.job_type, fp.payload),
});
for (const v of [...fp.different_payloads, ...fp.equivalent_payloads]) {
  verdicts.push({
    group: "fingerprint",
    case: v.name,
    fingerprint: deriveContentFingerprint(fp.job_type, v.payload),
  });
}

// Duplicate-delivery decisions must match.
for (const c of jobCases.delivery.cases) {
  verdicts.push({
    group: "delivery",
    case: c.name,
    decision: classifyDelivery(c.recorded_status),
  });
}

// Lifecycle transitions must agree.
for (const t of [
  ...jobCases.transitions.allowed,
  ...jobCases.transitions.rejected,
]) {
  verdicts.push({
    group: "transition",
    case: `${t.from}->${t.to}`,
    allowed: canTransition(t.from, t.to),
  });
}

// Job validation verdicts must agree, including the error codes produced.
const jobSuite = conformance.suites.find(
  (s: any) => s.schema === SCHEMA_FILES.Job,
);
for (const kind of ["valid", "invalid"] as const) {
  for (const c of jobSuite[kind]) {
    const result = validateJob(c.data);
    verdicts.push({
      group: `validate-${kind}`,
      case: c.name,
      valid: result.valid,
      codes: [...new Set(result.errors.map((e) => e.code))].sort(),
    });
  }
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

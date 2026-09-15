/**
 * Emit the TypeScript verdict for every case in the shared conformance corpus.
 *
 * Used by tests/contracts/test_cross_language_parity.py to prove the TypeScript
 * and Python representations reach IDENTICAL conclusions about the canonical
 * contracts. Output is a stable, sorted JSON array on stdout.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { SCHEMA_DIR, SCHEMA_FILES, validateAgainstSchema } from "../src/index.ts";
import { validateChatRequest } from "@studio/validation";

interface Case {
  name: string;
  data: unknown;
}
interface Suite {
  schema: string;
  valid: Case[];
  invalid: Case[];
}

const corpus = JSON.parse(
  readFileSync(join(SCHEMA_DIR, "conformance-cases.json"), "utf8"),
) as { suites: Suite[] };

interface Verdict {
  schema: string;
  case: string;
  expected: "valid" | "invalid";
  schema_valid: boolean;
  validation_valid: boolean | null;
}

const verdicts: Verdict[] = [];

for (const suite of corpus.suites) {
  for (const expected of ["valid", "invalid"] as const) {
    for (const c of suite[expected]) {
      verdicts.push({
        schema: suite.schema,
        case: c.name,
        expected,
        schema_valid: validateAgainstSchema(suite.schema, c.data).valid,
        validation_valid:
          suite.schema === SCHEMA_FILES.ChatRequest
            ? validateChatRequest(c.data).valid
            : null,
      });
    }
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

verdicts.sort((a, b) => byKey(`${a.schema}::${a.case}`, `${b.schema}::${b.case}`));

process.stdout.write(JSON.stringify(verdicts, null, 2));

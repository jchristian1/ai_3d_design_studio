/**
 * Single-conversion-site guard — TypeScript runtime code (Spec 002, Task 2).
 *
 * The counterpart to tests/spatial/test_conversion_site_guard.py. The invariant is
 * language-neutral — cm->m, deg->rad, percent->factor and encoded sRGB->linear exist
 * exactly once each, in @studio/spatial — so it has to be enforced on both sides. A
 * duplicate implementation in the browser would be just as capable of disagreeing
 * with the platform as one in a service.
 *
 * There is no TypeScript AST available here without adding a parser dependency, and
 * adding one to enforce a rule would be a poor trade. Instead the scan strips
 * comments and string literals first and then looks for the arithmetic SHAPES, so
 * the two things that would otherwise make a text scan brittle — prose in a comment
 * and a number inside a string — cannot produce a false positive. Every pattern is
 * proved to fire against synthetic violating source, and proved not to fire against
 * real legitimate source, in the same way as the Python guard.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..", "..");

/** The approved home of every conversion. Not scanned. */
const APPROVED_DIR = join(REPO_ROOT, "packages", "spatial", "src");

/**
 * Runtime TypeScript roots. Tests are excluded: a test may legitimately compute an
 * expected value by hand.
 */
const RUNTIME_ROOTS = [
  join(REPO_ROOT, "apps", "web", "lib"),
  join(REPO_ROOT, "apps", "web", "components"),
  join(REPO_ROOT, "apps", "web", "app"),
  join(REPO_ROOT, "packages", "contracts", "src"),
  join(REPO_ROOT, "packages", "types", "src"),
  join(REPO_ROOT, "packages", "validation", "src"),
];

function collect(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...collect(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry)) continue;
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (entry === "test-support.ts") continue;
    if (full.startsWith(APPROVED_DIR)) continue;
    out.push(full);
  }
  return out;
}

const RUNTIME_FILES = RUNTIME_ROOTS.flatMap(collect);

/** Remove comments only, keeping string literals. */
function stripComments(source: string): string {
  let out = "";
  let i = 0;
  while (i < source.length) {
    const two = source.slice(i, i + 2);
    if (two === "//") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (two === "/*") {
      i += 2;
      while (i < source.length && source.slice(i, i + 2) !== "*/") i += 1;
      i += 2;
      continue;
    }
    const char = source[i];
    if (char === '"' || char === "'" || char === "`") {
      const quote = char;
      out += char;
      i += 1;
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\\") {
          out += source[i];
          i += 1;
        }
        out += source[i];
        i += 1;
      }
      out += quote;
      i += 1;
      continue;
    }
    out += char;
    i += 1;
  }
  return out;
}

/**
 * Remove comments AND string/template literals.
 *
 * This is what makes the arithmetic scan trustworthy rather than brittle: after
 * stripping, a comment explaining "convert 180 degrees" and a message containing
 * "100" are gone, so only executable arithmetic remains.
 */
function stripCommentsAndStrings(source: string): string {
  let out = "";
  let i = 0;
  while (i < source.length) {
    const two = source.slice(i, i + 2);
    if (two === "//") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (two === "/*") {
      i += 2;
      while (i < source.length && source.slice(i, i + 2) !== "*/") i += 1;
      i += 2;
      continue;
    }
    const char = source[i];
    if (char === '"' || char === "'" || char === "`") {
      const quote = char;
      i += 1;
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\\") i += 1;
        i += 1;
      }
      i += 1;
      out += '""';
      continue;
    }
    out += char;
    i += 1;
  }
  return out;
}

interface Pattern {
  name: string;
  regex: RegExp;
  hint: string;
  /**
   * How much of the source the pattern is applied to.
   *
   * `code` strips comments and string literals, which is right for arithmetic: a
   * number inside a message is not a conversion.
   *
   * `code-and-strings` strips comments only. A colour-name table's keys ARE string
   * literals, so stripping them would make the very violation invisible. Comments
   * are still stripped, so documentation that mentions the colour is fine.
   */
  scope: "code" | "code-and-strings";
}

const PATTERNS: Pattern[] = [
  {
    name: "division by 100",
    regex: /\/\s*100(?![0-9.])/,
    hint: "unit or percentage conversion belongs to @studio/spatial",
    scope: "code",
  },
  {
    name: "multiplication by 0.01",
    scope: "code",
    regex: /\*\s*0\.01(?![0-9])|0\.01\s*\*/,
    hint: "the double-rounding cm->m form Spec 001 rejected",
  },
  {
    name: "division by 255",
    scope: "code",
    regex: /\/\s*255(?![0-9.])/,
    hint: "dividing by 255 is not an sRGB transfer function",
  },
  {
    name: "pi reference",
    scope: "code",
    regex: /Math\.PI/,
    hint: "angle conversion belongs to @studio/spatial",
  },
  {
    name: "degree constant",
    scope: "code",
    regex: /\/\s*180(?![0-9.])|180\s*\/(?!\s*180)/,
    hint: "degree/radian arithmetic belongs to @studio/spatial",
  },
  {
    name: "srgb transfer constants",
    scope: "code",
    regex: /1\.055|12\.92|0\.04045|0\.0031308/,
    hint: "use srgbEncodedChannelToLinear",
  },
  {
    name: "srgb exponent",
    scope: "code",
    regex: /\*\*\s*2\.4|Math\.pow\([^)]*,\s*2\.4\s*\)/,
    hint: "use srgbEncodedChannelToLinear",
  },
  {
    name: "palette name",
    regex: /warm\s+beige/i,
    hint: "the named palette lives in @studio/spatial only",
    scope: "code-and-strings",
  },
];

test("the scan covers a plausible number of files", () => {
  // A guard that silently scans nothing passes forever.
  assert.ok(
    RUNTIME_FILES.length >= 15,
    `only scanned ${RUNTIME_FILES.length} files`,
  );
  const names = new Set(RUNTIME_FILES.map((f) => f.split("/").pop()));
  for (const expected of ["client.ts", "reducer.ts", "index.ts"]) {
    assert.ok(names.has(expected), `scan missed ${expected}`);
  }
});

test("the approved package is excluded from the scan", () => {
  assert.ok(!RUNTIME_FILES.some((file) => file.startsWith(APPROVED_DIR)));
});

function prepare(source: string, scope: Pattern["scope"]): string {
  return scope === "code" ? stripCommentsAndStrings(source) : stripComments(source);
}

for (const pattern of PATTERNS) {
  test(`no runtime TypeScript contains: ${pattern.name}`, () => {
    const offenders: string[] = [];
    for (const file of RUNTIME_FILES) {
      const stripped = prepare(readFileSync(file, "utf8"), pattern.scope);
      stripped.split("\n").forEach((line, index) => {
        if (pattern.regex.test(line)) {
          offenders.push(`${file.replace(REPO_ROOT, "")}:${index + 1}: ${line.trim()}`);
        }
      });
    }
    assert.deepEqual(offenders, [], `${pattern.hint}\n${offenders.join("\n")}`);
  });
}

// ---------------------------------------------------------------------------
// Every pattern must actually fire, and must not fire on legitimate code
// ---------------------------------------------------------------------------

function matches(name: string, source: string): boolean {
  const pattern = PATTERNS.find((p) => p.name === name);
  assert.ok(pattern, `no pattern named ${name}`);
  return pattern.regex.test(prepare(source, pattern.scope));
}

for (const [name, source] of [
  ["division by 100", "const meters = value / 100;"],
  ["division by 100", "const factor = (100 - percent) / 100;"],
  ["multiplication by 0.01", "const meters = value * 0.01;"],
  ["division by 255", "const linear = value / 255;"],
  ["pi reference", "const r = degrees * Math.PI / 180;"],
  ["degree constant", "const r = (degrees / 180) * pi;"],
  ["srgb transfer constants", "const linear = e / 12.92;"],
  ["srgb transfer constants", "if (e <= 0.04045) return e;"],
  ["srgb exponent", "const linear = ((e + 0.055) / 1.055) ** 2.4;"],
  ["srgb exponent", "const linear = Math.pow(x, 2.4);"],
  ["palette name", 'const PALETTE = { "warm beige": 1 };'],
] as [string, string][]) {
  test(`pattern '${name}' flags: ${source}`, () => {
    assert.ok(matches(name, source), "pattern missed a real violation");
  });
}

for (const [name, source] of [
  // The false positives the guard must NOT produce.
  ["division by 100", 'const label = "100% complete";'],
  ["division by 100", "const MAX_ATTEMPTS = 100;"],
  ["division by 100", "// divide by 100 to get metres"],
  ["division by 100", "const half = budget / 2;"],
  ["division by 100", "const ms = seconds / 1000;"],
  ["palette name", "// resolves a warm beige by name"],
  ["palette name", "/* the warm beige entry is canonical */"],
  ["srgb transfer constants", '/* set 2.4 -> read 2.4000000953674316 */'],
  ["degree constant", 'const alt = "rotate 180 degrees";'],
  ["pi reference", "// uses Math.PI in @studio/spatial"],
] as [string, string][]) {
  test(`pattern '${name}' ignores: ${source}`, () => {
    assert.ok(!matches(name, source), "false positive");
  });
}

test("the approved package really does contain the conversions", () => {
  const source = readdirSync(APPROVED_DIR)
    .filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts"))
    .map((f) => readFileSync(join(APPROVED_DIR, f), "utf8"))
    .join("\n");
  for (const expected of [
    "CM_PER_METER",
    "RADIANS_PER_DEGREE",
    "PERCENT_WHOLE",
    "SRGB_LINEAR_THRESHOLD",
    "NAMED_COLORS",
  ]) {
    assert.ok(source.includes(expected), `${expected} is not defined in the package`);
  }
});

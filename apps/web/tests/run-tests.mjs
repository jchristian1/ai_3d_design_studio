/**
 * Per-file test runner for the web app.
 *
 * WHY THIS EXISTS: `node --test tests/*.test.ts` runs every test file in ONE
 * process. Each UI file installs a full jsdom window/document plus a React tree,
 * and the esbuild/tsx transform cache grows with every module. Stacked in a
 * single heap across all files, peak memory climbed until systemd-oomd killed
 * the terminal (see the Sep-17 OOM incident). Running one file per child process
 * caps peak memory at a single file's environment: when the child exits, the OS
 * reclaims everything.
 *
 * Each child also gets a hard heap cap (--max-old-space-size). If a genuine leak
 * ever reappears, Node throws a catchable OOM inside the child instead of letting
 * the heap balloon until the OS OOM-killer reaps the whole terminal session.
 *
 * A specific file (or files) can be passed as arguments; otherwise every
 * tests/*.test.ts file runs.
 */
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const testsDir = dirname(fileURLToPath(import.meta.url));

// Per-child heap cap in MB. Comfortably above one jsdom+React file, far below
// what it takes to exhaust the machine. Override with WEB_TEST_HEAP_MB.
const heapMb = Number.parseInt(process.env.WEB_TEST_HEAP_MB ?? "1024", 10);

const args = process.argv.slice(2);
const files =
  args.length > 0
    ? args.map((f) => resolve(process.cwd(), f))
    : readdirSync(testsDir)
        .filter((name) => name.endsWith(".test.ts"))
        .sort()
        .map((name) => join(testsDir, name));

if (files.length === 0) {
  console.error("No test files found.");
  process.exit(1);
}

const registerHooks = join(testsDir, "register-hooks.mjs");
let failed = 0;

for (const file of files) {
  const rel = file.replace(`${process.cwd()}/`, "");
  console.log(`\n=== ${rel} ===`);
  const result = spawnSync(
    process.execPath,
    [
      `--max-old-space-size=${heapMb}`,
      "--import",
      "tsx",
      "--import",
      registerHooks,
      "--test",
      file,
    ],
    { stdio: "inherit", cwd: process.cwd() },
  );
  if (result.status !== 0) {
    failed += 1;
    console.error(`FAILED (exit ${result.status ?? "signal " + result.signal}): ${rel}`);
  }
}

if (failed > 0) {
  console.error(`\n${failed} test file(s) failed.`);
  process.exit(1);
}
console.log(`\nAll ${files.length} test file(s) passed.`);

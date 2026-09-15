/**
 * Scene contract + scene-version digest tests — TypeScript (Spec 002, Task 1).
 *
 * The same four things test_scene.py proves, asserted against the same canonical
 * schemas and the same shared vector corpus:
 *
 *   1. the `studio-scene-v1` projection is deterministic for unchanged semantic
 *      state and sensitive to every planning-relevant change;
 *   2. what the digest excludes really is excluded;
 *   3. unsafe fields are UNREPRESENTABLE — the documents really carry a path, a
 *      hostname, a token, a script, and the schema really refuses them;
 *   4. the projection is an allow-list, so a future informational field cannot
 *      silently change concurrency semantics.
 *
 * tests/contracts/test_scene_digest_cross_language_parity.py then proves this
 * implementation and the Python one produce byte-identical canonical JSON.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DIGEST_DECIMALS,
  FORBIDDEN_SCENE_FIELDS,
  SCENE_DIGEST_VERSION,
  SCENE_OBJECT_DIGEST_FIELDS,
  SCENE_OBJECT_INFORMATIONAL_FIELDS,
  SCENE_SNAPSHOT_DIGEST_FIELDS,
  SCENE_SNAPSHOT_INFORMATIONAL_FIELDS,
  SCENE_UNITS_DIGEST_FIELDS,
  SCHEMA_FILES,
  SceneDigestError,
  canonicalDigestJson,
  computeSceneVersion,
  formatDigestNumber,
  loadSchema,
  objectSortKey,
  sceneDigestProjection,
  sceneVersionsMatch,
  schemaProperties,
  schemaRequired,
  toWire,
  validateAgainstSchema,
  validateSceneSnapshot,
} from "./index.ts";
import {
  JOB_TYPES,
  MUTATING_JOB_TYPES,
  READ_JOB_TYPES,
  isMutatingJobType,
} from "@studio/types";
import type {
  EulerRadians,
  MaterialColor,
  MaterialSummary,
  Scale3,
  SceneObject,
  SceneSnapshot,
  SceneUnits,
} from "@studio/types";

const HERE = dirname(fileURLToPath(import.meta.url));
const CASES = JSON.parse(
  readFileSync(join(HERE, "..", "scene-cases.json"), "utf8"),
) as {
  numbers: {
    cases: { name: string; value: unknown; expected: string }[];
    rejected: { name: string; value: unknown }[];
  };
  scenes: { name: string; scene: unknown }[];
  rejected_scenes: { name: string; scene: unknown }[];
};

const FAKE_VERSION = `sha256:${"a".repeat(64)}`;

type Dict = Record<string, unknown>;

function units(over: Dict = {}): Dict {
  return { unit_system: "METRIC", length_unit: "METERS", scale_length: 1.0, ...over };
}

function cube(over: Dict = {}): Dict {
  return {
    studio_object_id: "obj_cube001",
    name: "Cube",
    object_type: "MESH",
    world_position_meters: { x: 0, y: 0, z: 0 },
    dimensions_meters: { x: 2, y: 2, z: 2 },
    rotation_euler_radians: { x: 0, y: 0, z: 0 },
    scale: { x: 1, y: 1, z: 1 },
    visible: true,
    ...over,
  };
}

function scene(objects: Dict[] = [cube()], over: Dict = {}): Dict {
  return { units: units(), objects, ...over };
}

function snapshot(objects: Dict[] = [cube()], over: Dict = {}): Dict {
  const body = scene(objects);
  return {
    project_id: "proj_seed",
    scene_version: computeSceneVersion(body),
    units: body.units,
    objects: body.objects,
    captured_at: "2026-09-15T04:00:00Z",
    ...over,
  };
}

/** A snapshot-shaped document with a PLACEHOLDER version, for scenes that cannot
 * be digested at all (NaN coordinate, ambiguous ordering). */
function rawSnapshot(objects: Dict[] = [cube()], over: Dict = {}): Dict {
  const body = scene(objects);
  return {
    project_id: "proj_seed",
    scene_version: FAKE_VERSION,
    units: body.units,
    objects: body.objects,
    captured_at: "2026-09-15T04:00:00Z",
    ...over,
  };
}

/**
 * First element, or a hard failure. `noUncheckedIndexedAccess` is on, and a test
 * that silently indexed past the end would assert nothing.
 */
function first<T>(items: T[]): T {
  const [head] = items;
  if (head === undefined) {
    throw new Error("expected at least one item");
  }
  return head;
}

/** The projected objects of a digest projection. */
function projected(projection: Dict): Dict[] {
  return projection.objects as Dict[];
}

function withoutKey(source: Dict, key: string): Dict {
  const copy = { ...source };
  delete copy[key];
  return copy;
}

// ---------------------------------------------------------------------------
// Numeric representation
// ---------------------------------------------------------------------------

for (const [value, expected] of [
  [0, "0.000000"],
  [-0, "0.000000"],
  [0.5, "0.500000"],
  [1, "1.000000"],
  [2.0, "2.000000"],
  [1.6, "1.600000"],
  [-0.25, "-0.250000"],
  [Math.PI / 4, "0.785398"],
  [Math.PI / 2, "1.570796"],
  [1e-9, "0.000000"],
  [-1e-9, "0.000000"],
  [1234.567891, "1234.567891"],
] as [number, string][]) {
  test(`digest number ${value} formats as ${expected}`, () => {
    assert.equal(formatDigestNumber(value), expected);
  });
}

test("digest numbers always carry the documented decimals", () => {
  assert.equal(formatDigestNumber(1).split(".")[1], "0".repeat(DIGEST_DECIMALS));
});

test("negative zero cannot produce a different version", () => {
  const positive = scene([cube({ rotation_euler_radians: { x: 0, y: 0, z: 0 } })]);
  const negative = scene([cube({ rotation_euler_radians: { x: -0, y: -0, z: -0 } })]);
  assert.equal(computeSceneVersion(positive), computeSceneVersion(negative));
});

for (const value of [NaN, Infinity, -Infinity]) {
  test(`non-finite ${value} is refused, not serialized`, () => {
    assert.throws(() => formatDigestNumber(value), SceneDigestError);
    assert.throws(
      () =>
        computeSceneVersion(
          scene([cube({ world_position_meters: { x: value, y: 0, z: 0 } })]),
        ),
      SceneDigestError,
    );
  });
}

for (const value of ["0.5", null, true, [0.5]]) {
  test(`non-numeric ${JSON.stringify(value)} is refused`, () => {
    assert.throws(() => formatDigestNumber(value), SceneDigestError);
  });
}

// ---------------------------------------------------------------------------
// The projection shape
// ---------------------------------------------------------------------------

test("projection carries the digest version inside the payload", () => {
  const projection = sceneDigestProjection(scene());
  assert.equal(projection.digest_version, SCENE_DIGEST_VERSION);
  assert.ok(canonicalDigestJson(projection).includes(SCENE_DIGEST_VERSION));
});

test("projection needs only units and objects", () => {
  assert.ok(
    computeSceneVersion({ units: units(), objects: [cube()] }).startsWith("sha256:"),
  );
});

test("projection emits explicit null for absent optionals", () => {
  const projection = sceneDigestProjection(
    scene([withoutKey(cube(), "studio_object_id")]),
  );
  const entry = first(projected(projection));
  assert.equal(entry.studio_object_id, null);
  assert.equal(entry.material, null);
});

test("projection covers exactly the declared object fields", () => {
  const entry = first(projected(sceneDigestProjection(scene())));
  assert.deepEqual(Object.keys(entry).sort(), [...SCENE_OBJECT_DIGEST_FIELDS].sort());
});

test("projection covers exactly the declared unit and snapshot fields", () => {
  const projection = sceneDigestProjection(scene());
  assert.deepEqual(
    Object.keys(projection.units as object).sort(),
    [...SCENE_UNITS_DIGEST_FIELDS].sort(),
  );
  assert.deepEqual(
    Object.keys(projection).sort(),
    ["digest_version", ...SCENE_SNAPSHOT_DIGEST_FIELDS].sort(),
  );
});

test("canonical JSON is sorted and compact", () => {
  assert.equal(
    canonicalDigestJson({ b: 1, a: { d: 2, c: 3 } }),
    '{"a":{"c":3,"d":2},"b":1}',
  );
});

test("canonical JSON does not escape non-ASCII", () => {
  assert.equal(canonicalDigestJson({ name: "Küche" }), '{"name":"Küche"}');
});

test("projection serializes numbers as fixed-decimal strings", () => {
  const entry = first(projected(sceneDigestProjection(scene())));
  assert.deepEqual(entry.world_position_meters, [
    "0.000000",
    "0.000000",
    "0.000000",
  ]);
  assert.deepEqual(entry.dimensions_meters, ["2.000000", "2.000000", "2.000000"]);
});

// ---------------------------------------------------------------------------
// The eleven required digest properties
// ---------------------------------------------------------------------------

test("1. repeated inspection of an unchanged scene gives the same version", () => {
  assert.equal(
    computeSceneVersion(snapshot()),
    computeSceneVersion(snapshot([cube()], { captured_at: "2026-09-15T05:30:00Z" })),
  );
});

test("2. captured_at does not change the scene version", () => {
  const base = snapshot();
  assert.equal(
    computeSceneVersion({ ...base, captured_at: "2027-01-01T00:00:00Z" }),
    computeSceneVersion(base),
  );
});

test("3. object enumeration order does not change the scene version", () => {
  const table = cube({ studio_object_id: "obj_table001", name: "Table" });
  const chair = cube({ studio_object_id: "obj_chair001", name: "Chair" });
  assert.equal(
    computeSceneVersion(scene([table, chair])),
    computeSceneVersion(scene([chair, table])),
  );
});

test("4. position change changes the scene version", () => {
  assert.notEqual(
    computeSceneVersion(scene([cube({ world_position_meters: { x: 0.5, y: 0, z: 0 } })])),
    computeSceneVersion(scene()),
  );
});

test("5. dimensions change changes the scene version", () => {
  assert.notEqual(
    computeSceneVersion(scene([cube({ dimensions_meters: { x: 1.6, y: 1.6, z: 1.6 } })])),
    computeSceneVersion(scene()),
  );
});

test("6. rotation change changes the scene version", () => {
  assert.notEqual(
    computeSceneVersion(
      scene([cube({ rotation_euler_radians: { x: 0, y: 0, z: Math.PI / 4 } })]),
    ),
    computeSceneVersion(scene()),
  );
});

test("7. scale change changes the scene version", () => {
  assert.notEqual(
    computeSceneVersion(scene([cube({ scale: { x: 0.8, y: 0.8, z: 0.8 } })])),
    computeSceneVersion(scene()),
  );
});

test("8. exposed material colour change changes the scene version", () => {
  const before = scene([
    cube({ material: { name: "Beige", base_color: { r: 0.76, g: 0.66, b: 0.5, a: 1 } } }),
  ]);
  const after = scene([
    cube({ material: { name: "Beige", base_color: { r: 0.2, g: 0.66, b: 0.5, a: 1 } } }),
  ]);
  assert.notEqual(computeSceneVersion(after), computeSceneVersion(before));
});

test("8b. material presence and name participate too", () => {
  const versions = new Set([
    computeSceneVersion(scene([cube()])),
    computeSceneVersion(scene([cube({ material: { name: "Beige" } })])),
    computeSceneVersion(scene([cube({ material: { name: "Oak" } })])),
  ]);
  assert.equal(versions.size, 3);
});

test("8c. absent base colour is not the same as black", () => {
  assert.notEqual(
    computeSceneVersion(scene([cube({ material: { name: "Procedural" } })])),
    computeSceneVersion(
      scene([
        cube({
          material: { name: "Procedural", base_color: { r: 0, g: 0, b: 0, a: 1 } },
        }),
      ]),
    ),
  );
});

test("9. visibility change changes the scene version", () => {
  assert.notEqual(
    computeSceneVersion(scene([cube({ visible: false })])),
    computeSceneVersion(scene([cube({ visible: true })])),
  );
});

test("10. adding or removing an object changes the scene version", () => {
  const versions = new Set([
    computeSceneVersion(scene([cube()])),
    computeSceneVersion(
      scene([cube(), cube({ studio_object_id: "obj_chair001", name: "Chair" })]),
    ),
    computeSceneVersion(scene([])),
  ]);
  assert.equal(versions.size, 3);
});

test("11. excluded metadata change does not change the scene version", () => {
  const base = snapshot();
  const noisy = {
    ...base,
    captured_at: "2030-06-01T12:00:00Z",
    project_id: "proj_completely_different",
    scene_version: `sha256:${"f".repeat(64)}`,
  };
  assert.equal(computeSceneVersion(noisy), computeSceneVersion(base));
});

test("11b. repeated digest computation is byte-identical", () => {
  const body = scene([
    cube(),
    cube({ studio_object_id: "obj_chair001", name: "Chair", visible: false }),
  ]);
  const canonical = new Set<string>();
  const versions = new Set<string>();
  for (let i = 0; i < 25; i += 1) {
    canonical.add(canonicalDigestJson(sceneDigestProjection(body)));
    versions.add(computeSceneVersion(body));
  }
  assert.equal(canonical.size, 1);
  assert.equal(versions.size, 1);
});

test("11c. project_id exclusion is deliberate and documented", () => {
  assert.ok(SCENE_SNAPSHOT_INFORMATIONAL_FIELDS.includes("project_id"));
  const here = snapshot([cube()], { project_id: "proj_a" });
  const there = snapshot([cube()], { project_id: "proj_b" });
  assert.equal(computeSceneVersion(here), computeSceneVersion(there));
  assert.ok(!canonicalDigestJson(sceneDigestProjection(here)).includes("project_id"));
});

test("the version is a prefixed sha256", () => {
  const version = computeSceneVersion(scene());
  assert.ok(validateAgainstSchema(SCHEMA_FILES.SceneVersion, version).valid);
  assert.ok(sceneVersionsMatch(version, computeSceneVersion(scene())));
  assert.ok(!sceneVersionsMatch(version, version.replace("sha256:", "")));
});

// ---------------------------------------------------------------------------
// Ordering
// ---------------------------------------------------------------------------

test("objects with stable ids sort before objects without", () => {
  const withId = cube({ studio_object_id: "obj_zzz", name: "Aaa" });
  const withoutId = withoutKey(cube({ name: "Bbb" }), "studio_object_id");
  const ordered = sceneDigestProjection(scene([withoutId, withId])).objects as Dict[];
  assert.deepEqual(
    ordered.map((entry) => entry.name),
    ["Aaa", "Bbb"],
  );
});

test("sort key prefers the stable id then falls back to name", () => {
  assert.deepEqual(objectSortKey(cube()), [0, "obj_cube001"]);
  assert.deepEqual(objectSortKey(withoutKey(cube(), "studio_object_id")), [1, "Cube"]);
});

test("blank stable id falls back to name rather than sorting as empty", () => {
  assert.deepEqual(objectSortKey(cube({ studio_object_id: "   " })), [1, "Cube"]);
});

test("duplicate sort keys are refused rather than ordered arbitrarily", () => {
  assert.throws(
    () => computeSceneVersion(scene([cube(), cube({ name: "Cube.001" })])),
    /ambiguous/,
  );
});

test("duplicate names without ids are refused", () => {
  const a = withoutKey(cube(), "studio_object_id");
  const b = withoutKey(cube(), "studio_object_id");
  assert.throws(() => computeSceneVersion(scene([a, b])), /ambiguous/);
});

test("an object without a usable order key is refused", () => {
  const nameless = withoutKey(cube({ name: "   " }), "studio_object_id");
  assert.throws(() => computeSceneVersion(scene([nameless])), SceneDigestError);
});

// ---------------------------------------------------------------------------
// Malformed input is refused, never digested
// ---------------------------------------------------------------------------

for (const [name, body] of [
  ["missing units", { objects: [] }],
  ["missing objects", { units: units() }],
  ["objects as an object", { units: units(), objects: { Cube: {} } }],
  ["objects as a string", { units: units(), objects: "Cube" }],
  ["units as a string", { units: "METRIC", objects: [] }],
  ["blank unit system", { units: units({ unit_system: "" }), objects: [] }],
  ["blank length unit", { units: units({ length_unit: "" }), objects: [] }],
] as [string, unknown][]) {
  test(`malformed scene body refused: ${name}`, () => {
    assert.throws(() => computeSceneVersion(body), SceneDigestError);
  });
}

for (const [name, over] of [
  ["visible as a string", { visible: "yes" }],
  ["blank object type", { object_type: "" }],
  ["blank name", { name: "" }],
  ["numeric stable id", { studio_object_id: 7 }],
  ["short position vector", { world_position_meters: { x: 0, y: 0 } }],
  ["scale as an array", { scale: [1, 1, 1] }],
  ["material as a string", { material: "Oak" }],
] as [string, Dict][]) {
  test(`malformed object refused: ${name}`, () => {
    assert.throws(() => computeSceneVersion(scene([cube(over)])), SceneDigestError);
  });
}

// ---------------------------------------------------------------------------
// Future-schema rule: the projection is an allow-list
// ---------------------------------------------------------------------------

test("every snapshot field is classified", () => {
  const declared = new Set([
    ...SCENE_SNAPSHOT_DIGEST_FIELDS,
    ...SCENE_SNAPSHOT_INFORMATIONAL_FIELDS,
  ]);
  assert.deepEqual(
    schemaProperties(SCHEMA_FILES.SceneSnapshot).sort(),
    [...declared].sort(),
  );
});

test("every scene object field is classified", () => {
  const declared = new Set([
    ...SCENE_OBJECT_DIGEST_FIELDS,
    ...SCENE_OBJECT_INFORMATIONAL_FIELDS,
  ]);
  assert.deepEqual(
    schemaProperties(SCHEMA_FILES.SceneObject).sort(),
    [...declared].sort(),
  );
});

test("every unit field participates in the digest", () => {
  assert.deepEqual(
    schemaProperties(SCHEMA_FILES.SceneUnits).sort(),
    [...SCENE_UNITS_DIGEST_FIELDS].sort(),
  );
});

test("an unclassified informational field would not change the version", () => {
  const base = snapshot();
  const withExtra = { ...base, thumbnail_hint: "dark" };
  assert.equal(computeSceneVersion(withExtra), computeSceneVersion(base));
  assert.ok(!validateAgainstSchema(SCHEMA_FILES.SceneSnapshot, withExtra).valid);
});

// ---------------------------------------------------------------------------
// Security contract: unsafe fields are UNREPRESENTABLE
// ---------------------------------------------------------------------------

for (const key of ["SceneSnapshot", "SceneObject"] as const) {
  test(`${key} is a closed contract`, () => {
    assert.equal(loadSchema(SCHEMA_FILES[key]).additionalProperties, false);
  });
}

for (const key of ["SceneSnapshot", "SceneObject", "MaterialSummary"] as const) {
  test(`${key} declares no forbidden field`, () => {
    const properties = schemaProperties(SCHEMA_FILES[key]);
    for (const forbidden of FORBIDDEN_SCENE_FIELDS) {
      assert.ok(!properties.includes(forbidden), `${key} exposes ${forbidden}`);
    }
  });
}

test("the schema rejects every forbidden field on a scene object", () => {
  for (const forbidden of FORBIDDEN_SCENE_FIELDS) {
    const document = cube({ [forbidden]: "/srv/projects/seed.blend" });
    const result = validateAgainstSchema(SCHEMA_FILES.SceneObject, document);
    assert.equal(result.valid, false, `SceneObject accepted ${forbidden}`);
    assert.ok(result.violations.some((v) => v.path.includes(forbidden)));
  }
});

test("the schema rejects every forbidden field on a snapshot", () => {
  for (const forbidden of FORBIDDEN_SCENE_FIELDS) {
    const document = { ...snapshot(), [forbidden]: "legion-t7" };
    const result = validateAgainstSchema(SCHEMA_FILES.SceneSnapshot, document);
    assert.equal(result.valid, false, `SceneSnapshot accepted ${forbidden}`);
    assert.ok(result.violations.some((v) => v.path.includes(forbidden)));
  }
});

test("a snapshot cannot smuggle a path inside a material", () => {
  const document = snapshot([
    cube({ material: { name: "Oak", texture_path: "/tmp/oak.png" } }),
  ]);
  assert.equal(validateAgainstSchema(SCHEMA_FILES.SceneSnapshot, document).valid, false);
});

test("inspect_scene payload cannot carry anything", () => {
  assert.ok(validateAgainstSchema(SCHEMA_FILES.InspectScenePayload, {}).valid);
  for (const smuggled of [
    { blend_path: "/srv/a.blend" },
    { target: { name: "Cube" } },
    { python: "import bpy" },
    { project_id: "proj_other" },
  ]) {
    assert.equal(
      validateAgainstSchema(SCHEMA_FILES.InspectScenePayload, smuggled).valid,
      false,
    );
  }
});

// ---------------------------------------------------------------------------
// Validation layer
// ---------------------------------------------------------------------------

test("a well-formed snapshot validates", () => {
  const result = validateSceneSnapshot(snapshot());
  assert.ok(result.valid, JSON.stringify(result.errors));
});

test("scene_version can be verified against the state it labels", () => {
  assert.ok(validateSceneSnapshot(snapshot(), { verifySceneVersion: true }).valid);
  const lying = { ...snapshot(), scene_version: `sha256:${"b".repeat(64)}` };
  const result = validateSceneSnapshot(lying, { verifySceneVersion: true });
  assert.equal(result.valid, false);
  assert.deepEqual(
    result.errors.map((e) => e.code),
    ["SCENE_VERSION_MISMATCH"],
  );
});

test("version verification is off by default because producers build in order", () => {
  const lying = { ...snapshot(), scene_version: `sha256:${"b".repeat(64)}` };
  assert.ok(validateSceneSnapshot(lying).valid);
});

for (const scaleLength of [0, -1]) {
  test(`scale_length ${scaleLength} is INVALID_UNITS`, () => {
    const result = validateSceneSnapshot({
      ...snapshot(),
      units: units({ scale_length: scaleLength }),
    });
    assert.equal(result.valid, false);
    assert.ok(result.errors.some((e) => e.code === "INVALID_UNITS"));
  });
}

test("unknown unit system is reported as INVALID_UNITS", () => {
  const result = validateSceneSnapshot({
    ...snapshot(),
    units: units({ unit_system: "GALACTIC" }),
  });
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.code === "INVALID_UNITS"));
});

test("non-finite numbers are reported by the validation layer", () => {
  const document = rawSnapshot([
    cube({ world_position_meters: { x: NaN, y: 0, z: 0 } }),
  ]);
  const result = validateSceneSnapshot(document);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.code === "INVALID_UNITS"));
});

test("zero scale is rejected as degenerate", () => {
  const result = validateSceneSnapshot(
    rawSnapshot([cube({ scale: { x: 0, y: 1, z: 1 } })]),
  );
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.message.includes("non-zero")));
});

test("ambiguous ordering is reported rather than thrown", () => {
  const result = validateSceneSnapshot(
    rawSnapshot([cube(), cube({ name: "Cube.001" })]),
  );
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.message.includes("sort key")));
});

test("a non-object snapshot is rejected", () => {
  assert.equal(validateSceneSnapshot(["not", "a", "snapshot"]).valid, false);
});

// ---------------------------------------------------------------------------
// Representation parity: TS types vs canonical schemas
// ---------------------------------------------------------------------------

test("SceneSnapshot field set matches canonical schema", () => {
  const maximal: Required<SceneSnapshot> = {
    project_id: "proj_seed",
    scene_version: FAKE_VERSION,
    units: { unit_system: "METRIC", length_unit: "METERS", scale_length: 1 },
    objects: [],
    captured_at: "2026-09-15T04:00:00Z",
  };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.SceneSnapshot),
  );
  assert.deepEqual(
    ["captured_at", "objects", "project_id", "scene_version", "units"],
    schemaRequired(SCHEMA_FILES.SceneSnapshot),
  );
});

test("SceneObject field set matches canonical schema", () => {
  const maximal: Required<SceneObject> = {
    name: "Cube",
    object_type: "MESH",
    world_position_meters: { x: 0, y: 0, z: 0 },
    dimensions_meters: { x: 2, y: 2, z: 2 },
    rotation_euler_radians: { x: 0, y: 0, z: 0 },
    scale: { x: 1, y: 1, z: 1 },
    visible: true,
    studio_object_id: "obj_cube001",
    material: { name: "Beige", base_color: { r: 0.76, g: 0.66, b: 0.5, a: 1 } },
  };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.SceneObject),
  );
});

test("SceneUnits, MaterialSummary, MaterialColor, EulerRadians and Scale3 match", () => {
  const unitsMaximal: Required<SceneUnits> = {
    unit_system: "METRIC",
    length_unit: "METERS",
    scale_length: 1,
  };
  const materialMaximal: Required<MaterialSummary> = {
    name: "Beige",
    base_color: { r: 0, g: 0, b: 0, a: 1 },
  };
  const colorMaximal: Required<MaterialColor> = { r: 0, g: 0, b: 0, a: 1 };
  const eulerMaximal: Required<EulerRadians> = { x: 0, y: 0, z: 0 };
  const scaleMaximal: Required<Scale3> = { x: 1, y: 1, z: 1 };

  assert.deepEqual(
    Object.keys(unitsMaximal).sort(),
    schemaProperties(SCHEMA_FILES.SceneUnits),
  );
  assert.deepEqual(
    Object.keys(materialMaximal).sort(),
    schemaProperties(SCHEMA_FILES.MaterialSummary),
  );
  assert.deepEqual(
    Object.keys(colorMaximal).sort(),
    schemaProperties(SCHEMA_FILES.MaterialColor),
  );
  assert.deepEqual(
    Object.keys(eulerMaximal).sort(),
    schemaProperties(SCHEMA_FILES.EulerRadians),
  );
  assert.deepEqual(
    Object.keys(scaleMaximal).sort(),
    schemaProperties(SCHEMA_FILES.Scale3),
  );
});

test("a TS-built snapshot is schema-valid and digestible", () => {
  const built: SceneSnapshot = {
    project_id: "proj_seed",
    scene_version: FAKE_VERSION,
    units: { unit_system: "METRIC", length_unit: "METERS", scale_length: 1 },
    objects: [
      {
        name: "Cube",
        object_type: "MESH",
        world_position_meters: { x: 0, y: 0, z: 0 },
        dimensions_meters: { x: 2, y: 2, z: 2 },
        rotation_euler_radians: { x: 0, y: 0, z: 0 },
        scale: { x: 1, y: 1, z: 1 },
        visible: true,
        studio_object_id: "obj_cube001",
        material: { name: "Beige", base_color: { r: 0.76, g: 0.66, b: 0.5, a: 1 } },
      },
    ],
    captured_at: "2026-09-15T04:00:00Z",
  };
  assert.ok(validateAgainstSchema(SCHEMA_FILES.SceneSnapshot, toWire(built)).valid);
  assert.ok(computeSceneVersion(built).startsWith("sha256:"));
});

// ---------------------------------------------------------------------------
// inspect_scene job contract
// ---------------------------------------------------------------------------

test("inspect_scene is a canonical job type with a read classification", () => {
  assert.ok(JOB_TYPES.includes("inspect_scene"));
  const mutating = new Set(MUTATING_JOB_TYPES);
  const read = new Set(READ_JOB_TYPES);
  assert.deepEqual([...JOB_TYPES].sort(), [...new Set([...mutating, ...read])].sort());
  assert.equal([...mutating].filter((t) => read.has(t)).length, 0);
  assert.ok(isMutatingJobType("move_object"));
  assert.ok(!isMutatingJobType("inspect_scene"));
});

test("an inspect_scene job is schema-valid and its payload stays empty", () => {
  const job = {
    job_id: "job_inspect_1",
    job_type: "inspect_scene",
    project_id: "proj_seed",
    session_id: "sess_1",
    user_id: "user_1",
    payload: {},
    origin: { request_id: "req_read_1", operation_index: 0 },
    status: "queued",
    created_at: "2026-09-15T04:00:00Z",
    idempotency_key: "idem_read_1",
  };
  assert.ok(validateAgainstSchema(SCHEMA_FILES.Job, job).valid);
  assert.equal(
    validateAgainstSchema(SCHEMA_FILES.Job, {
      ...job,
      payload: { target: { name: "Cube" } },
    }).valid,
    false,
  );
});

test("adding a job type did not loosen move_object discrimination", () => {
  const job = {
    job_id: "job_1",
    job_type: "move_object",
    project_id: "proj_seed",
    session_id: "sess_1",
    user_id: "user_1",
    payload: {},
    origin: { request_id: "req_1", operation_index: 0 },
    status: "queued",
    created_at: "2026-09-15T04:00:00Z",
    idempotency_key: "idem_1",
  };
  assert.equal(validateAgainstSchema(SCHEMA_FILES.Job, job).valid, false);
});

// ---------------------------------------------------------------------------
// Shared vector corpus (the same file test_scene.py reads)
// ---------------------------------------------------------------------------

for (const c of CASES.numbers.cases) {
  test(`[corpus] number ${c.name}`, () => {
    assert.equal(formatDigestNumber(c.value), c.expected);
  });
}

for (const c of CASES.numbers.rejected) {
  test(`[corpus] number rejected: ${c.name}`, () => {
    assert.throws(() => formatDigestNumber(c.value), SceneDigestError);
  });
}

for (const c of CASES.scenes) {
  test(`[corpus] scene digestible: ${c.name}`, () => {
    const version = computeSceneVersion(c.scene);
    assert.ok(validateAgainstSchema(SCHEMA_FILES.SceneVersion, version).valid);
  });
}

for (const c of CASES.rejected_scenes) {
  test(`[corpus] scene refused: ${c.name}`, () => {
    assert.throws(() => computeSceneVersion(c.scene), SceneDigestError);
  });
}

test("[corpus] every scene vector is distinct", () => {
  const versions = CASES.scenes.map((c) => computeSceneVersion(c.scene));
  assert.equal(new Set(versions).size, versions.length);
});

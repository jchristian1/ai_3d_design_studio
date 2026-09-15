# Canonical Contract Schemas

This directory is the **single source of truth** for every contract that crosses a
service boundary.

## Direction of truth

```
packages/contracts/schemas/*.schema.json      <-- CANONICAL (language-neutral)
                │
                ├──> TypeScript representation   packages/{contracts,types,validation}/src
                └──> Python representation       packages/{contracts,types,validation}/python
```

It is explicitly **not**:

```
TypeScript source of truth  +  manually maintained Python copy
```

Neither language is authoritative. Both are *representations* that must conform to the
schemas here. When a contract changes, the schema changes **first**; the conformance
tests then fail until both representations are realigned.

## Canonical schemas

| Schema file | Contract | Notes |
|---|---|---|
| `vec3.schema.json` | `Vec3` | All components in canonical **meters** |
| `job.schema.json` | `Job` | `project_id` mandatory — project isolation. Typed discriminated union on `job_type` |
| `job-type.schema.json` | `JobType` enum | The job/operation discriminator. Only types with a payload schema are listed |
| `object-ref.schema.json` | `ObjectRef` | Requires at least one of `object_id` / `name` |
| `move-object-payload.schema.json` | `MoveObjectPayload` | Resolved meters only — no unit or direction field exists |
| `job-claim.schema.json` | `JobClaim` | Worker ownership + lease shape |
| `request-origin.schema.json` | `RequestOrigin` | `request_id` + `operation_index`; the source of mutation identity |
| `chat-request.schema.json` | `ChatRequest` | `request_id` + `project_id` required, `additionalProperties: false` |
| `chat-response.schema.json` | `ChatResponse` | Conditional rules bind `status` to `error` |
| `error-response.schema.json` | Structured error payload | `code` + human-readable `message` |
| `error-code.schema.json` | `ErrorCode` enum | The one canonical error-code list |
| `length-unit.schema.json` | `LengthUnit` enum | Input units: `m`, `cm`. Meters is canonical internally |
| `measurement.schema.json` | `Measurement` | Value + explicit unit, pre-conversion |
| `axis.schema.json` | `Axis` enum | Blender **world-space** axes only |
| `direction.schema.json` | `Direction` enum | Named world-space directions, **not** camera-relative |
| `axis-direction.schema.json` | `AxisDirection` | Resolved axis + sign (`right` → `{x, +1}`) |
| `angle-unit.schema.json` | `AngleUnit` enum | Input angle units: `rad`, `deg`. **Radians** is canonical internally |
| `angle-measurement.schema.json` | `AngleMeasurement` | Value + explicit angle unit, pre-conversion. No axis field: an axis belongs to the operation |
| `conformance-cases.json` | Shared test corpus | Language-neutral valid/invalid cases |

### Angles, resize and colour (Spec 002, Task 2)

`angle-unit` holds the **normalized** wire values only (`rad`, `deg`). Human spellings
(`degrees`, `radian`, `°`) are accepted as input *tokens* by `studio_spatial` /
`@studio/spatial` and collapse to those two before anything crosses a boundary — the
same treatment direction tokens get. Gradians and turns are deliberately absent.

Radians are canonical because Blender's `rotation_euler` is radians, so nothing is
converted at the Blender boundary. Degrees are converted exactly once, in
`packages/spatial`, using one precomputed constant (`RADIANS_PER_DEGREE = π / 180`), so
`45 deg` is exactly `π/4` in both languages. **Conversion is not normalization**:
nothing reduces an angle modulo 2π, because `720°` is a legitimate two-turn instruction.

Resize intent has **no canonical schema on purpose**. A resize factor never crosses a
boundary: `set_object_dimensions` carries absolute metres, so the factor is an internal
intermediate and a schema for it would be a contract with no wire. `(percent, direction)`
enters the canonical vocabulary in Task 6, as part of the model-proposal schema.

`material-color.schema.json` states its colour space precisely: sRGB primaries with the
transfer function **decoded** to linear light, alpha a plain unitless scalar that is
never gamma transformed. Encoded `#RRGGBB` values are not linear, and the only supported
conversion is `srgbEncodedChannelToLinear`; dividing by 255 is explicitly not it.

## Spec 002 additions — scene description (Task 1)

| Schema file | Contract | Notes |
|---|---|---|
| `scene-snapshot.schema.json` | `SceneSnapshot` | Authoritative read-only scene description. `captured_at` is informational and **excluded** from `scene_version` |
| `scene-object.schema.json` | `SceneObject` | One object as described to a model. No path, host, token, pointer, script or metadata bag is representable |
| `scene-units.schema.json` | `SceneUnits` | Blender's **observed** unit configuration. A different concept from `length-unit.schema.json`, which is the input-boundary vocabulary (`m`, `cm`) |
| `scene-version.schema.json` | `SceneVersion` | `sha256:<64 hex>` digest of an explicit versioned projection — not a hash of the snapshot |
| `euler-radians.schema.json` | `EulerRadians` | Rotation in canonical **radians**. Separate from `vec3` so a contract cannot state metres and mean radians |
| `scale3.schema.json` | `Scale3` | **Unitless** transform scale. Separate from physical size (`dimensions_meters`) |
| `material-summary.schema.json` | `MaterialSummary` | Basic material description. No node tree, no texture path |
| `material-color.schema.json` | `MaterialColor` | Linear sRGB RGBA in `[0, 1]` — what Blender's `base_color` expects |
| `inspect-scene-payload.schema.json` | `InspectScenePayload` | Deliberately **empty** and closed: the project is already trusted job identity |
| `scene-cases.json` (in `packages/contracts/`) | Shared digest vectors | Numeric formatting, ordering, exclusion and sensitivity vectors for the scene-version digest |

### The scene-version digest is a projection, not a document hash

`scene_version` is SHA-256 over an explicitly enumerated, versioned **allow-list**
projection of scene state, identified by `digest_version: "studio-scene-v1"` inside the
hashed payload. It is implemented once per language
(`python/studio_contracts/scene.py`, `src/scene.ts`) and nowhere else.

Two failure modes drove that choice:

1. hashing the snapshot document would include `captured_at`, so two reads of an
   unchanged scene would disagree and every plan would be refused;
2. a deny-list ("hash everything except `captured_at`") would make concurrency
   semantics change *silently* — a future informational field would invalidate every
   in-flight plan for no design reason.

Consequences, all test-enforced:

- excluded: `captured_at`, `scene_version` itself, and `project_id` (the digest answers
  "is this the same scene state?", not "whose scene is this?");
- object order is normalised by a documented total order — `studio_object_id` when
  present, else `name`; a duplicate sort key is **refused**, never ordered arbitrarily;
- numbers are quantised to 1e-6, rounded half-to-even, and serialised as
  **fixed-decimal strings** rather than JSON numbers, because Python renders `1.0` as
  `"1.0"` and JavaScript as `"1"`;
- non-finite values raise instead of being serialised;
- adding a `SceneSnapshot` field requires classifying it as informational (outside the
  digest) or planning-relevant (inside it, requiring `studio-scene-v2`), and a
  projection-completeness test fails if it is left unclassified.

`tests/contracts/test_scene_digest_cross_language_parity.py` asserts the two
implementations produce byte-identical canonical JSON and identical digests for every
vector in `scene-cases.json`.

`chat-response.schema.json` encodes two contract rules that would otherwise live only in
prose:

- `status: "error"` **must** carry an `error` payload.
- `status: "success"` **must not** carry an `error` payload (no ambiguous success).

## How schema drift is prevented

Five independent mechanisms, all executed by the test suites:

1. **Shared language-neutral corpus.** `conformance-cases.json` holds the valid/invalid
   cases. Both `conformance.test.ts` and `test_conformance.py` load this same file, so a
   case added once is enforced in both languages immediately.
2. **Enum parity.** `ERROR_CODES`, `JOB_STATUSES`, and `JOB_TYPES` in both languages are
   asserted equal to the canonical `enum` arrays read from the schema files at runtime.
   Adding a code to one language alone fails the tests.
3. **Field parity.** Each representation's field set is asserted equal to the canonical
   `properties` list, and each `required` list is asserted against the schema. Python
   additionally asserts that canonically-required fields have no dataclass default.
4. **Round-trip validity.** Instances constructed through the TS types and the Python
   dataclasses are serialized to wire documents and validated against the canonical
   schemas — including Python's auto-generated `created_at`, which must satisfy the
   canonical `date-time` format.
5. **Cross-language verdict parity.** `tests/contracts/test_cross_language_parity.py`
   runs the TypeScript validator in Node and the Python validator in-process over every
   corpus case and asserts the verdict vectors are byte-identical. A one-sided contract
   change fails here even if it slips past the per-language tests.

Mechanism 5 also cross-checks the *validation* layer (`validateChatRequest` /
`validate_chat_request`) against the canonical schema, so the runtime rules cannot drift
from the declared contract either. This is how the `additionalProperties: false`
divergence was caught and fixed during Task 1: the schema forbade unknown properties
while the validators accepted them.

## Why no code generation (yet)

This milestone deliberately avoids a codegen framework. The validator is a small
dependency-free JSON Schema subset interpreter implemented once per language
(`src/schema.ts`, `python/studio_contracts/schema.py`), supporting exactly the keywords
the canonical contracts use: `$ref`, `type`, `enum`, `const`, `required`, `properties`,
`additionalProperties`, `items`, `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`,
`format: date-time`, `allOf`, `anyOf`, `oneOf`, `if`/`then`/`else`, `not`.

If the contract surface grows enough that hand-written representations become a burden,
these schemas are already the correct input for generators (`json-schema-to-typescript`,
`datamodel-code-generator`) — the canonical layer is codegen-ready without rework.

## Known limitation

JSON has no `NaN`/`Infinity` literals, so the *finite meters* rule cannot be expressed in
JSON Schema. It is enforced by the validation layer in both languages
(`isFiniteMeters` / `is_finite_meters`) and covered by unit tests.

## Changing a contract

1. Edit the schema in this directory.
2. Add valid/invalid cases to `conformance-cases.json`.
3. Run both suites — they will fail, naming exactly what diverged.
4. Update the TypeScript and Python representations until green.

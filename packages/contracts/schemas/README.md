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
| `conformance-cases.json` | Shared test corpus | Language-neutral valid/invalid cases |

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

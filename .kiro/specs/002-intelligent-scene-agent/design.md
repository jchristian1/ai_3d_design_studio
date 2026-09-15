# Design — 002 Intelligent Scene Agent

Spec 001 is the baseline. This document describes only what changes and what is added.
Where a Spec 001 boundary works, it is reused as-is and named rather than restated.

## The governing idea

> The model gains language ability, not authority.

Everything below follows from that. The model sees a safe description of the scene and
returns a constrained **proposal**. The platform validates the proposal, resolves
object references against real scene state, and only then constructs Jobs using its own
code. There is no path by which model output becomes execution.

```
UNTRUSTED                          TRUSTED
─────────────────────────────      ────────────────────────────────────
user message                  ─┐
SceneSnapshot (safe, read-only)├─→ ContextBuilder ─→ LLM ─→ Proposal ─┐
conversation / clarification  ─┘                                      │
                                                                      ▼
                                              schema validation + object resolution
                                                                      │
                                                                      ▼
                                              AgentOutcome: Answer | Clarification
                                                          | AgentPlan | Error
                                                                      │
                                                          (plan only) ▼
                                              JobFactory → canonical Jobs → worker
```

---

## 1. Architecture

New and changed components are marked. Everything unmarked is Spec 001, unchanged.

```
┌─ apps/web ───────────────────────────────────────────────────────────┐
│  StudioShell · ChatPanel · PreviewPanel                              │
│  lib/api (client)            [CHANGED: answer/clarification results] │
│  lib/session/reducer.ts      [CHANGED: outcome kinds]                │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP
┌─ services/api ────────────────▼──────────────────────────────────────┐
│  routes/chat.py                                                      │
│  ChatService                  [CHANGED: outcome dispatch]            │
│  SceneContextService          [NEW]  snapshot fetch + cache          │
│  ContextBuilder               [NEW]  assembles provider context      │
│  ObjectResolver               [NEW]  proposal → resolved reference   │
│  ClarificationStore           [NEW]  short session-scoped state      │
│  PlanCoordinator              [NEW]  ordered multi-Job orchestration  │
│  JobReconciler                [CHANGED: per-operation status]        │
│  worker_link/{manager,gateway}                                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ canonical Job / read query (WS)
┌─ services/agent ──────────────┴──────────────────────────────────────┐
│  provider.py            AgentProvider protocol (unchanged shape)     │
│  outcome.py             [NEW] AgentOutcome union                     │
│  proposal.py            [NEW] untrusted ProposedOperation model      │
│  plan.py                [CHANGED] richer PlannedOperation payloads   │
│  job_factory.py         [CHANGED] one Job per operation_index        │
│  providers/rule_based.py         retained for deterministic tests    │
│  providers/<real>.py    [NEW] the real LLM adapter                   │
│  providers/fake_llm.py  [NEW] scripted provider for offline tests    │
└──────────────────────────────────────────────────────────────────────┘

┌─ services/blender-worker ────────────────────────────────────────────┐
│  WorkerExecutor         [CHANGED] operation dispatch + read jobs     │
│                         [CHANGED] in-lock scene-version precondition │
│  executor ops registry  [NEW] one handler per semantic operation     │
└───────────────────────────────┬──────────────────────────────────────┘
┌─ services/blender-mcp ────────▼──────────────────────────────────────┐
│  tools/move_object.py                                                │
│  tools/rotate_object.py         [NEW]                                │
│  tools/dimensions.py            [NEW]  set_object_dimensions (primary)│
│  tools/scale_object.py          [NEW]  explicit transform scale       │
│  tools/set_material_color.py    [NEW]                                │
│  tools/inspect_scene.py         [NEW]  read-only + scene_version     │
│  adapters/scene.py              [CHANGED] wider SceneAdapter         │
│  adapters/blender_scene.py      [CHANGED] the only bpy               │
└──────────────────────────────────────────────────────────────────────┘

┌─ services/preview ───────────────────────────────────────────────────┐
│  blender_scripts/render_preview.py  [CHANGED] Workbench shading      │
│                                     colour source = object material  │
└──────────────────────────────────────────────────────────────────────┘
```

### Why `SceneContextService` lives in the API, not the agent

The agent must not decide when to read Blender, and must not hold a worker connection.
Scene retrieval is a control-plane concern (it needs worker selection, dispatch, and
caching), so it sits beside `ChatService`. The provider receives a snapshot as data.

---

## 2. SceneSnapshot flow

### Retrieval path

`inspect_scene` is dispatched through the **existing Job path**, not a new protocol
channel. That reuses worker selection, offer/accept, result delivery, and
reconciliation, and avoids widening the protocol surface.

```
ChatService needs scene knowledge
      ↓
SceneContextService.snapshot_for(project_id)
      ↓  cache hit? ─────────────────────→ return cached snapshot
      ↓  miss
create READ job (job_type: inspect_scene)
      ↓
WorkerGateway.offer → worker → WorkerExecutor
      ↓  read classification: no write lock, no recovery point, no save, no preview
inspect_scene → SceneAdapter.list_objects() → Blender
      ↓
SceneSnapshot + scene_version  ──(job_result)──→ JobReconciler → cache
```

### Read jobs are a distinct job class

A job carries a **mutating** classification derived from its `job_type`:

| Class | Lock | Recovery point | Save | Preview | Idempotency |
|---|---|---|---|---|---|
| mutating (`move_object`, `rotate_object`, …) | exclusive | yes | yes | yes | derived key, executes once |
| read (`inspect_scene`) | none or shared | no | no | no | naturally idempotent, cacheable |

This is the cleanest available extension: the executor already branches on `job_type`,
and a read that took a write lock, wrote a recovery snapshot, and rendered a preview
would be both slow and wrong.

### Caching and invalidation

`SceneContextService` caches by `project_id`, storing the snapshot and its
`scene_version`. The cache is invalidated when a mutation for that project reaches a
terminal successful state. In-memory for Spec 002, behind an interface, matching the
Spec 001 precedent for control-plane state.

**Cost, stated plainly:** Spec 001 launches a fresh Blender subprocess per operation
(~1 s). A cache miss therefore costs about a second before the model is even called.
Acceptable for Spec 002; a persistent Blender process is the documented remedy and
stays deferred.

### `scene_version`

A SHA-256 digest over an **explicit versioned projection** of semantic scene state. It
serves three purposes:

1. cache validity,
2. a record of *which* scene a plan was reasoned against,
3. an **enforced execution precondition** — a plan reasoned against one scene is not
   permitted to mutate a different scene.

Purpose 3 is specified in §2.1. The digest projection is specified in §2.3, and is
deliberately **not** "hash the SceneSnapshot".

---

## 2.1 Scene-version precondition (enforced)

### Why `expected_before` is not enough

Spec 001's `expected_before_meters` proves *the target object* has not moved. A
scene-aware plan reasons about more than its target:

> "Move the chair closer to the table."

The chair may be exactly where the snapshot said, while the **table** — used only in the
reasoning, never named in the payload — has moved. The per-object precondition passes and
the resulting position is wrong. Per-object verification is necessary but insufficient,
so Spec 002 adds a whole-scene precondition. Both are enforced; neither replaces the
other.

### Where the version is computed

Only Blender knows the authoritative scene state, so the digest is computed inside the
same Blender invocation that already loads the project:

```
worker acquires project lock
  → Blender opens the .blend
  → compute scene_version of the LOADED scene      (pre-mutation authoritative version)
  → precondition check                              ← §2.1 decision point
  → recovery point → mutate → save
  → compute scene_version of the SAVED scene        (post-mutation authoritative version)
  → report both on the job result
```

No extra subprocess and no extra file read: both digests come from the process that was
going to open the file anyway.

### Where the check happens — inside the lock

The check must be atomic with respect to mutation, so it happens in the worker, holding
the project lock, **before** the recovery point and before any write. A check performed
in the API against a cached snapshot is racy by construction; the API may perform one as
a cheap early rejection, but it is **advisory only** and is documented as such. The
in-lock check is the authority.

### Decision order inside the operation

Order matters, because a retry of an already-applied operation legitimately sees a
*changed* scene — the change it made itself:

```
1. already-applied?   desired_after already met (Spec 001 tolerance)
                      → return already_applied, mutate nothing, skip the version check
2. version match?     loaded scene_version == required_version
                      → no  → SCENE_VERSION_MISMATCH, mutate nothing
3. expected_before?   target object still where the plan expected
                      → no  → PRECONDITION_MISMATCH, mutate nothing
4. apply absolute desired_after → verify from saved file → save
```

Step 1 precedes step 2 deliberately. Reversing them would make Spec 001's idempotency
guarantee collide with the new precondition: a verbatim retry would be rejected as stale
even though the correct answer is "already done, nothing to do".

### `required_version` per operation

Each persisted operation record carries a `required_scene_version`:

| Operation | `required_scene_version` |
|---|---|
| index 0 | the `scene_version` the plan was reasoned against |
| index *n* > 0 | the post-mutation `scene_version` reported by operation *n−1* |

Operation 0's value is fixed when the plan is created. Operation *n*'s value is filled in
by `PlanCoordinator` from operation *n−1*'s result, and persisted in the worker journal
record before operation *n* is offered.

This is the chaining rule, and it gives exactly the behaviour required:

- an intentional change by operation 0 does **not** reject operation 1, because
  operation 1's requirement *is* operation 0's outcome;
- an external change between operation 0 and operation 1 **does** reject operation 1,
  because the loaded digest matches neither the chained value nor anything else;
- a resumed retry re-derives the same chain from the journal, so resumption is
  deterministic rather than dependent on live API memory.

### What happens on mismatch

```
SCENE_VERSION_MISMATCH  (canonical error code, added to error-code.schema.json)
  → nothing mutated, no recovery point written, lock released
  → SceneContextService invalidates the cached snapshot for the project
  → a fresh SceneSnapshot is fetched
  → the request is re-resolved and re-planned against the new scene state
```

Re-planning is bounded: **at most one automatic re-plan per user request**. If the second
attempt also mismatches, the platform stops and reports it — no unbounded retry loop
against a scene that keeps changing. For a multi-operation plan, a mismatch on operation
*n* stops the plan under §10's fail-fast rule; the already-applied operations stay
applied, and re-planning starts from the refreshed scene rather than resuming a plan
built on stale assumptions.

The browser is told the project changed and nothing was modified. It is never shown a
digest.

### Cost

One digest computation per Blender invocation, over scene state already being read.
Negligible against the ~1 s subprocess launch Spec 001 already pays.

---

## 2.2 Contract — new canonical schemas

New canonical schemas (language-neutral, mirrored in Python and TypeScript):

- `scene-snapshot.schema.json` — `project_id`, `scene_version`, unit configuration,
  `objects[]`, `captured_at`
- `scene-object.schema.json` — `studio_object_id?`, `name`, `object_type`,
  `world_position_meters`, `dimensions_meters`, `rotation_euler_radians`, `scale`,
  `material?` (basic summary), `visible`
- `material-color.schema.json` — canonical colour (see §7)

`additionalProperties: false` throughout. **No** path, filename, hostname, or Blender
internal is representable — the same closure that protects `PreviewArtifact`.

`error-code.schema.json` gains `SCENE_VERSION_MISMATCH` (§2.1).

---

## 2.3 Scene-version digest projection — `studio-scene-v1`

`scene_version` is **not** a hash of the SceneSnapshot. It is a hash of an explicitly
enumerated projection of semantic scene state, identified by a digest version.

### Why a projection, not "all fields except X"

A "hash everything except a deny-list" rule fails in two ways. First, mechanically
hashing the snapshot would include `captured_at`, so two reads of an unchanged scene
would produce different versions and every plan would be refused. Second, and worse, a
deny-list makes concurrency semantics change silently: adding a future informational
field — a thumbnail hint, a UI label, a statistics block — would change every project's
`scene_version` and invalidate plans for no design reason.

So the digest input is an **allow-list keyed by a digest version**. A field that is not
in the projection cannot affect concurrency detection, whether it exists today or is
added later.

### Definition

```
scene_version = "sha256:" + hex(
  sha256(
    canonical_json({
      "digest_version": "studio-scene-v1",
      "units": {
        "unit_system":  <string>,
        "length_unit":  <string>,
        "scale_length": <number>
      },
      "objects": [
        {
          "studio_object_id":       <string | null>,
          "name":                   <string>,
          "object_type":            <string>,
          "world_position_meters":  [<number>, <number>, <number>],
          "dimensions_meters":      [<number>, <number>, <number>],
          "rotation_euler_radians": [<number>, <number>, <number>],
          "scale":                  [<number>, <number>, <number>],
          "visible":                <boolean>,
          "material": null | {
            "name":       <string>,
            "base_color": [<number>, <number>, <number>, <number>]
          }
        }
      ]
    }).encode("utf-8")
  )
)
```

The `digest_version` string is inside the hashed payload, so it is domain separation:
two different projections can never collide, and a version bump is visible in the input
rather than only in a comment.

### In the projection

Unit configuration (`unit_system`, `length_unit`, `scale_length`) and, per object:
membership, `studio_object_id` where present, `name`, `object_type`, world position,
dimensions, rotation, scale, visibility, and the basic material state the SceneSnapshot
exposes (material name and base colour). This is exactly the set the model is shown and
may plan against.

### Excluded, by construction

`captured_at`, `scene_version` itself, worker id, hostname, filesystem paths, tokens,
`request_id`/`session_id`, preview metadata, and transport metadata. None of these is
scene state, and several change on every read.

`project_id` is **excluded**. The digest answers "is this the same scene state?", not
"whose scene is this?" — authorization and cache keying are already project-scoped
elsewhere (jobs carry `project_id`, the cache is keyed by it). Excluding it also
preserves a useful property for tests: an identical scene has an identical version
regardless of which project holds it. Domain separation comes from `digest_version`,
which is what domain separation is actually for.

### Canonical serialization

- UTF-8, object keys sorted lexicographically, no insignificant whitespace
  (`,` and `:` separators), no trailing newline.
- Arrays preserve their given order — vectors are positional (x, y, z), and the object
  array is explicitly sorted (below).
- Booleans and `null` are emitted as JSON literals, never as strings or numbers.
- Non-finite numbers (`NaN`, `±Infinity`) are a **hard error**, not a serialised token:
  an unreadable scene is refused rather than digested.
- Absent optional values are emitted as explicit `null` with the key present, so
  "no material" and "material removed" cannot alias into different shapes.

### Object ordering

Object enumeration order in Blender must not affect the digest, so objects are sorted by
a documented total order before serialisation:

```
sort key = (0, studio_object_id)   when a stable id is present
           (1, name)               otherwise
```

Objects carrying a stable id sort before those without, then by id; the remainder sort by
name. Blender guarantees object names are unique within a file, so the fallback is a
total order rather than a tie-break. As a guard, the implementation asserts the resulting
key sequence is strictly increasing; a duplicate key means an ambiguous snapshot and is
refused rather than digested into an arbitrary order.

### Floating-point serialization

Floats are quantised, then formatted as fixed-point decimal, so the same scene hashes
identically on repeated inspection and across both languages:

| Quantity | Quantum | Decimals |
|---|---|---|
| position, dimensions (metres) | 1e-6 (1 µm) | 6 |
| rotation (radians) | 1e-6 rad (≈ 0.2 arcsec) | 6 |
| scale (unitless) | 1e-6 | 6 |
| colour channels `[0,1]` | 1e-6 | 6 |
| `scale_length` | 1e-6 | 6 |

Rules: round half to even at the quantum, format with exactly the stated number of
decimals (never exponent notation), and normalise negative zero to `0.000000` so `-0.0`
and `0.0` cannot produce different versions. Quantised values are emitted into the
canonical JSON as **fixed-decimal strings**, not as JSON numbers: Python renders `1.0`
as `"1.0"` and JavaScript as `"1"`, and their exponent formatting differs too, so a
JSON number would make the two implementations disagree.

**Why quantise at all, and why this coarse.** Quantisation exists to remove
representation noise, not change. Blender stores these values as float32; a digest
computed twice from the same bytes is identical regardless of quantum, so stability does
not depend on it — but a shared, documented decimal form is what makes the Python and
TypeScript implementations agree, and it removes any dependence on a language's float
repr. 1 µm is three orders of magnitude below the smallest design-meaningful change
(sub-millimetre), and well below Spec 001's calibrated position tolerance, so no change
large enough to be *applied* can vanish into the quantum. Rotation at 1e-6 rad and colour
at 1e-6 are chosen on the same basis.

The quantum is part of `studio-scene-v1`. Changing it changes the digest version.

### Future-schema rule

Adding a field to `SceneSnapshot` does **not** change `scene_version`. Two paths exist,
and the choice is explicit:

- the field is **informational** → add it to the snapshot and to the documented
  informational list; the digest is untouched, and no plan is invalidated;
- the field is **planning-relevant** → it must enter the projection, which requires a new
  `digest_version` (`studio-scene-v2`) and a deliberate decision, because every recorded
  `scene_version` will mismatch once and every in-flight plan will be refused.

A **projection-completeness test** enforces the choice mechanically: it enumerates the
SceneSnapshot schema's fields and asserts each one appears in either the digest
projection or the informational list. A new field with no decision fails the test rather
than silently altering concurrency semantics.

### Test obligations

Task 1 must prove all eleven properties enumerated in `tasks.md` — identical version on
repeated inspection, insensitivity to `captured_at`, object order and excluded metadata,
and sensitivity to position, dimensions, rotation, scale, material colour, visibility and
object membership — plus shared cross-language digest vectors so Python and TypeScript
agree on the exact hex.

SceneSnapshot earns a canonical schema because it genuinely crosses the wire
(worker → control plane → provider → browser). `AgentPlan` still does not, and remains
schema-free per the Spec 001 decision.

---

## 3. Context-building flow

`ContextBuilder` is the only place that decides what the model sees. Provider-specific
prompt construction lives in the provider adapter; **route code never builds prompts**.

```
ContextBuilder.build(request, identity, snapshot, session) → AgentContext
  ├── system rules            operation catalogue + refusal rules + output schema
  ├── trusted identity        project_id · session_id · user_id   (server-supplied)
  ├── SceneSnapshot           authoritative, safe
  ├── selected_object_id      browser selection, if any
  ├── clarification state     the pending question and its candidates, if any
  ├── recent turns            short, bounded, session-scoped
  └── current user message
```

Deliberately absent, and asserted absent by tests: credentials, filesystem paths,
worker tokens, `.blend` locations, other projects' data, and any prior project's
history.

`AgentContext` already exists from Spec 001 with `user_id`/`project_id`/`session_id`.
Spec 002 extends it with the snapshot, selection, clarification state, and recent
turns — the fields Spec 001 listed as future work.

**Bounded by construction.** Recent turns and candidate lists are capped so a long
conversation cannot grow the prompt without limit, and large scenes are summarised
rather than serialised whole (strategy: full detail for resolution candidates, compact
listing otherwise).

---

## 4. Agent result union

Spec 001's `AgentResult` is a boolean-success shape (`ok`, `plan`, `error`). Spec 002
replaces it with a discriminated union, because "what happened" now has four genuinely
different answers.

```python
AgentOutcome = Answer | Clarification | PlanProposal | AgentError
#   kind: "answer" | "clarification" | "plan" | "error"
```

| Kind | Carries | Jobs created | Preview |
|---|---|---|---|
| `Answer` | prose text grounded in the snapshot | none | none |
| `Clarification` | question, candidate objects, pending-intent handle | none | none |
| `PlanProposal` | ordered `AgentPlan` + the `scene_version` it was reasoned against (enforced, §2.1) | one per operation | per operation |
| `AgentError` | canonical `ChatError` | none | none |

`ProviderMetadata` continues to carry only provider name, version, and operation count
— never reasoning, never prompt text.

This is a deliberate, contained change to a Spec 001 boundary. `RuleBasedProvider` is
migrated to return `PlanProposal` / `AgentError`, keeping its deterministic behaviour.

---

## 5. Provider boundary

```
AgentProvider (protocol, unchanged shape)
├── RuleBasedProvider     retained — deterministic, offline, tiny grammar
├── FakeLlmProvider       [NEW] scripted proposals, for offline tests of the whole
│                                pipeline including clarification and multi-op
├── <RealLlmProvider>     [NEW] the one real provider Spec 002 requires
└── <other placeholder>   may remain unimplemented
```

### Inside the real provider

```
AgentContext → prompt assembly → model call (structured output requested)
             → raw proposal (UNTRUSTED)
             → schema validation                    ← rejects here, not later
             → AgentOutcome
```

The provider owns prompt text, model parameters, retry/backoff on transport errors, and
translation of model output into a validated proposal. It does **not** own object
resolution or Job construction — those are platform concerns downstream, so a
misbehaving provider cannot reach Blender.

### Two-stage trust model

This is the core safety mechanism:

```
ProposedOperation  (untrusted, model-authored, schema-validated)
        ↓  ObjectResolver — against the authoritative SceneSnapshot
        ↓  unit/range validation — via packages/spatial
PlannedOperation   (trusted, platform-authored)
        ↓  JobFactory
canonical Job
```

A `ProposedOperation` may name an object loosely ("the chair", `"Cube"`, a candidate
id). A `PlannedOperation` addresses a resolved `studio_object_id` and carries canonical
values only. **The model never authors a Job.**

### Credentials

Environment/configuration only, read once in the settings layer, excluded from `repr`,
never logged, never returned by the API, never in `NEXT_PUBLIC_*`. Same discipline as
`STUDIO_WORKER_TOKEN` in Spec 001.

---

## 6. Tool / operation model

The catalogue the model may propose from. It is closed: an operation outside this list
is rejected before the worker.

| Operation | Mutating | Canonical payload (resolved) |
|---|---|---|
| `inspect_scene` | no | — |
| `move_object` | yes | `target`, `delta_meters` *(Spec 001, unchanged)* |
| `rotate_object` | yes | `target`, axis, absolute `desired_after_radians` |
| `set_object_dimensions` | yes | `target`, absolute `desired_after_dimensions_meters` — **preferred for user-facing resize language** |
| `scale_object` | yes | `target`, absolute `desired_after_scale` (unitless) — explicit transform-scale intent |
| `set_material_color` | yes | `target`, canonical colour |

Every mutating operation additionally carries `required_scene_version` and
`expected_before` (§2.1), and every one of them enforces both.

Each mutating operation follows the Spec 001 execution contract without exception:

```
resolve project path (trusted registry) → acquire project lock
→ load journal record → persist plan BEFORE mutation
→ already-applied? → scene-version precondition → expected_before precondition   (§2.1)
→ recovery point
→ apply ABSOLUTE desired-after (never a relative += on retry)
→ verify from the SAVED file → durable save → post-mutation scene_version → preview
→ complete
```

**Why absolute targets everywhere.** Spec 001 learned this for translation: a retry
that re-reads current state and re-applies a delta double-applies. Rotation, scale, and
dimensions have exactly the same hazard, so each operation persists
`expected_before` + `desired_after` and writes the absolute target. A retry reuses the
persisted plan and, seeing the target already met, applies nothing.

### `SceneAdapter` widening

Today: `find_object`, `set_world_position`, `read_world_position`. Spec 002 adds
`list_objects`, `read_object_state`, `scene_version`, `set_rotation_euler`, `set_scale`,
`set_dimensions`, `set_material_base_color`. Still no query language, no fuzzy search,
no arbitrary code path — `bpy` stays confined to `adapters/blender_scene.py` and the
Blender scripts.

---

## 7. Units and colour

| Quantity | Canonical form | Converted where |
|---|---|---|
| distance, dimensions | metres | `packages/spatial` (Spec 001) |
| angle | **radians** | `packages/spatial` (new `angles.py`) |
| size intent (resize) | **absolute dimensions in metres** | `packages/spatial`, against the snapshot |
| transform scale | absolute unitless scale factors | `packages/spatial` |
| colour | linear sRGB RGBA floats in `[0,1]` | `packages/spatial` or a small colour module |

**Radians, not degrees.** Blender's `rotation_euler` is radians, and the Task 5 fixture
digest already records `rotation_euler_radians`. Choosing radians means zero conversion
at the Blender boundary — the place a conversion bug would be most expensive. Degrees
are a *language-edge* unit: "45 degrees" is converted once, above the worker, exactly
as "50 cm" is.

### Resize: `set_object_dimensions` is primary (decision closed by review)

Two operations exist because two different intents exist, and conflating them produces
surprising results:

| Instruction | Operation | Why |
|---|---|---|
| "Make Cube 20% smaller." | `set_object_dimensions` | **design-space size intent** — the user is talking about metres of real size |
| "Make it 1.6 m wide." | `set_object_dimensions` | same: an absolute physical size |
| "Set its scale to 0.8." | `scale_object` | **explicit transform-scale intent** — the user is talking about the transform |

User-facing resize language resolves to `set_object_dimensions` with absolute desired
dimensions in metres, derived from the authoritative SceneSnapshot:

```
snapshot dimensions      2.00 × 2.00 × 2.00 m
"20% smaller"            factor 0.8, applied above the worker
desired_after_dimensions 1.60 × 1.60 × 1.60 m      ← what the worker receives
```

The worker never receives a percentage, and never a relative multiplier it would have to
apply to whatever the current value happens to be. `scale_object` is retained for
explicit scale commands and likewise carries **absolute** validated unitless scale
values. Both persist `expected_before` and `desired_after`, so both are retry-safe under
§2.1's decision order.

Dimensions are chosen as primary for size language because they are the representation
the user is actually speaking about, they are verifiable directly from the saved file,
and they do not depend on the object's mesh-bound history the way a bare scale factor
does. Blender's `scale` and `dimensions` interact through mesh bounds; picking the
physical quantity as the canonical one keeps the reasoning honest.

**Percentages are resolved above the worker.** "20% smaller" needs current dimensions,
so it resolves against the SceneSnapshot into an absolute target — and therefore
inherits the §2.1 scene-version precondition, because the derived target is only correct
for the scene it was derived from.

**Colour names are resolved above the worker.** The model may interpret "warm beige"
into a canonical colour, which is then range-validated. The worker receives explicit
numeric values. A small named-colour vocabulary provides deterministic offline testing;
`linear sRGB` is chosen because it is what Blender's `base_color` expects, avoiding a
colour-space conversion at the Blender boundary.

### Preview must visibly show material colour (Task 11 tightened)

Spec 001's preview is Workbench with flat lighting and a fixed background, deliberately
chosen so output is byte-reproducible with no sampling and no GPU. In that configuration
the shading colour source is not the object's material, so a colour change would be
saved and verified but **invisible** — the user would be told "done" and see no
difference. That is not acceptable behaviour for Spec 002.

Decision: change the Workbench shading colour source to the object's material
(`scene.display.shading.color_type = 'MATERIAL'`, keeping flat lighting), so the base
colour written by `set_material_color` appears in the rendered PNG.

Why this option:

- Workbench remains the engine, so it stays **headless**, **CPU-viable**, and
  **sample-free**, therefore still deterministic. No Cycles, no GPU dependency, no
  denoiser variance.
- Flat lighting is retained rather than studio lighting, so the rendered pixel colour is
  a predictable function of the material colour — which is what makes the acceptance
  assertion meaningful instead of approximate.
- It is a configuration change to one existing render script, not a new pipeline.

Consequences accepted deliberately:

- Rendered bytes change for existing scenes. Determinism is preserved (same input → same
  output), and Spec 001's assertions compare a served PNG against the checksum recorded
  in the artifact store rather than a hard-coded golden hash, so this is a re-baseline,
  not a contract break. Any recorded constant found during implementation is re-baselined
  explicitly, never loosened to make a test pass.
- All Spec 001 preview safety properties are preserved unchanged: all stamp flags stay
  disabled (no `.blend` path in PNG `tEXt` metadata), the temporary camera is still never
  saved, resolution and framing stay fixed.

Acceptance is pixel-level, not merely "a PNG exists": the preview after "Make Cube a warm
beige." must differ from the preview before it, and pixels sampled in the object's region
must be within tolerance of the requested colour.

---

## 8. Object resolution rules

Applied by `ObjectResolver` against the authoritative snapshot, in order:

1. **Explicit stable id** — if the proposal carries a `studio_object_id`, it must exist
   in the snapshot. If it does not: error. The model cannot invent identifiers.
2. **Exact name match** — case-sensitive, exactly one match → resolved.
3. **Case-insensitive name match** — exactly one → resolved.
4. **Browser selection** — if the referent is unresolvable but a `selected_object_id`
   is supplied and present in the snapshot → resolved.
5. **Clarification answer** — if a pending clarification is being answered and the
   answer identifies one candidate → resolved.
6. **Several matches** → `Clarification` listing candidates.
7. **No match** → structured error (`OBJECT_NOT_FOUND`); never create, never substitute.

Resolution is server-side and deterministic. Fuzzy or semantic matching is explicitly
**not** performed by the resolver — if the model wants to propose a candidate set, it
does so as data and the resolver still validates every id.

---

## 9. Ambiguity and clarification

```
ambiguous request
      ↓
Clarification { question, candidates[], pending_intent_id }
      ↓  stored: ClarificationStore[session_id] (short-lived, bounded, in-memory)
      ↓  browser renders a question; zero Jobs created
user replies "the one by the window" / "the second one" / "Chair_02"
      ↓
ContextBuilder includes the pending clarification + candidates
      ↓
resolved → PlanProposal, or → Clarification again
```

Design points:

- A clarification is **not** an error: it has its own outcome kind, its own UI
  treatment, and it is the correct, successful response to an ambiguous instruction.
- **Zero Jobs** are created. Nothing is locked, nothing is planned, nothing is saved.
- State is session-scoped, capped in size and count, and expires. It is *conversational
  continuity*, not project memory — persistent memory remains deferred.
- The pending intent stores the original message and candidate set, so the follow-up
  need not repeat the instruction.

---

## 10. Multi-operation semantics

### Plan shape

```
request_id: req_x
  operation_index 0 → move_object          → Job(idem = hash(project, req_x, 0))
  operation_index 1 → rotate_object        → Job(idem = hash(project, req_x, 1))
  operation_index 2 → set_material_color   → Job(idem = hash(project, req_x, 2))
```

`AgentPlan.operations` is already an ordered tuple whose index *is* the
`operation_index` — Spec 001 built it that way for exactly this. Mutation identity
remains `project_id + request_id + operation_index`, so per-operation idempotency comes
for free.

### Execution: sequential, non-atomic, resumable

`PlanCoordinator` offers operation *n* only after *n−1* reaches terminal success.

### Scene-version chaining across a plan

An approved ordered plan is *expected* to change the scene as it runs, so a single fixed
precondition would reject its own second operation. The chain from §2.1:

```
plan reasoned against V0
  op 0   required_scene_version = V0    → applies → saved scene is V1 (reported)
  op 1   required_scene_version = V1    → applies → saved scene is V2 (reported)
  op 2   required_scene_version = V2    → applies → saved scene is V3 (reported)
```

`PlanCoordinator` writes operation *n*'s `required_scene_version` from operation *n−1*'s
reported post-mutation version, and persists it before offering the job — so a resumed
retry re-derives the same chain from the journal rather than from live API memory.

If anything **other** than the preceding operation changes the project — another session,
a manual edit, a different plan — the loaded digest matches neither the chained value nor
anything else, and the remaining operations are refused with `SCENE_VERSION_MISMATCH`
under the fail-fast rule below. Intentional self-inflicted change is accepted; unexpected
external change is not.

**Partial failure is explicit.** If operation 1 fails:

- operation 0 remains **applied and saved** — it is not rolled back,
- operation 2 is **not attempted**,
- the outcome reports per-operation status: `applied` · `failed` · `not_attempted`,
- the browser says what happened; it does **not** claim success.

**Why no rollback.** Honest engineering: a true multi-operation transaction would need
either a scene-version-checked apply-all or coordinated restoration of pre-mutation
snapshots. Spec 001 provides per-job recovery points, so rollback is *possible in
principle*, but doing it correctly across several saves — while another request could
arrive — is a larger design than Spec 002 should smuggle in. Pretending atomicity
exists would be worse than documenting that it does not.

Retrying the same `request_id` **resumes**: already-applied operations are recognised
as duplicates and skipped, and execution continues from the first unapplied one.

---

## 11. Failure semantics summary

| Failure | Outcome | Jobs | Project |
|---|---|---|---|
| Provider unreachable / unauthenticated | `AgentError` `PROVIDER_UNAVAILABLE` | none | untouched |
| Model output malformed / invalid / unknown operation | `AgentError` `VALIDATION_ERROR` | none | untouched |
| Model invents an object id | `AgentError` `OBJECT_NOT_FOUND` | none | untouched |
| Ambiguous referent | `Clarification` | none | untouched |
| Object genuinely absent | `AgentError` `OBJECT_NOT_FOUND` | none | untouched |
| Scene unreadable | `AgentError` | none | untouched |
| Scene changed since the plan was reasoned | `SCENE_VERSION_MISMATCH` on that operation; snapshot refreshed; at most one automatic re-plan | that Job fails, later ops not attempted | untouched by the refused operation |
| No worker available | HTTP 503 `BLENDER_UNAVAILABLE` | none | untouched |
| Lock conflict | `LOCK_CONFLICT` on that operation | that Job fails | untouched |
| Mutation fails mid-plan | per-operation status | earlier applied, later not attempted | consistent, verified |
| Preview fails | job still `succeeded` + `preview_error` | — | mutation durable |

Every row preserves the Spec 001 rule: **a failure never leaves the project partially
mutated within a single operation**, and success is always verified from the saved file.

---

## 12. Security and threat boundaries

### Threat model additions

| Threat | Control |
|---|---|
| Prompt injection → shell/Python/file access | No such tool exists in the catalogue; the protocol enum is closed; model output is data validated against a schema and never evaluated |
| Model invents a protocol message type | Protocol vocabulary is a closed enum validated at both ends; the provider cannot emit protocol messages at all |
| Model supplies a `.blend` path | No path field exists in any proposal, plan, Job, or protocol schema; `additionalProperties: false` throughout; the worker resolves paths from its own trusted registry |
| Model overrides identity | `project_id`/`user_id`/`session_id` come from `TrustedIdentity` and `AgentContext`; the proposal schema has no identity fields; `JobFactory` already refuses a project mismatch |
| Model bypasses object resolution | Every object reference is resolved server-side against the snapshot; unknown ids are rejected |
| Model exfiltrates secrets via prompt | Prompts are assembled by `ContextBuilder` from a declared field list; credentials, paths and tokens are never included; asserted by test |
| Scene snapshot leaks host detail | Snapshot schema is closed with no path/hostname field |
| Provider credentials leak | Read from environment, excluded from `repr`, never logged or returned; absent from `NEXT_PUBLIC_*` |
| Model output is huge / adversarial | Output size and operation count are bounded; a plan exceeding limits is rejected |
| Plan executes against a scene it never reasoned about | Enforced in-lock `scene_version` precondition with per-operation chaining (§2.1, §10); mismatch mutates nothing |

### Unchanged Spec 001 invariants

The workstation never listens; Blender and MCP are not publicly exposed; `bpy` is
confined to two locations; the worker connects outbound with a pre-shared token; jobs
and artifacts are project-scoped with no unscoped routes; PNG metadata is stripped.

---

## 13. Testing architecture

Four tiers. **The default suite is deterministic and offline** — no network, no API key,
no paid calls.

```
1. Unit / contract        pure logic, schema conformance, cross-language parity
                          (snapshot shapes, angle conversion, colour validation,
                           proposal validation, resolver rules, plan ordering)

2. Fake-provider pipeline FakeLlmProvider scripts exact proposals, so the WHOLE
                          pipeline — context → proposal → validation → resolution →
                          plan → Jobs → outcome — is tested without a model.
                          This is where clarification, multi-op, and injection
                          resistance are proven.

3. Real Blender           marked `blender`, opt-in: each new operation verified from
                          the SAVED .blend by a fresh Blender process; snapshot
                          accuracy verified against a known fixture scene; colour
                          verified in the rendered PNG pixels.

4. Live provider          marked separately, opt-in, skipped without credentials —
                          but a REQUIRED GATE before Spec 002 is declared
                          product-complete (§13.1). Asserts the real model produces
                          schema-valid proposals and correct structured outcomes for
                          the acceptance sentences — never asserts exact prose.
```

`FakeLlmProvider` is the load-bearing piece: it makes intelligence-dependent behaviour
deterministically testable. Tests assert the **platform's** handling of a proposal, not
the model's wording.

Reused infrastructure: `studio_fixtures.slice_stack` (`build_slice_stack`),
`ControlPlaneServer`, `WorkerLinkClient`, the seed fixture working copy, and the Blender
executable resolver. Spec 002 adds a richer fixture scene (several objects, including
two chairs, to make ambiguity real) alongside the existing single-cube fixture, which
stays for Spec 001 regression.

### 13.1 Live-provider acceptance gate

`FakeLlmProvider` remains the default CI provider, and the default suite still requires
no API key, no network, and no paid calls. But a fake provider cannot demonstrate the
product promise of this spec, which is a *real* LLM. So the live tier is opt-in **and**
mandatory for completion:

| | Instruction | Must demonstrate |
|---|---|---|
| L-A | "What objects are in this scene?" | grounded `Answer`, zero mutations |
| L-B | "Move Cube 30 cm left." | schema-valid proposal → validated plan → real verified Blender mutation |
| L-C | "Move Cube 20 cm right and make it beige." | ordered validated operations 0 and 1 |
| L-D | several chairs; "Move the chair right." | `Clarification`, zero mutations |

Rules for these tests:

- assert **structured outcomes and safety boundaries**, never exact prose;
- skip cleanly and loudly when credentials are absent — never silently pass;
- run on Christian's development environment against the configured real provider before
  Spec 002 is declared product-complete;
- provider *choice* may stay deferred to Task 8, but the acceptance itself is not
  optional.

### Acceptance scenarios

| | Instruction | Expected |
|---|---|---|
| A | "What objects are in this scene?" | truthful `Answer` from snapshot; zero Jobs; no preview |
| B | "Move Cube 30 cm left." | saved X decreases 0.30 m; preview; success |
| C | "Rotate Cube 45 degrees around Z." | saved Z rotation = π/4 rad within tolerance; preview |
| D | "Make Cube 20% smaller." | `set_object_dimensions` with absolute 1.6 m targets; verified dimensions = 0.8 × original; preview |
| E | "Make Cube a warm beige." | material base colour verified in the saved file **and visible in the preview pixels** |
| F | "Move Cube 20 cm right and make it beige." | ordered ops 0, 1 with chained scene versions; both persist; **one** browser reply |
| G | two chairs exist; "Move the chair right." | `Clarification`; zero Jobs; scene unchanged |
| H | instruction demanding Python/shell/file access | refused inside the semantic boundary; no such Job exists; scene unchanged |
| I | scene modified externally after the snapshot | `SCENE_VERSION_MISMATCH`; nothing mutated; snapshot refreshed |

Plus: the Spec 001 mandatory E2E remains green unchanged (Requirement 12), and the live
gate L-A…L-D passes on the development environment (§13.1).

---

## 14. Decisions requiring review before implementation

### Closed by review (previously open)

- **Scene-version enforcement** — now an enforced in-lock precondition with chained
  per-operation requirements. Specified in §2.1 and §10.
- **Resize representation** — `set_object_dimensions` with absolute metres is primary for
  user-facing size language; `scale_object` retained for explicit transform-scale intent.
  Specified in §7.
- **Preview colour observability** — Workbench shading colour source changed to the
  object's material, keeping headless, GPU-free, deterministic rendering. Specified in §7.
- **Real-provider acceptance** — live tier stays opt-in but is a required completion gate.
  Specified in §13.1.
- **Digest sensitivity of `scene_version`** — an explicit versioned allow-list projection
  (`studio-scene-v1`), not a hash of the snapshot and not a deny-list. Capture and
  transport metadata are excluded, object order is normalised, floats are quantised and
  formatted deterministically, and a planning-relevant addition requires a digest-version
  bump. Specified in §2.3.

### Still open

1. **Which real provider.** `tech.md` names "Astra / Codex initially" and both exist as
   unimplemented placeholders. The choice depends on which API is actually available; any
   implementation already compatible with `AgentProvider` is acceptable. The design is
   provider-agnostic and the adapter shape is identical either way, so this may stay
   deferred until Task 8 — but §13.1's acceptance gate applies to whichever is chosen.
2. **Structured-output mechanism.** Native JSON-schema/structured output, tool/function
   calling, or constrained decoding — depends on the chosen provider's capabilities.
   Validation is server-side regardless, so this is an efficiency and reliability
   choice, not a safety one.
3. **Snapshot size strategy for large scenes.** Full serialisation is fine for the
   fixture scene; a real room needs a summarisation policy. Deferrable until scenes
   grow, but the shape should be chosen deliberately. Note that a summarisation policy
   must not change the digest projection: §2.3 hashes authoritative scene state, not
   whatever subset was shown to the model in one request.

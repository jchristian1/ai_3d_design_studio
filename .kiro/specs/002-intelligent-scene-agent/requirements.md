# Requirements — 002 Intelligent Scene Agent

## Introduction

Spec 001 proved the vertical slice: one hard-coded sentence shape, interpreted by a
deterministic `RuleBasedProvider`, moving one cube. Every boundary works and is
regression-tested (see `001-core-vertical-slice/verification.md`).

Spec 002 replaces the toy language engine with a **real LLM** and makes the assistant
**scene-aware**, so it can answer questions about a project, perform several kinds of
change, handle multi-step instructions, and ask for clarification instead of guessing.

Spec 001 is the architectural baseline. Its boundaries are not redesigned:

```
Browser → FastAPI → AgentProvider → validated plan → canonical Jobs
       → outbound worker → semantic operation → Blender → save → preview → Browser
```

Spec 002 inserts scene grounding ahead of the provider and widens what the provider
may produce:

```
FastAPI → SceneContextService → authoritative read-only SceneSnapshot (from Blender)
        → ContextBuilder → real AgentProvider
        → Answer | Clarification | AgentPlan | Error
```

### The governing principle

The model gains **language ability**, not **authority**. It proposes; the platform
validates, resolves, and executes. Every Spec 001 safety property — canonical metres,
persisted-plan-before-mutation, retry safety, idempotency, project isolation, no
public Blender, no arbitrary execution — survives unchanged.

## Glossary

- **SceneSnapshot** — an authoritative, read-only, safe description of a project's
  current scene, produced by Blender via the worker. Never model-authored.
- **Scene version** — a content digest of an explicitly enumerated, versioned projection
  of semantic scene state (`studio-scene-v1`, see Requirement 1 and design §2.3), used
  for caching and as an **enforced execution precondition**: a plan reasoned against one
  scene version is not permitted to mutate a different scene version.
- **Proposal** — the model's *untrusted* structured output.
- **Plan** — a *validated, resolved* AgentPlan the platform is willing to execute.
- **Clarification** — a first-class agent outcome that asks the user a question and
  emits no Jobs.
- **Canonical angle** — radians (see Requirement 7).

---

## Requirements

### Requirement 1 — Authoritative scene grounding

**User Story:** As a user, I want the assistant to know what is actually in my
project, so that its answers and changes reflect reality rather than a guess.

#### Acceptance Criteria

1. WHEN the agent needs scene knowledge THEN the platform SHALL obtain a
   `SceneSnapshot` derived from the current saved Blender project, not from model
   memory or a database summary.
2. WHEN a SceneSnapshot is produced THEN it SHALL be read-only: producing it SHALL
   NOT mutate the project, save the `.blend`, or create a preview.
3. WHEN a SceneSnapshot is produced THEN it SHALL carry `project_id`, the scene's
   unit configuration, a `scene_version` identity, and a list of objects.
4. WHEN an object appears in a SceneSnapshot THEN it SHALL include its stable
   `studio_object_id` where one exists, its name, object type, world position in
   metres, dimensions in metres, rotation, scale, a basic material summary, and safe
   visibility state.
5. WHEN a SceneSnapshot is serialized THEN it SHALL NOT contain a filesystem path, a
   `.blend` location, worker credentials, hostnames, or arbitrary Blender internals.
6. IF the scene cannot be read THEN the platform SHALL return a structured error and
   SHALL NOT fall back to an assumed or stale scene for a mutation decision.
7. WHEN a mutation succeeds THEN any cached SceneSnapshot for that project SHALL be
   invalidated, so a subsequent request reasons about the changed scene.
8. WHEN a mutation plan is produced from a SceneSnapshot THEN the plan SHALL record the
   authoritative `scene_version` it was reasoned against.
9. WHEN the first mutation of a plan is about to execute THEN the platform SHALL verify
   that the authoritative scene state still matches the plan's recorded
   `scene_version`, and this verification SHALL happen while the project lock is held,
   before any recovery point or mutation.
10. IF the authoritative scene version no longer matches THEN the platform SHALL NOT
    mutate Blender, SHALL return the canonical error `SCENE_VERSION_MISMATCH`, SHALL
    refresh the SceneSnapshot, and SHALL require re-resolution and re-planning against
    the new scene state.
11. WHEN a plan reasons about a relationship between objects (for example "move the
    chair closer to the table") THEN per-object `expected_before` verification SHALL NOT
    be treated as sufficient: the scene-version precondition SHALL also hold, because an
    object used only in the reasoning may have moved.
12. WHEN a mutation completes and the project is saved THEN the resulting authoritative
    `scene_version` SHALL be computed from the saved state and reported, so subsequent
    execution and caching reason about a known scene version.
13. WHEN `scene_version` is computed THEN it SHALL be a SHA-256 digest over an
    **explicitly enumerated, versioned projection** of semantic scene state, carrying a
    `digest_version` domain string inside the hashed payload. It SHALL NOT be defined as
    a mechanical hash of every SceneSnapshot field, and SHALL NOT be defined as
    "everything except a deny-list".
14. WHEN the digest projection is defined THEN it SHALL cover at minimum: unit
    configuration, object membership, stable `studio_object_id` where present, object
    name, object type, world position, dimensions, rotation, scale, visible state, and
    the basic material state exposed in the SceneSnapshot.
15. WHEN the digest is computed THEN it SHALL exclude `captured_at`, `scene_version`
    itself, worker id, hostname, filesystem paths, tokens, request and session ids,
    preview metadata, and transport metadata. Two reads of an unchanged scene SHALL
    therefore produce an identical `scene_version`.
16. WHEN the digest is computed THEN object enumeration order SHALL NOT affect it:
    objects SHALL be sorted by a documented total order, preferring stable
    `studio_object_id` where available with a documented fallback, and map/dictionary key
    ordering SHALL be canonical.
17. WHEN floating-point values are serialized for the digest THEN the serialization SHALL
    be deterministic, documented, and shared across languages, so an unchanged Blender
    scene hashes identically across repeated inspections — while remaining fine-grained
    enough that a design-meaningful change cannot be quantised away.
18. WHEN a new field is added to `SceneSnapshot` THEN it SHALL be classified explicitly as
    informational (outside the digest) or planning-relevant (inside the digest, requiring
    a new `digest_version`), and an automated check SHALL fail if a field is left
    unclassified. Adding an informational field SHALL NOT change concurrency semantics.

### Requirement 2 — Real LLM provider behind the existing abstraction

**User Story:** As the platform, I want a real language model reached only through
`AgentProvider`, so that intelligence improves without weakening any boundary.

#### Acceptance Criteria

1. WHEN a real provider is added THEN it SHALL implement the existing
   `AgentProvider` protocol and SHALL be reached only through the provider registry.
2. WHEN provider selection is configured THEN it SHALL be configuration, not code:
   switching providers SHALL require no change to FastAPI routes, `ChatService`,
   the worker, or the frontend.
3. WHEN Spec 002 completes THEN `RuleBasedProvider` SHALL remain available and
   selectable, so deterministic tests and offline development keep working.
4. WHEN credentials are required THEN they SHALL come from environment or
   configuration only, SHALL never be committed, logged, returned through the API, or
   placed in a `NEXT_PUBLIC_*` variable.
5. WHEN the default test suite runs THEN it SHALL NOT require network access, an API
   key, or paid model calls.
6. IF live-provider tests exist THEN they SHALL be explicitly opt-in and SHALL skip
   cleanly when credentials are absent.
7. WHEN a provider returns metadata THEN it SHALL NOT include chain-of-thought,
   hidden reasoning, or raw prompt contents.
8. IF the configured provider is unavailable or unauthenticated THEN the platform
   SHALL return a structured `PROVIDER_UNAVAILABLE` outcome and SHALL NOT fabricate a
   plan.
9. WHEN Spec 002 is declared product-complete THEN the configured **real** provider
   SHALL have been demonstrated, on the development environment, to satisfy the
   live-provider acceptance gate of Requirement 2.10. Passing only with
   `FakeLlmProvider` SHALL NOT be sufficient, because a real LLM provider is the product
   promise of this spec.
10. WHEN the live-provider acceptance gate runs THEN the real provider SHALL
    successfully produce, end to end:
    - a grounded `Answer` for "What objects are in this scene?" with zero mutations;
    - a schema-valid proposal for "Move Cube 30 cm left." that becomes a validated plan
      and a real, verified Blender mutation;
    - ordered validated operations for "Move Cube 20 cm right and make it beige.";
    - a `Clarification` with zero mutations for "Move the chair right." when several
      chairs exist.
    These tests SHALL assert structured outcomes and safety boundaries, never exact
    prose.

### Requirement 3 — Constrained structured model output

**User Story:** As the platform owner, I want the model's output to be data the
platform validates, so that language never becomes execution authority.

#### Acceptance Criteria

1. WHEN the provider calls the model THEN it SHALL request structured output
   constrained to a declared schema of proposable operations.
2. WHEN model output is received THEN the platform SHALL validate it against that
   canonical schema BEFORE any resolution or execution.
3. WHEN model output is invalid, malformed, truncated, or unparseable THEN the
   platform SHALL return a structured error and SHALL emit no Jobs.
4. WHEN model output names an operation the platform does not implement THEN the
   platform SHALL reject it and SHALL emit no Jobs.
5. WHEN model output is processed THEN the platform SHALL NEVER evaluate, execute, or
   shell out to any text the model produced.
6. WHEN model output includes fields the platform did not declare THEN those fields
   SHALL be rejected rather than ignored.
7. WHEN a proposal is accepted THEN the platform SHALL convert it into an internal
   plan through its own construction code, so the model never authors a Job directly.

### Requirement 4 — Read-only questions

**User Story:** As a user, I want to ask about my project, so that I can understand it
without changing anything.

#### Acceptance Criteria

1. WHEN the user asks a question answerable from the SceneSnapshot (for example
   "What objects are in this scene?", "Where is Cube?", "How large is Cube?") THEN the
   platform SHALL return an `Answer`.
2. WHEN an `Answer` is produced THEN the platform SHALL create zero mutation Jobs.
3. WHEN an `Answer` is produced THEN the platform SHALL NOT modify the `.blend`,
   SHALL NOT save, and SHALL NOT generate a new preview.
4. WHEN an answer states a measurement THEN it SHALL be consistent with the
   SceneSnapshot's canonical values.
5. IF a question cannot be answered from the available snapshot THEN the platform
   SHALL say so plainly rather than inventing a value.

### Requirement 5 — Clarification as a first-class outcome

**User Story:** As a user, I want to be asked when my instruction is ambiguous, so
that the assistant never changes the wrong thing.

#### Acceptance Criteria

1. WHEN an instruction matches more than one candidate object THEN the platform SHALL
   return a `Clarification` naming the candidates.
2. WHEN an instruction relies on an unresolvable referent (for example "it", "that
   one", "over there") THEN the platform SHALL return a `Clarification`.
3. WHEN a `Clarification` is returned THEN the platform SHALL create zero mutation
   Jobs and SHALL NOT modify the project.
4. WHEN a `Clarification` is returned THEN the response SHALL carry enough session and
   request context for the user's next message to continue the same intent
   coherently.
5. WHEN the user answers a clarification THEN the platform SHALL resolve the original
   intent against the answer and proceed, or clarify again.
6. WHEN clarification state is retained THEN it SHALL be short-lived and
   session-scoped, and SHALL NOT constitute persistent project memory.
7. IF a clarification is never answered THEN it SHALL expire without side effects.
8. WHEN a `Clarification` is returned THEN it SHALL be presented as a question, not as
   an error.

### Requirement 6 — Object resolution against the authoritative scene

**User Story:** As the platform, I want objects resolved from real scene state, so
that no change is ever applied to an invented object.

#### Acceptance Criteria

1. WHEN the model refers to an object THEN the platform SHALL resolve that reference
   against the authoritative SceneSnapshot.
2. WHEN resolution succeeds THEN the resulting operation SHALL address the object by
   its stable `studio_object_id` where one exists.
3. WHEN the model emits an object identifier THEN the platform SHALL verify it exists
   in the SceneSnapshot and SHALL reject any identifier that does not.
4. WHEN exactly one object matches THEN resolution SHALL succeed.
5. WHEN several objects match THEN the platform SHALL return a `Clarification`
   (Requirement 5).
6. WHEN no object matches THEN the platform SHALL return a structured error or a
   clarification, and SHALL NOT create the object, guess a substitute, or emit a Job.
7. WHEN a browser-selected object is supplied THEN the platform MAY use it to resolve
   an otherwise ambiguous referent, provided it exists in the SceneSnapshot.

### Requirement 7 — Canonical units for distance and angle

**User Story:** As the platform, I want one canonical representation per quantity, so
that conversion bugs cannot reach Blender.

#### Acceptance Criteria

1. WHEN distances or dimensions cross any boundary THEN they SHALL be in metres, as
   established by Spec 001.
2. WHEN rotations cross any boundary THEN they SHALL be in **radians**, which SHALL be
   the single canonical internal angular unit.
3. WHEN a user expresses an angle in degrees THEN conversion to radians SHALL happen
   exactly once, in `packages/spatial`, above the worker.
4. WHEN a user expresses a resize as a proportion (for example "20% smaller") THEN the
   canonical mutation SHALL be `set_object_dimensions` carrying absolute desired
   dimensions in metres, derived from the authoritative SceneSnapshot above the worker.
   The worker SHALL NEVER receive a percentage.
5. WHEN a user expresses an explicit transform-scale intent THEN `scale_object` SHALL be
   used, carrying validated absolute unitless scale values, not a relative multiplier
   applied to whatever the current scale happens to be.
6. WHEN the worker or an MCP operation receives a value THEN it SHALL already be
   canonical: no unit strings, no degree values, no percentages, no direction tokens,
   no colour names.
7. WHEN unit conversion logic exists THEN it SHALL exist only in `packages/spatial`;
   no duplicated cm→m or degree→radian arithmetic SHALL appear elsewhere in runtime
   code.
8. WHEN world-space directions are interpreted THEN Spec 001's mapping SHALL hold:
   right +X, left −X, forward +Y, back −Y, up +Z, down −Z; camera-relative
   interpretation remains deferred.

### Requirement 8 — Semantic mutation operations

**User Story:** As a user, I want to move, rotate, resize, and recolour objects, so
that the assistant is useful beyond a single translation.

#### Acceptance Criteria

1. WHEN Spec 002 completes THEN the platform SHALL support these semantic operations:
   `move_object` (existing), `rotate_object`, `scale_object`,
   `set_object_dimensions`, `set_material_color`, and the read operation
   `inspect_scene`.
2. WHEN any mutation executes THEN it SHALL follow Spec 001 execution safety: a plan
   persisted before mutation, an absolute desired-after target rather than a blind
   relative increment on retry, a recovery point, post-mutation verification against
   the saved file, a durable save, and structured errors.
3. WHEN any mutation is retried THEN it SHALL reuse its persisted plan and SHALL NOT
   recompute its expected-before state from the current scene.
4. WHEN a rotation is applied THEN the operation SHALL specify an axis and a canonical
   absolute target orientation, and the result SHALL be verified from the saved file.
5. WHEN a scale or dimension change is applied THEN the resulting dimensions SHALL be
   verified from the saved file within a documented tolerance.
6. WHEN a material colour is applied THEN the worker SHALL receive explicit validated
   numeric colour values in a documented canonical colour space, never a colour name.
7. WHEN `inspect_scene` executes THEN it SHALL be read-only: no lock-for-write, no
   recovery point, no save, and no preview.
8. WHEN an operation is not implemented THEN the platform SHALL reject it before the
   worker rather than attempting a partial equivalent.
9. WHEN material editing is considered THEN Spec 002 SHALL remain limited to a basic
   colour operation; full shader/node authoring is out of scope.
10. WHEN a resize is requested THEN the platform SHALL choose the operation by intent:
    `set_object_dimensions` expresses **design-space size intent** ("make it 20%
    smaller", "make it 1.6 m wide") and is the preferred form for user-facing resize
    language; `scale_object` expresses **explicit transform-scale intent** ("set its
    scale to 0.8"). Both SHALL be retry-safe with persisted expected-before and
    desired-after values.
11. WHEN a material colour change succeeds THEN the generated preview SHALL visibly
    reflect the new colour. A preview configuration that cannot display material colour
    SHALL NOT be accepted as final behaviour; the preview configuration SHALL be
    adjusted deliberately while preserving headless operation, no hard GPU requirement,
    deterministic output, and no path metadata in the image.

### Requirement 9 — Multi-operation requests

**User Story:** As a user, I want to give one instruction containing several changes,
so that I do not have to type them separately.

#### Acceptance Criteria

1. WHEN one request implies several changes THEN the platform SHALL produce an ordered
   `AgentPlan` whose operations carry `operation_index` 0, 1, 2, … in intended
   execution order.
2. WHEN a plan contains several operations THEN all resulting Jobs SHALL share the
   originating `request_id`.
3. WHEN mutation identity is derived THEN it SHALL remain
   `project_id + request_id + operation_index`, as established in Spec 001.
4. WHEN a multi-operation request is retried verbatim THEN no operation SHALL be
   applied a second time.
5. WHEN a multi-operation request is retried after partial completion THEN only the
   operations not yet applied SHALL execute.
6. WHEN a genuinely new `request_id` carries the same instruction THEN the changes
   SHALL be applied again intentionally.
7. WHEN operations execute THEN they SHALL execute in `operation_index` order.
8. IF an operation fails THEN the platform SHALL stop, SHALL NOT execute later
   operations, and SHALL report per-operation status distinguishing applied, failed,
   and not-attempted.
9. WHEN a plan partially fails THEN the platform SHALL NOT claim overall success, and
   SHALL NOT silently roll back already-applied operations; the documented semantics
   are sequential, non-atomic, and resumable.
10. WHEN operation 0 of a plan executes THEN its scene-version precondition SHALL be the
    `scene_version` the plan was reasoned against.
11. WHEN a later operation of an already-approved ordered plan executes THEN its
    scene-version precondition SHALL be the authoritative scene version resulting from
    the preceding operation, so an intentional change made by operation 0 SHALL NOT
    cause operation 1 to be rejected.
12. WHEN the scene changes for any reason other than a preceding operation of the same
    plan THEN the mismatch SHALL still be detected and the remaining operations SHALL
    NOT execute.

### Requirement 10 — Safety and threat resistance

**User Story:** As the platform owner, I want untrusted language to be unable to
escape the semantic boundary, so that adding an LLM does not add an attack surface.

#### Acceptance Criteria

1. WHEN a user message contains an injection attempt THEN it SHALL NOT cause shell
   execution, Python evaluation, subprocess launch, or filesystem access.
2. WHEN model output is processed THEN it SHALL NOT be able to introduce a new worker
   protocol message type.
3. WHEN model output is processed THEN it SHALL NOT be able to supply a `.blend` path
   or any filesystem path.
4. WHEN model output is processed THEN it SHALL NOT be able to override the trusted
   `project_id`, `user_id`, or `session_id`.
5. WHEN model output is processed THEN it SHALL NOT be able to bypass object
   resolution against the SceneSnapshot.
6. WHEN prompts are constructed THEN they SHALL contain no credentials, no filesystem
   paths, and no worker token.
7. WHEN Spec 002 completes THEN every Spec 001 security invariant SHALL still hold:
   the workstation never listens, Blender and MCP are not publicly exposed, the
   protocol vocabulary remains closed, and no arbitrary-execution tool exists.
8. WHEN the model requests an unavailable capability THEN the platform SHALL refuse
   with a structured outcome rather than approximating it.

### Requirement 11 — Browser experience

**User Story:** As a user, I want one coherent reply per thing I say, so that the
conversation stays readable even when the system does several things internally.

#### Acceptance Criteria

1. WHEN the user sends one message THEN the transcript SHALL show exactly one user
   entry and exactly one Studio reply slot for it, as established in Spec 001.
2. WHEN one request produces several internal Jobs THEN the browser SHALL still show
   one Studio reply, not one per operation.
3. WHEN the outcome is an `Answer` THEN the browser SHALL render it as an assistant
   reply with no mutation progress and no preview change.
4. WHEN the outcome is a `Clarification` THEN the browser SHALL render it as a
   question and SHALL allow the user to answer it in the normal input.
5. WHEN a mutation is in progress THEN the browser SHALL show progress and SHALL show
   final success or a readable failure.
6. WHEN a multi-operation plan partially fails THEN the browser SHALL communicate what
   was applied and what was not, without claiming overall success.
7. WHEN a preview is produced THEN the browser SHALL display it, per Spec 001, and a
   colour change SHALL be visible in the displayed preview.
8. WHEN a request is rejected because the scene changed underneath it THEN the browser
   SHALL explain that the project changed and that nothing was modified, rather than
   showing an internal version identifier.
9. WHEN anything is rendered THEN it SHALL contain no filesystem path, credential,
   job identifier, provider prompt, or internal worker detail.

### Requirement 12 — Regression compatibility with Spec 001

**User Story:** As a developer, I want Spec 001's proven behaviour to keep working, so
that new intelligence does not cost existing correctness.

#### Acceptance Criteria

1. WHEN Spec 002 completes THEN the Spec 001 mandatory E2E scenario SHALL still pass
   unchanged in intent: "Move Cube 50 cm to the right." moves the cube to X = 0.50 m,
   saves, previews, and reports success.
2. WHEN Spec 002 completes THEN all Spec 001 failure-case E2E tests SHALL still pass:
   worker/Blender unavailable, invalid object, and lock conflict, each with a
   structured error and no project corruption.
3. WHEN Spec 002 completes THEN Spec 001's idempotency behaviour SHALL still hold: a
   verbatim retry does not re-apply, and a new `request_id` applies again.
4. WHEN the agent result model is extended THEN existing callers SHALL be migrated
   deliberately, and `RuleBasedProvider` SHALL keep producing valid outcomes.
5. WHEN canonical contracts are extended THEN the language-neutral JSON Schemas SHALL
   remain the source of truth, with Python and TypeScript representations kept in
   parity and conformance tests passing.
6. WHEN Spec 002 completes THEN a fresh environment SHALL still install and import
   every package with no `PYTHONPATH`, and the frontend SHALL still type-check and
   build.
7. WHEN the preview configuration changes THEN preview determinism SHALL be preserved
   (identical input producing identical bytes), and any recorded preview constant SHALL
   be re-baselined explicitly rather than an assertion being loosened.

---

## Out of scope (explicitly deferred)

These are deliberately **not** part of Spec 002:

- authentication, real users, production authorization
- billing, quotas, cost controls
- multi-user collaboration
- production deployment, TLS termination, cloud infrastructure
- multiple worker scheduling
- Three.js interactive scene, object selection by clicking
- live viewport streaming, WebRTC
- asset or furniture libraries
- persistent long-term project memory, design-decision history
- uploaded floor plans or photos as model context
- version history or undo UI
- full material/node graph editing
- photorealistic final render workflow
- camera-relative direction interpretation

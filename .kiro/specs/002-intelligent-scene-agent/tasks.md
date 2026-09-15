# Tasks — 002 Intelligent Scene Agent

Implementation plan for making the assistant scene-aware and genuinely intelligent
while preserving every Spec 001 boundary. Each task is incremental, independently
testable, references requirements, and ends in verifiable behaviour.

Spec 001 is the baseline: reuse `packages/contracts` as the source of truth,
`packages/spatial` as the only conversion site, the existing Job lifecycle, the
outbound worker link, the `SceneAdapter`/`bpy` confinement, and
`tests/fixtures/studio_fixtures/slice_stack.py` for assembly.

The default test suite must stay **offline and deterministic**: no network, no API key,
no paid model calls. Live-provider tests are opt-in and skip without credentials — but
passing them against a real provider is a required gate for completion (Task 16).

Four decisions were closed by review before Task 1 and are now binding:

1. `scene_version` is an **enforced** in-lock execution precondition with per-operation
   chaining (design §2.1, §10), computed from an **explicit versioned digest projection**
   `studio-scene-v1` (design §2.3) — Tasks 1, 3, 4, 5, 13.
2. `set_object_dimensions` with absolute metres is the **primary** resize representation;
   `scale_object` covers explicit transform-scale intent (design §7) — Tasks 2, 11.
3. The preview configuration changes so material colour is **visibly** rendered, while
   staying headless, GPU-free and deterministic (design §7) — Task 12.
4. The real provider must pass a **live acceptance gate** before Spec 002 is declared
   product-complete (design §13.1) — Tasks 9, 16.

---

- [x] 1. SceneSnapshot canonical contracts
  - Add canonical language-neutral schemas in `packages/contracts/schemas/`:
    `scene-snapshot`, `scene-object`, `material-color`, and extend `job-type` with the
    read operation `inspect_scene`.
  - `scene-object` carries `studio_object_id?`, `name`, `object_type`,
    `world_position_meters`, `dimensions_meters`, `rotation_euler_radians`, `scale`,
    `material?` (basic summary), `visible`. `scene-snapshot` carries `project_id`,
    `scene_version`, unit configuration, `objects[]`, `captured_at`.
  - `additionalProperties: false` throughout, so no filesystem path, `.blend` location,
    hostname, worker token, or arbitrary Blender internal is *representable* — the same
    closure that protects `PreviewArtifact` in Task 10 of Spec 001.
  - Mirror in Python (`studio_types`/`studio_contracts`) and TypeScript, add corpus
    cases to `conformance-cases.json`, and keep `SCHEMA_FILES` parity plus the
    cross-language verdict-parity test green.
  - Define `scene_version` as SHA-256 over the **explicit versioned projection**
    `studio-scene-v1` specified in design §2.3 — an allow-list, not a hash of the
    snapshot and not a deny-list. The projection covers unit configuration and, per
    object: membership, `studio_object_id` where present, name, object type, world
    position, dimensions, rotation, scale, visibility, and exposed basic material state.
  - Exclude from the digest: `captured_at`, `scene_version` itself, worker id, hostname,
    filesystem paths, tokens, request/session ids, preview metadata, transport metadata.
    `project_id` is excluded too — domain separation comes from the `digest_version`
    string inside the hashed payload, and the digest answers "same scene state?", not
    "whose scene?".
  - Implement the canonical serialization rules in both languages: UTF-8, sorted keys, no
    insignificant whitespace, explicit `null` for absent optional values, JSON literals
    for booleans/null, and non-finite numbers as a hard error rather than a serialised
    token.
  - Normalise object order with the documented total order — `(0, studio_object_id)` when
    a stable id is present, else `(1, name)` — and assert the resulting key sequence is
    strictly increasing, refusing an ambiguous snapshot rather than digesting an arbitrary
    order.
  - Implement the documented float rule: quantise to 1e-6 (position/dimensions in metres,
    rotation in radians, scale, colour channels, `scale_length`), round half to even,
    format with exactly six decimals and never exponent notation, and normalise negative
    zero. The quantum is part of `studio-scene-v1`; changing it changes the digest
    version.
  - Add the **projection-completeness guard**: enumerate the SceneSnapshot schema's fields
    and assert each appears in either the digest projection or an explicit informational
    list, so a future field cannot silently change concurrency semantics.
  - Add shared cross-language digest vectors to the corpus so Python and TypeScript
    produce byte-identical canonical JSON and identical hex digests.
  - Prove the eleven digest properties (pure, no Blender needed — the fixture-backed
    equivalents follow in Task 3):
    1. repeated inspection of an unchanged scene → identical `scene_version`;
    2. a different `captured_at` → unchanged `scene_version`;
    3. a different object enumeration order → unchanged `scene_version`;
    4. changed position → changed `scene_version`;
    5. changed dimensions → changed;
    6. changed rotation → changed;
    7. changed scale → changed;
    8. changed exposed material colour → changed;
    9. changed visibility → changed;
    10. added or removed object → changed;
    11. any excluded metadata changed alone → unchanged.
  - Add `SCENE_VERSION_MISMATCH` to `error-code.schema.json` (mirrored in both
    languages), since scene-version enforcement is a closed decision (design §2.1).
  - Extend the `job` schema's payload discrimination for `inspect_scene` (no payload
    beyond the target project) following the existing `allOf` conditional pattern, and
    declare `required_scene_version` on mutating plans/payloads so a plan cannot be
    represented without the scene it was reasoned against.
  - No agent code, no worker code, no Blender in this task.
  - _Requirements: 1.3, 1.4, 1.5, 1.8, 1.13, 1.14, 1.15, 1.16, 1.17, 1.18, 12.5_

- [ ] 2. Canonical angle, scale and colour utilities
  - Add `angles` to `packages/spatial` (TypeScript + Python): degrees → radians as the
    single conversion site, with **radians as the canonical internal angular unit**.
  - Add canonical schemas for the new cross-boundary vocabulary needed by the operation
    catalogue (`angle-unit`, `angle-measurement`, `scale-factor`, `dimensions` or
    equivalent), keeping the canonical-schema-first discipline.
  - Resolve proportional resize language to **absolute dimensions in metres**: "20%
    smaller" against snapshot dimensions `2.00 × 2.00 × 2.00` becomes
    `1.60 × 1.60 × 1.60`, computed above the worker. Percentages never cross the worker
    boundary (design §7).
  - Provide separate, explicitly named helpers for size intent (dimensions in metres) and
    transform-scale intent (absolute unitless factors), so the two cannot be confused at
    a call site.
  - Add colour handling: a small deterministic named-colour vocabulary plus validation
    of linear sRGB RGBA floats in `[0, 1]`, rejecting out-of-range and non-finite values
    with `INVALID_UNITS`.
  - Keep these utilities pure: no Blender, no AI, no scene state, no camera state, no
    I/O — the `packages/spatial` independence rule from `structure.md`.
  - Enforce the single-conversion-site rule with the Spec 001 style AST/source scan: no
    degree→radian arithmetic, `math.pi/180`, `* 0.01`, or percentage maths anywhere in
    runtime code outside `packages/spatial`.
  - Unit + shared-corpus + cross-language parity tests: `45 deg → π/4`,
    `90 deg → π/2`, `-45 deg → -π/4`, `20% smaller of 2 m → 1.6 m`, `"warm beige" → a
    stable validated RGBA`.
  - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 12.5_

- [ ] 3. Read-only `inspect_scene` operation and authoritative `scene_version`
  - Widen `SceneAdapter` with `list_objects()`, `read_object_state(handle)` and
    `scene_version()`, and implement them in `adapters/blender_scene.py` — still the only
    `bpy` module, still no query language and no fuzzy search.
  - Add `blender_mcp/tools/inspect_scene.py` producing a canonical `SceneSnapshot`
    (Task 1) with its `scene_version`, testable against the existing in-memory fake
    adapter.
  - Compute the digest inside the Blender invocation that already loads the project, so
    the authoritative version costs no extra subprocess and no extra file read.
  - Classify jobs as **mutating** or **read** from `job_type` in `WorkerExecutor`: a read
    job takes no exclusive write lock, writes no recovery point, does not save, and
    generates no preview.
  - Prove read-only-ness the hard way: SHA-256 the `.blend` before and after an
    `inspect_scene` job and assert it is byte-identical, and assert no recovery copy,
    no journal mutation record, and no artifact were created.
  - Verify snapshot accuracy against a real fixture scene with `-m blender`: reported
    positions, dimensions, rotations, scale and `studio_object_id` must match the
    generated fixture spec.
  - Verify the digest against **real Blender**, not just in memory: repeated
    `inspect_scene` calls on an unchanged file produce an identical `scene_version`; and
    a real mutation of position, dimensions, rotation, scale, material colour,
    visibility, or object membership changes it. This is the fixture-backed counterpart
    to Task 1's pure digest tests.
  - Confirm the float quantisation rule holds against Blender's float32 storage: values
    read from the same saved file digest identically across processes, and a
    design-meaningful change is never quantised away.
  - Add a richer fixture scene (several objects, including **two chairs**, so ambiguity
    is real) alongside the existing single-cube fixture, which stays untouched for
    Spec 001 regression.
  - _Requirements: 1.1, 1.2, 1.12, 1.15, 1.17, 8.1, 8.7, 12.1_

- [ ] 4. SceneContextService in the control plane
  - Add `SceneContextService` beside `ChatService` in `services/api`: given a
    `project_id`, return an authoritative snapshot, dispatching an `inspect_scene` read
    job through the existing worker selection / offer / result / reconciliation path.
  - Cache by `project_id` with its `scene_version`, behind an interface (in-memory for
    Spec 002, matching the Spec 001 precedent for control-plane state).
  - Invalidate the cached snapshot when any mutation for that project reaches terminal
    success, and also on `SCENE_VERSION_MISMATCH`, so the next request reasons about the
    changed scene.
  - Fail closed: if the scene cannot be read, return a structured error. Never fall back
    to a stale or assumed snapshot for a mutation decision (a *stale-snapshot-must-not-be-used*
    test is required here).
  - Record the `scene_version` on every plan produced from a snapshot, and implement the
    **advisory** API-side early rejection when the cached version is already known to be
    stale — documented and tested as advisory only, because the authoritative check is
    the in-lock one in Task 5.
  - Keep the agent out of it: the provider receives a snapshot as data and holds no
    worker connection — assert by AST that `services/agent` imports nothing from the
    worker, the gateway, or the API.
  - _Requirements: 1.1, 1.6, 1.7, 1.8_

- [ ] 5. Enforced scene-version precondition in the execution path
  - In `WorkerExecutor`, evaluate preconditions **inside the project lock, before the
    recovery point and before any write**, in this exact documented order (design §2.1):
    1. already-applied (`desired_after` met within Spec 001 tolerance) → `already_applied`,
       mutate nothing, **skip** the version check;
    2. `loaded scene_version == required_scene_version` → else `SCENE_VERSION_MISMATCH`,
       mutate nothing;
    3. `expected_before` still holds → else `PRECONDITION_MISMATCH`, mutate nothing;
    4. apply the absolute `desired_after`.
  - Step 1 must precede step 2. Add a named regression test for exactly this, because
    reversing them would make a verbatim retry fail as "stale" and break Spec 001's
    idempotency guarantee.
  - Compute and report the **post-mutation** authoritative `scene_version` from the saved
    file, alongside the existing durability verification, and persist it in the worker
    journal record.
  - On mismatch: no recovery copy, no journal mutation record, no preview, lock released,
    project byte-identical (`assert_project_intact`).
  - Document and test why per-object `expected_before` is insufficient: a relational plan
    ("move the chair closer to the table") where the *table* moved must be refused even
    though the chair is exactly where the plan expected.
  - Bound recovery: `ChatService`/`SceneContextService` refresh the snapshot and re-resolve
    and re-plan **at most once automatically** per user request; a second mismatch is
    reported, never looped.
  - Extend the worker protocol additively for the reported scene version, following the
    Task 10 precedent (version bump, `additionalProperties: false` consequences
    documented, forbidden on message types where it is meaningless).
  - _Requirements: 1.9, 1.10, 1.11, 1.12, 8.2, 8.3_

- [ ] 6. Agent outcome union, untrusted proposal model, and `FakeLlmProvider`
  - Replace Spec 001's boolean `AgentResult` with the discriminated
    `AgentOutcome = Answer | Clarification | PlanProposal | AgentError` union
    (`kind` discriminator) in `services/agent/studio_agent/outcome.py`.
  - `PlanProposal` carries the `scene_version` it was reasoned against; a plan without one
    must be unconstructible.
  - Add `proposal.py`: the **untrusted** `ProposedOperation` model and a closed
    operation catalogue, validated against the canonical proposal schema *before* any
    resolution or execution. Unknown operations, unknown fields, malformed, truncated
    and oversized output are all rejected with `VALIDATION_ERROR` and zero Jobs.
  - Bound proposals by construction: maximum operation count and maximum output size,
    with a test proving an oversized or adversarial proposal is refused.
  - Migrate `RuleBasedProvider` deliberately to return `PlanProposal` / `AgentError`,
    keeping its deterministic grammar and all its existing tests passing.
  - Add `FakeLlmProvider`: scripts exact proposals so the whole pipeline — context →
    proposal → validation → resolution → plan → Jobs → outcome — is testable offline.
    This is the load-bearing test seam for the rest of the spec.
  - Keep `ProviderMetadata` to provider name, version and operation count. Assert by
    test that no chain-of-thought, hidden reasoning, or raw prompt text is ever stored,
    logged, or returned.
  - Confirm the model can never author a Job: `JobFactory` accepts only
    platform-constructed `PlannedOperation` values.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 2.7, 12.4_

- [ ] 7. ContextBuilder and ObjectResolver
  - Add `ContextBuilder` as the **only** place that decides what the model sees: system
    rules and operation catalogue, trusted identity, SceneSnapshot, browser selection,
    pending clarification state, bounded recent turns, current user message.
  - Extend `AgentContext` with those fields (Spec 001 listed them as future work).
    Bound recent turns and candidate lists so a long conversation cannot grow the prompt
    without limit; summarise large scenes rather than serialising them whole.
  - Assert absence, not just presence: tests must prove the assembled context contains
    no credential, no filesystem path, no `.blend` location, no worker token, and no
    other project's data. Route code must never build prompts (AST-tested).
  - Add `ObjectResolver` applying the documented ordered rules against the authoritative
    snapshot: explicit stable id (must exist) → exact name → case-insensitive name →
    browser selection → clarification answer → several matches = clarification → none =
    `OBJECT_NOT_FOUND`.
  - The resolver performs no fuzzy or semantic matching and never creates or substitutes
    an object; a model-invented `studio_object_id` is rejected.
  - Resolved operations address objects by stable `studio_object_id` where one exists and
    carry canonical values only — the untrusted → trusted transition — including the
    `scene_version` the resolution was performed against.
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 10.4, 10.5, 10.6_

- [ ] 8. Clarification lifecycle
  - Add `ClarificationStore`: session-scoped, size- and count-bounded, expiring
    in-memory state holding the pending question, its candidate objects, and the
    original intent. Explicitly *conversational continuity*, not project memory.
  - A `Clarification` creates **zero** Jobs: nothing locked, nothing planned, nothing
    saved, no preview, no `.blend` write (assert by digest).
  - The response carries enough session/request context for the user's next message to
    continue the same intent without repeating the instruction.
  - Answering a clarification resolves the original intent against the answer and either
    proceeds to a plan or clarifies again; an unanswered clarification expires with no
    side effects.
  - A clarification is surfaced as a question, never as an error — distinct outcome kind,
    distinct HTTP shape, distinct UI treatment.
  - A clarification answered after the scene changed must be re-grounded against a fresh
    snapshot rather than against the stale candidate set.
  - Tests with `FakeLlmProvider`: two chairs + "Move the chair right." → clarification,
    zero Jobs, scene byte-identical; then "the second one" → exactly one Job.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

- [ ] 9. Real LLM provider adapter
  - Implement one real provider under `services/agent/studio_agent/providers/`,
    reached only through the existing provider registry and `AgentProvider` protocol.
    Which provider (Astra, Codex, or another implementation already compatible with
    `AgentProvider`) may be decided at this task (design §14, still-open item 1).
  - Request structured output constrained to the declared proposal schema; the exact
    mechanism (native JSON schema, tool calling, or constrained decoding) is a reviewed
    open decision (design §14, still-open item 2). Server-side validation is
    unconditional either way.
  - Provider selection is configuration, not code: switching providers must require no
    change to routes, `ChatService`, the worker, or the frontend (AST-tested, as in
    Spec 001 Task 13).
  - `RuleBasedProvider` remains available and selectable so offline development and the
    Spec 001 deterministic tests keep working.
  - Credentials from environment/configuration only: excluded from `repr`, never logged,
    never returned by the API, never in a `NEXT_PUBLIC_*` variable — the
    `STUDIO_WORKER_TOKEN` discipline.
  - Provider owns prompt text, model parameters, and transport retry/backoff; it does not
    own object resolution or Job construction, so a misbehaving provider cannot reach
    Blender.
  - Unavailable, unauthenticated, rate-limited or timed-out provider → structured
    `PROVIDER_UNAVAILABLE`, never a fabricated plan.
  - Write the opt-in live acceptance tests L-A…L-D here (design §13.1). They skip cleanly
    **and loudly** without credentials, never silently pass, and assert structured
    outcomes and safety boundaries rather than prose. They are executed as a gate in
    Task 16.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.8, 2.10_

- [ ] 10. `rotate_object`
  - Add `blender_mcp/tools/rotate_object.py` and `SceneAdapter.set_rotation_euler`,
    layered handler → service → adapter so behaviour is testable against the fake.
  - Canonical payload: target plus axis plus an **absolute** `desired_after_radians`,
    with `expected_before_radians` persisted in the plan — the same three-path
    already-applied / apply / `PRECONDITION_MISMATCH` structure as `move_object`, so a
    retry never double-rotates — plus the Task 5 scene-version precondition.
  - Add a documented rotation tolerance calibrated against measured Blender float
    round-trip error, in the existing `tolerance.py`, rather than a guessed epsilon.
  - Full Spec 001 execution contract: lock → plan persisted before mutation →
    preconditions → recovery point → absolute write → verify **from the saved file** →
    durable save → post-mutation scene version → preview.
  - Canonical schemas for the payload/plan/result added first, mirrored in both
    languages, with corpus cases and lifecycle conditionals (no failure claiming
    success, no unverified apply).
  - `-m blender` verification: "Rotate Cube 45 degrees around Z." → saved Z rotation is
    π/4 rad read by a fresh Blender process; verbatim retry leaves it at π/4.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 7.2_

- [ ] 11. `set_object_dimensions` (primary) and `scale_object`
  - Add `blender_mcp/tools/dimensions.py` for `set_object_dimensions` and
    `blender_mcp/tools/scale_object.py` for `scale_object`, plus
    `SceneAdapter.set_dimensions` and `set_scale`.
  - Document and enforce the intent split (design §7):
    `set_object_dimensions` = **design-space size intent**, the canonical target of
    user-facing resize language, carrying absolute dimensions in metres;
    `scale_object` = **explicit transform-scale intent**, carrying absolute validated
    unitless scale values. Neither carries a relative multiplier.
  - "Make Cube 20% smaller." resolves above the worker to
    `set_object_dimensions(1.60, 1.60, 1.60)` from snapshot dimensions
    `2.00 × 2.00 × 2.00`. Assert by test that no percentage and no relative factor is
    representable in the worker-facing payload.
  - Absolute desired-after targets and persisted `expected_before` for both operations,
    so a retry cannot compound a resize, and both under the Task 5 scene-version
    precondition — the derived target is only correct for the scene it was derived from.
  - Verify resulting dimensions from the saved file within a documented tolerance;
    `-m blender`: "Make Cube 20% smaller." → verified dimensions are 0.8 × original, and
    a verbatim retry leaves them at 0.8 × original.
  - Document how Blender's `scale` and `dimensions` interact through mesh bounds, and
    which of the two each operation writes, so the difference is not folklore.
  - _Requirements: 8.1, 8.2, 8.3, 8.5, 8.10, 7.4, 7.5_

- [ ] 12. `set_material_color` with a visibly correct preview
  - Add `blender_mcp/tools/set_material_color.py` plus
    `SceneAdapter.set_material_base_color`, creating or reusing a basic material as
    documented; full shader/node authoring stays out of scope.
  - The worker receives explicit validated numeric linear sRGB RGBA values only — never
    a colour name. Name interpretation happens above the worker (Task 2), and the result
    is range-validated before a Job exists.
  - Absolute desired-after colour with persisted `expected_before`, verified from the
    saved file within tolerance; retry-safe like every other mutating operation, and
    under the Task 5 scene-version precondition.
  - **Change the preview configuration deliberately** (design §7): set the Workbench
    shading colour source to the object's material while keeping flat lighting, a fixed
    background, fixed resolution and framing, and no sampling — so the render stays
    headless, GPU-free and deterministic while actually showing the colour. "The preview
    engine does not show material colour" is not an acceptable outcome.
  - Preserve every Spec 001 preview safety property: all stamp flags disabled (no
    `.blend` path in PNG `tEXt` metadata), the temporary camera never saved (SHA-256
    before/after), no path or hostname in the served bytes.
  - Re-baseline any recorded preview constant **explicitly** rather than loosening an
    assertion, and re-assert determinism (same input → identical bytes) under the new
    configuration.
  - `-m blender`: "Make Cube a warm beige." → base colour verified in the reopened
    `.blend`, **and** pixels sampled in the object's region in the served PNG within
    tolerance of the requested colour, **and** the image differs from the pre-change
    preview.
  - _Requirements: 8.1, 8.2, 8.6, 8.9, 8.11, 7.4, 11.7, 12.7_

- [ ] 13. Multi-operation orchestration with chained scene versions
  - Add `PlanCoordinator` in the API: offer operation *n* only after *n−1* reaches
    terminal success, executing strictly in `operation_index` order.
  - Mutation identity stays `project_id + request_id + operation_index`, so
    per-operation idempotency comes from the existing derived key with no new mechanism.
  - Implement scene-version chaining (design §10): operation 0's
    `required_scene_version` is the plan's original version; operation *n*'s is the
    post-mutation version reported by operation *n−1*, written and **persisted in the
    journal before operation *n* is offered**, so a resumed retry re-derives the same
    chain from the journal rather than from live API memory.
  - Semantics are **sequential, non-atomic, resumable, fail-fast**, and documented as
    such: on failure of operation *n*, earlier operations remain applied and saved,
    later operations are not attempted, and nothing is silently rolled back.
  - Report per-operation status distinguishing `applied` · `failed` · `not_attempted`
    through `JobReconciler` and the job-status/chat response; partial failure never
    claims overall success.
  - Retry of the same `request_id` **resumes**: already-applied operations resolve as
    duplicates and are skipped, and execution continues from the first unapplied one; a
    genuinely new `request_id` applies the whole plan again intentionally.
  - Tests with `FakeLlmProvider`: a 3-operation plan; verbatim retry applies nothing
    twice; retry after partial completion finishes only the remainder; a forced failure
    at index 1 leaves index 0 durable, index 2 untouched, and the project verified
    consistent.
  - Two named chaining tests are mandatory: (a) operation 0's intentional change does
    **not** reject operation 1; (b) an **external** modification between operation 0 and
    operation 1 **does** reject operation 1 with `SCENE_VERSION_MISMATCH`, leaving
    operation 0 applied and operation 2 not attempted.
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [ ] 14. Browser experience for answers, questions and partial failure
  - Extend `lib/api` and the pure `lib/session/reducer.ts` with the new outcome kinds,
    keeping HTTP isolated in the client and the reducer pure and unit-tested.
  - One user message → exactly one user transcript entry and exactly one Studio reply
    slot, even when the request produces several internal Jobs. Do **not** render one
    bubble per operation — extend the Task 11 stable-message-id collapsing rather than
    adding a second mechanism.
  - `Answer`: rendered as an assistant reply with no mutation progress and no preview
    change. `Clarification`: rendered as a question, answerable in the normal input.
  - Mutation in progress: aggregate progress for the whole request, then final success
    or a readable failure. Partial failure communicates what was applied and what was
    not, without claiming success.
  - `SCENE_VERSION_MISMATCH` is rendered as "the project changed, nothing was modified",
    with the refreshed state reflected — never as an internal version identifier and
    never as a raw error code.
  - Preview behaviour from Spec 001 preserved, including the "succeeded with
    `preview_error`" case reading as applied-and-saved with a warning, and a colour
    change being visible in the displayed preview.
  - Nothing rendered may contain a filesystem path, credential, job identifier, provider
    prompt, or internal worker detail (assert in tests, as in Task 11).
  - Accessibility maintained: `aria-live` announcements for answers, questions and
    progress; colour never the sole signal; `tsc --noEmit` clean and `next build`
    succeeding.
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9_

- [ ] 15. Mandatory intelligent-agent end-to-end acceptance suite
  - Add `tests/e2e/test_spec002_intelligent_agent.py` covering acceptance scenarios A–I
    against real FastAPI + real worker + real Blender + real PNG, assembled through the
    existing `build_slice_stack`.
  - A: "What objects are in this scene?" → truthful `Answer` grounded in the snapshot,
    zero Jobs, `.blend` byte-identical, no new artifact.
  - B–E: one saved, verified mutation each — move (X decreases 0.30 m), rotate
    (Z = π/4 rad), resize via `set_object_dimensions` (1.60 m from 2.00 m), colour
    (base colour **and** preview pixels) — each read back by a **fresh** Blender process
    and each producing a preview.
  - F: "Move Cube 20 cm right and make it beige." → ordered operations 0 and 1 with
    chained scene versions, both durable, and exactly **one** Studio reply in the
    transcript.
  - G: two chairs; "Move the chair right." → `Clarification`, zero Jobs, scene unchanged;
    then an answer resolves to exactly one mutation.
  - H: an instruction demanding Python execution, shell access, a file path, or a new
    protocol message → refused inside the semantic boundary, no such Job exists, scene
    unchanged, no subprocess spawned beyond the sanctioned Blender invocations.
  - I: the project is modified externally after the snapshot is taken and before the plan
    executes → `SCENE_VERSION_MISMATCH`, nothing mutated, no recovery copy, snapshot
    refreshed, and the bounded single automatic re-plan behaves as documented.
  - Failure cases retested at this tier: provider unavailable, malformed model output,
    invented object id, scene unreadable, no worker, lock conflict, mid-plan failure,
    scene-version mismatch mid-plan, preview failure — each with a structured outcome and
    `assert_project_intact`.
  - The whole suite runs with `FakeLlmProvider` by default so it is deterministic and
    free; the live-provider variant is separately marked and opt-in (Task 9), and gated
    in Task 16.
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 10.1, 10.2, 10.3, 10.7, 10.8, 12.1, 12.2_

- [ ] 16. Final verification, live-provider gate, and closeout
  - Re-run the complete Spec 001 regression surface and require it green: the mandatory
    slice E2E unchanged in intent (`0.00 → 0.50 m`, saved, previewed, reported), all
    Spec 001 failure-case E2E tests, and Spec 001 idempotency behaviour.
  - **Run the live-provider acceptance gate** (design §13.1) against the configured real
    provider on the development environment, with credentials supplied: L-A grounded
    answer, L-B real verified mutation, L-C ordered multi-operation, L-D clarification
    with zero mutation. Record the provider, model, and date in `verification.md`.
    Passing only with `FakeLlmProvider` does not satisfy this gate.
  - Audit the trust boundary against the real code, not the diagram: trace user message
    → `SceneContextService` → `ContextBuilder` → provider → proposal → validation →
    `ObjectResolver` → `PlannedOperation` → `JobFactory` → Job → worker → in-lock
    precondition check → MCP → Blender → save → post-mutation version → preview →
    browser, and confirm no model-authored value reaches Blender unvalidated.
  - Verify the precondition ordering invariant in the shipped code: already-applied is
    evaluated before the scene-version check, and both are evaluated before the recovery
    point and any write, with the lock held.
  - Re-verify every Spec 001 security invariant still holds: the workstation never
    listens, Blender/MCP are not publicly exposed, the protocol enum is closed, `bpy`
    stays confined, no arbitrary-execution tool exists, jobs and artifacts stay
    project-scoped, PNG metadata carries no path under the **new** preview configuration.
  - Re-verify single-conversion-site: no cm→m, degree→radian, percentage, or colour
    arithmetic in runtime code outside `packages/spatial`.
  - Confirm schema parity across both languages for every new contract, a fresh venv
    installing and importing every package with no `PYTHONPATH`, and a clean frontend
    type-check and build.
  - Write `.kiro/specs/002-intelligent-scene-agent/verification.md` mapping all
    Requirement 1–12 acceptance criteria to implementing modules and named tests, and
    recording deliberate scope boundaries carried forward.
  - _Requirements: 2.9, 2.10, 10.7, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

---

## Definition of done

Spec 002 is complete when, and only when:

1. All 16 tasks are `[x]` and every Requirement 1–12 acceptance criterion is verified by
   an executable test recorded in `verification.md`.
2. Acceptance scenarios A–I all pass against real Blender, with results read back from
   the saved `.blend` by a fresh Blender process.
3. The default test suite passes offline with no API key, no network, and no paid model
   calls; live-provider tests skip cleanly when credentials are absent.
4. **The live-provider acceptance gate has passed** on Christian's development
   environment against the configured real provider: L-A grounded answer, L-B real
   verified mutation, L-C ordered multi-operation, L-D clarification with zero mutation —
   with the provider, model and date recorded in `verification.md`. `FakeLlmProvider`
   alone does not satisfy this.
5. Every Spec 001 test suite is still green, including the mandatory slice E2E, the
   failure-case E2E tests, and the idempotency behaviour.
6. No model-authored value reaches Blender without schema validation, object resolution
   against an authoritative snapshot, and platform-side Job construction.
7. **No mutation executes against a scene it was not reasoned against**: the in-lock
   `scene_version` precondition is enforced for every mutating operation, chained across
   multi-operation plans, and evaluated after the already-applied check and before the
   recovery point.
8. `scene_version` is computed from the explicit `studio-scene-v1` projection: repeated
   inspection of an unchanged scene yields an identical version, capture and transport
   metadata and object order cannot affect it, every planning-relevant property change
   does affect it, and the projection-completeness guard passes.
9. A colour change is verified both in the saved `.blend` and in the rendered preview
   pixels, with the preview still headless, GPU-free, deterministic, and free of path
   metadata.
10. No new arbitrary-execution path, no new listening socket on the workstation, no path
    or credential representable in any contract, and no chain-of-thought stored or
    transmitted.


---

## Task 1 — implementation notes

Two deliberate deviations from the task text, both disclosed rather than silently
absorbed:

1. **`required_scene_version` was NOT added to `move-object-plan` yet.** The plan
   schema is the MCP tool's input schema, and `mcp_server.plan_from_wire` rebuilds a
   plan field by field. Declaring an optional precondition field there now would
   produce a schema a caller can populate and a parser that silently drops it — a
   worse failure mode than the field being absent. `scene-version.schema.json` exists
   as the single canonical form, and Task 5 adds the field to the plan, the journal
   record, the parser and the in-lock check together.
2. **`SCENE_VERSION_MISMATCH` is not yet mapped in `services/api/errors.py` or the
   browser message table.** Both fall back safely (an unmapped code becomes
   `INTERNAL`/500 and a generic message, never a 200), nothing produces the code yet,
   and mapping it is API and browser behaviour that Tasks 5 and 14 own. It must be
   mapped to a conflict reason (409) when enforcement lands.

Two things outside the task text were changed, both disclosed:

3. **`studio_fixtures.scene_state_digest` now delegates to the canonical digest**
   (via the documented `scene_body_from_inspection` adapter), so the repository has one
   definition of "scene state digest" rather than two. The value is now prefixed and
   uses the 1e-6 digest quantum, and `scene.name` no longer participates — the Blender
   fixture tests already assert the scene name directly and compare whole inspection
   dictionaries, which is stronger than folding it into a hash. All 23 seed-fixture
   Blender tests and all 78 Blender-marked tests pass.
4. **A latent race was fixed in `tests/integration/test_worker_link_websocket.py`.**
   `test_redelivering_the_same_job_over_the_socket_does_not_move_twice` waited with
   `_await`, which returns as soon as the collection is non-empty — and it already held
   the first result — then asserted `len(results) == 2`. It failed once under the load
   of a full Blender run and passed in isolation. It now waits with the existing
   `_await_count` helper for the count it actually needs.

Known gap, pre-existing and NOT introduced here: the shared TypeScript packages have
no `tsconfig.json`, so `node --test` type-strips without type-checking them. The new
`scene.ts`, `scene.test.ts` and `emit-scene-verdicts.ts` were type-checked explicitly
against the repo's strict settings and are clean; three pre-existing
`as Record<string, unknown>` cast errors remain in `jobs.test.ts` and were left alone.

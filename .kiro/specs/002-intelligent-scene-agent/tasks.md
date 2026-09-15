# Tasks — 002 Intelligent Scene Agent

Implementation plan for making the assistant scene-aware and genuinely intelligent,
with Blender ability provided by an **existing Blender MCP implementation** behind a
platform-owned adapter.

Spec 001 is the baseline: reuse `packages/contracts` as the source of truth,
`packages/spatial` as the only conversion site, the canonical Job lifecycle, the
outbound worker link, the durable journal, and the project lock.

The default test suite must stay **offline and deterministic**: no network, no API key,
no paid model calls, no Blender, and no MCP server. Real-MCP and live-provider tests are
opt-in; the live-provider gate is required for completion (Task 16).

---

## Refactor note (this plan was rewritten)

Tasks 1 and 2 are complete and unchanged. Tasks 3 onward were **rewritten** around one
decision:

> AI 3D Design Studio does not reinvent Blender MCP.

An existing implementation (`ahujasid/blender-mcp`, pinned to `1.9.4` / commit
`7684c6b3`) becomes the Blender capability engine, reached over the real Model Context
Protocol behind `BlenderMcpGateway`. See design §1 for the ownership table and §2 for
the pinned inventory.

**Tasks removed from the previous plan**, because they primarily re-implemented what an
existing Blender MCP already provides:

| Removed task | Replaced by |
|---|---|
| old 3 — platform-owned `inspect_scene` + `SceneAdapter` widening + bpy scene reader | new 3 (spike) + new 5 (normalise MCP reads into `SceneSnapshot`) |
| old 9 — `rotate_object` MCP tool (ours) | new 10 — capability mapping |
| old 10 — `scale_object` / `set_object_dimensions` tools (ours) | new 10 — capability mapping |
| old 11 — `set_material_color` tool (ours) | new 10 — capability mapping |
| old 14/15 — acceptance suites assuming our own tool catalogue | new 14 (MCP-backed) + new 15 (live gate) |

**Work already written for the old Task 3 is retained, not deleted** (design §13): the
platform-owned `inspect_scene`, its Blender script, the read-path guard and the richer
`studio_scene` fixture become the READ ORACLE that Task 5's normalisation is validated
against, and the fixture is required either way. Nothing is deleted until MCP-backed
parity is proven, per Requirement 18.4.

---

- [x] 1. SceneSnapshot canonical contracts
  - Add canonical language-neutral schemas in `packages/contracts/schemas/`:
    `scene-snapshot`, `scene-object`, `scene-units`, `scene-version`, `euler-radians`,
    `scale3`, `material-summary`, `material-color`, `inspect-scene-payload`, and extend
    `job-type` with the read operation `inspect_scene`.
  - `additionalProperties: false` throughout, so no filesystem path, project-file
    location, hostname, worker token, or arbitrary internal is *representable*.
  - Mirror in Python and TypeScript, extend the shared corpus, and keep `SCHEMA_FILES`
    parity plus cross-language verdict parity green.
  - Define `scene_version` as SHA-256 over the **explicit versioned projection**
    `studio-scene-v1`: an allow-list, not a hash of the snapshot and not a deny-list.
    Exclude `captured_at`, `scene_version` itself and `project_id`; normalise object
    order by a documented total order; quantise floats to 1e-6 and serialise them as
    fixed-decimal strings; refuse non-finite values.
  - Add the projection-completeness guard so a future field must be classified as
    informational or planning-relevant.
  - Add `SCENE_VERSION_MISMATCH` to `error-code`.
  - _Requirements: 1.7, 1.8, 1.9, 2.1, 2.6, 2.7, 2.8, 2.9, 18.6_

- [x] 2. Canonical angle, scale and colour utilities
  - Add `angles` to `packages/spatial` (both languages): degrees → radians as the single
    conversion site, with radians canonical. Conversion is NOT normalisation — nothing
    reduces modulo 2π.
  - Add canonical `angle-unit` and `angle-measurement` schemas; the enum holds normalised
    wire values only, with human spellings accepted as tokens above the boundary.
  - Add `sizing`: normalised `(percent, direction)` → absolute factor, with the boundary
    decisions enforced (100% smaller refused, >100% refused, negative percent refused,
    no invented growth ceiling) and a semantic size factor validated as strictly
    positive — deliberately stricter than observed `SceneObject.scale`.
  - Add `colors`: canonical linear sRGB RGBA, the real sRGB transfer function as the ONE
    conversion site, and a deliberately tiny named palette authored in canonical linear
    literals with its encoded provenance test-verified.
  - Add the single-conversion-site source guards (AST in Python, comment/string-stripped
    scan in TypeScript), each proved to detect real violations and to ignore legitimate
    ones.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 18.6_

- [ ] 3. External Blender MCP integration spike + pinned capability inventory
  - **A focused integration spike, read-only. Its purpose is to validate the
    architecture before later tasks are rewritten around assumptions — not to convert
    every operation.**
  - Pin the exact upstream: `blender-mcp==1.9.4`, commit
    `7684c6b3ad2aa0710bbdb1cb06b497c90899ae00`, MIT. Record the pin, its transitive
    deps (`mcp<2,>=1.9.0`, `httpx>=0.27.0`) and the Python floor in project config. No
    `latest` anywhere.
  - Install and configure it WITHOUT copying source: `uvx blender-mcp==1.9.4` for the
    server and `uvx blender-mcp==1.9.4 install-addon` for the add-on, so server and
    add-on are pinned as a pair. Do not vendor `addon.py`, do not fork, do not import
    `blender_mcp.*` internals.
  - Enable the add-on in Blender 5.2, start its server, and verify the
    `get_addon_status` handshake reports matching protocol versions.
  - Connect as an MCP CLIENT over stdio using the official `mcp` SDK (added as our own
    pinned dependency). Discover tools via `tools/list` and record every tool name and
    input schema into a committed inventory artefact.
  - Classify EVERY discovered tool as allowed / guarded / denied, and write the
    classification down. Assert the inventory matches design §2.1's 31 tools; a
    difference must FAIL rather than be absorbed.
  - Call a read-only capability (`get_scene_info`, plus `get_object_info` if needed) and
    normalise the result far enough to prove a canonical `SceneSnapshot` is derivable —
    including which `SceneObject` fields the upstream does NOT report.
  - Prove no mutation: SHA-256 the project file before and after, and assert no save, no
    sibling file, no journal record, no preview.
  - Prove connect → disconnect → reconnect works, and that a dead MCP server surfaces
    `BLENDER_UNAVAILABLE` rather than hanging.
  - Verify and record the loopback posture: the add-on binds `127.0.0.1:9876` by
    default, and a non-loopback host is rejected by our configuration.
  - Verify telemetry is OFF (`DISABLE_TELEMETRY=true`) and that no `user_prompt` we send
    contains user content.
  - **Measure and report:** the N+1 read cost (design D5), whether the add-on can run in
    a headless Blender session or requires an interactive one (D2), and whether
    `get_viewport_screenshot` works headless (D7).
  - **Answer D1 with evidence:** confirm that 1.9.4 exposes no semantic mutation tool
    and that all modelling routes through `execute_blender_code`. Report the three
    options (hybrid / platform-generated templates / different upstream) with findings.
    **Do not silently switch implementation, and do not enable Tier C.** If
    `ahujasid/blender-mcp` proves unsuitable, say why before proposing an alternative.
  - Nothing in this task may become a dependency of production code paths yet: the spike
    lands as a documented, runnable, opt-in integration test plus the inventory record.
  - _Requirements: 9.1, 9.2, 9.8, 9.9, 9.10, 9.11, 10.1, 10.2, 10.3, 11.2, 11.3, 11.7, 15.1, 15.2, 15.3_

- [ ] 4. `BlenderMcpGateway` + tool-policy boundary
  - Add `BlenderMcpGateway` (Protocol) with `capabilities()`, `invoke(capability, args)`
    and `health()`; the worker talks ONLY to this. Nothing above it may name an upstream
    tool (AST-tested across `services/agent`, `services/api`, `apps/web` and the
    contracts).
  - Add `ExistingBlenderMcpGateway` for the pinned upstream, speaking MCP over stdio via
    the official client SDK, owning process lifecycle, timeouts, and reconnect. It must
    not import upstream internals.
  - Add `FakeBlenderMcpGateway` replaying canned MCP responses, so every layer above is
    testable with no Blender and no MCP server. This is the load-bearing offline seam.
  - Add the capability registry: platform capability → upstream tool or ordered tool
    sequence. An unmapped capability is UNAVAILABLE and yields a structured
    capability-named failure — never a substitution with a code-execution tool.
  - Add `McpToolPolicy` with Tiers A/B/C and **fail-closed denial of anything
    unclassified**; evaluate it on our side before any MCP call; derive the model's
    permitted catalogue from it.
  - Enable upstream safe mode as defence-in-depth, and assert in a test that no design
    decision treats it as authorisation.
  - Add the compatibility test: the pinned inventory and schemas are asserted, so an
    upstream change fails loudly.
  - Add the configuration guard: a non-loopback MCP host is rejected in the
    production-safe default; telemetry disabled is verified.
  - _Requirements: 9.3, 9.4, 9.5, 9.10, 10.1–10.8, 11.2, 11.3, 11.7, 11.8, 14.5, 15.2, 15.4, 15.5, 16.1, 16.2_

- [ ] 5. SceneSnapshot normalisation from MCP output
  - Add `mcp/normalize.py`: raw MCP read output → validated canonical `SceneSnapshot`,
    then `compute_scene_version` (the Task 1 digest, unchanged — no second digest).
  - Treat raw MCP output as UNTRUSTED: validate shape and types, reject unexpected or
    non-finite values, and never default a missing field into a plausible one. Absent
    stays absent, so "no material" and "black" remain distinguishable.
  - Compose several read-only capabilities where one is insufficient (`get_scene_info` +
    `get_object_info` per object), and record the measured cost.
  - Translate identity at this boundary only: platform `studio_object_id` ↔ upstream
    object name.
  - Assert units at the boundary and convert through `packages/spatial` if the upstream
    disagrees.
  - Assert that raw MCP output never reaches `AgentProvider` and that no upstream field
    name appears in provider context.
  - **Validate against the retained read oracle:** the platform-owned `inspect_scene`
    path and the `studio_scene` fixture must produce the SAME `scene_version` as the
    MCP-normalised snapshot for the same project. That equality is the parity evidence
    Requirement 18.4/18.5 depends on.
  - Wire `SceneContextService` (cache by `project_id` + `scene_version`, invalidate on
    successful mutation and on `SCENE_VERSION_MISMATCH`, fail closed on read failure) and
    dispatch the read as a job that HOLDS the project lock (Requirement 1.12) while
    writing no journal record, no recovery point, no save and no preview.
  - _Requirements: 1.1–1.6, 1.10–1.16, 2.5, 8.9, 14.5_

- [ ] 6. Scene-version and durable mutation guard around MCP calls
  - Add `MutationGuard` in the worker implementing the documented order: acquire lock →
    inspect authoritative state → verify `scene_version` → **already-applied?** →
    verify `expected_before` → persist intent + required version + expected/desired +
    recovery point → invoke the MCP mutation capability → inspect again → verify
    desired-after → compute resulting version → journal completion.
  - Already-applied is evaluated BEFORE the version check, with a named regression test:
    reversing them makes a verbatim retry fail as stale and breaks Spec 001 idempotency.
  - Assume the external tool is NOT retry-idempotent. A retry after a crash inspects
    first and never blindly re-invokes.
  - Keep mutation identity `project_id + request_id + operation_index`.
  - Keep the guard free of Blender geometry logic — an AST test asserts it contains no
    bpy and no geometry maths, so it stays OUR safety semantics rather than a second
    implementation.
  - Preserve recovery points and the "failure never leaves a partially mutated
    operation" property; on verification failure, do not report success.
  - _Requirements: 2.1–2.5, 12.1–12.7_

- [ ] 7. ContextBuilder + agent outcome and proposal model
  - Replace the boolean `AgentResult` with the discriminated
    `AgentOutcome = Answer | Clarification | PlanProposal | AgentError`; migrate
    `RuleBasedProvider` deliberately.
  - Add the untrusted `ProposedOperation` model over the **platform capability**
    catalogue, schema-validated before any resolution, with bounded size and operation
    count. Unknown capabilities, unknown fields, malformed and oversized output are all
    refused with zero Jobs.
  - Add `ContextBuilder` as the only place that decides what the model sees: system
    rules, permitted CAPABILITIES (from the policy), trusted identity, SceneSnapshot,
    browser selection, pending clarification, bounded recent turns, current message.
  - Assert absence: no credential, no filesystem path, no worker token, no MCP transport
    detail, no upstream tool name, no other project's data. Route code never builds
    prompts.
  - Add `FakeLlmProvider` scripting exact proposals, so the whole pipeline is testable
    offline.
  - `ProviderMetadata` stays name/version/count; no chain-of-thought is stored or
    transmitted.
  - _Requirements: 3.7, 4.1–4.8, 5.1–5.5, 14.1–14.6_

- [ ] 8. ObjectResolver + clarification lifecycle
  - Add `ObjectResolver` with the documented ordered rules against the authoritative
    snapshot; a model-invented identifier is rejected; no fuzzy or semantic matching.
  - Resolved operations address stable ids and carry canonical values only, plus the
    `scene_version` they were resolved against.
  - Add `ClarificationStore`: session-scoped, bounded, expiring; zero Jobs; a
    clarification is a question, not an error; answering re-grounds against a fresh
    snapshot if the scene moved.
  - Prove with `FakeLlmProvider`: two chairs + "Move the chair right." → clarification,
    zero Jobs, project unchanged; then "the second one" → exactly one Job.
  - _Requirements: 6.1–6.8, 7.1–7.8_

- [ ] 9. Real LLM provider
  - Implement one real provider behind the existing registry (D3), requesting structured
    output constrained to the capability proposal schema (D4). Provider selection stays
    configuration, not code.
  - `RuleBasedProvider` remains available; credentials come from environment only and
    never leak.
  - Unavailable / unauthenticated / rate-limited → `PROVIDER_UNAVAILABLE`, never a
    fabricated plan.
  - Write the opt-in live acceptance tests (L-A grounded answer, L-B verified mutation,
    L-C ordered multi-operation, L-D clarification); they skip cleanly AND loudly
    without credentials and assert structured outcomes, never prose. Executed as a gate
    in Task 16.
  - _Requirements: 3.1–3.10, 4.1_

- [ ] 10. Core MCP-backed mutation capability mappings
  - **Gated on D1.** Implement the mutation capabilities — transform (move/rotate/scale),
    absolute dimensions, material colour — as capability→tool mappings under the chosen
    option, with NO new bespoke Blender implementation where the upstream provides one.
  - Every mapping carries absolute desired-after targets, goes through the Task 6 guard,
    and is verified by reading authoritative state back.
  - Percentages, colour names, degrees and direction tokens never cross the MCP
    boundary; conversion happens once, in `packages/spatial`.
  - **Parity against the retained legacy implementation:** for each capability, execute
    the same operation through both paths and assert the resulting `scene_version`
    agrees. That evidence is what later permits retiring the duplicate.
  - If the chosen option is the hybrid, record precisely which capabilities remain
    platform-owned and why, so the boundary is reviewable rather than accidental.
  - _Requirements: 8.4, 8.5, 8.6, 8.9, 9.5, 9.6, 9.7, 12.1–12.7, 18.4, 18.5_

- [ ] 11. Multi-operation orchestration
  - Add `PlanCoordinator`: offer operation *n* only after *n−1* reaches terminal success,
    strictly in `operation_index` order.
  - Chain scene versions: op 0 requires the planned version; op *n* requires op *n−1*'s
    reported result version, persisted before op *n* is offered so a resumed retry
    re-derives the chain from the journal.
  - Sequential, non-atomic, resumable, fail-fast, with per-operation
    `applied`/`failed`/`not_attempted` status; partial failure never claims success.
  - Two mandatory chaining tests: op 0's intentional change does NOT reject op 1; an
    EXTERNAL change between them DOES.
  - _Requirements: 13.1–13.9_

- [ ] 12. File and reference ingestion foundation — scope decision
  - **Decide explicitly (D6): deliver a minimal foundation in Spec 002, or defer to
    Spec 003 and record the boundary.** Do not leave it implicit.
  - If delivered: uploads are stored as project-scoped artifacts, references are
    addressed by platform id (never by path), and any Tier B import capability receives
    a platform-derived destination — never a model-chosen one.
  - If deferred: record what Spec 003 must provide and assert nothing in Spec 002
    structurally prevents it (Requirement 16.3).
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 10.3_

- [ ] 13. Browser intelligent-agent UX
  - Extend `lib/api` and the pure session reducer with the new outcome kinds.
  - One user message → exactly one user entry and one Studio reply slot, even for a
    multi-operation request; extend the existing stable-message-id collapsing rather than
    adding a second mechanism.
  - `Answer` renders as a reply with no mutation progress; `Clarification` renders as an
    answerable question; partial failure says what was and was not applied;
    `SCENE_VERSION_MISMATCH` reads as "the project changed, nothing was modified".
  - Nothing rendered may contain a path, credential, job id, provider prompt, upstream
    tool name or MCP transport detail.
  - Accessibility maintained; `tsc --noEmit` clean and `next build` succeeding.
  - _Requirements: 17.1–17.9_

- [ ] 14. MCP-backed acceptance suite
  - Add the Spec 002 acceptance suite covering scenarios A–J (design §12) against the
    real pinned MCP server and real Blender, marked `mcp` and opt-in, assembled through
    the existing slice-stack builder.
  - The same suite must also run fully offline against `FakeBlenderMcpGateway` +
    `FakeLlmProvider`, so the default suite proves the platform's handling without any
    external process.
  - Failure cases at this tier: provider unavailable, malformed model output, denied
    capability, unavailable capability, unclassified tool, MCP server down, add-on
    version mismatch, normalisation failure, invented object, lock conflict,
    scene-version mismatch, mid-plan failure, preview failure — each with a structured
    outcome and an intact project.
  - _Requirements: 5.1–5.5, 9.5, 10.2, 10.6, 14.1–14.8, 18.1, 18.2_

- [ ] 15. Live real-provider acceptance
  - Run L-A…L-D against the configured real provider on the development environment with
    credentials supplied, and record provider, model and date.
  - Assert structured outcomes and safety boundaries only; never exact prose.
  - `FakeLlmProvider` alone does not satisfy this gate.
  - _Requirements: 3.9, 3.10_

- [ ] 16. Final audit and closeout
  - Re-run the whole Spec 001 regression surface: mandatory slice E2E unchanged in intent,
    all failure-case E2E tests, idempotency behaviour.
  - Audit the trust boundary against the real code: user message → SceneContextService →
    normalisation → ContextBuilder → provider → proposal → validation → policy →
    resolver → PlannedOperation → JobFactory → Job → guard → gateway → MCP → Blender →
    verification → browser. Confirm no model-authored value reaches Blender unvalidated
    and no upstream tool name reaches the model.
  - Re-verify the revised security invariants: nothing on the workstation externally
    reachable, loopback-only MCP, worker link outbound, protocol vocabulary closed, Tier
    C never offered, policy fails closed, telemetry off, jobs and artifacts
    project-scoped.
  - Re-verify single-conversion-site and schema parity; fresh-venv install and import;
    clean frontend type-check and build.
  - Re-run the upstream compatibility test and confirm the pinned inventory is unchanged.
  - Write `verification.md` mapping every Requirement 1–18 acceptance criterion to
    implementing modules and named tests, and record the migration state: which
    capabilities are MCP-backed, which remain platform-owned, and what retiring the
    duplicates requires.
  - _Requirements: 9.10, 11.1–11.8, 14.7, 15.6, 18.1–18.8_

---

## Definition of done

Spec 002 is complete when, and only when:

1. All 16 tasks are `[x]` and every Requirement 1–18 acceptance criterion is verified by
   an executable test recorded in `verification.md`.
2. Acceptance scenarios A–J pass against the real pinned MCP server and real Blender, and
   the same suite passes offline against the fake gateway and fake provider.
3. The default test suite passes with no API key, no network, no Blender and no MCP
   server; opt-in tiers skip cleanly.
4. **The live-provider acceptance gate has passed** on the development environment
   against the configured real provider, recorded in `verification.md`.
5. Every Spec 001 test suite is still green, including the mandatory slice E2E, the
   failure-case E2E tests and idempotency.
6. No model-authored value reaches Blender without schema validation, capability-policy
   approval, object resolution against an authoritative snapshot, and platform-side Job
   construction.
7. No upstream tool name appears in any prompt, proposal, plan, Job, API response or
   browser surface.
8. `McpToolPolicy` denies every unclassified tool, Tier C is never in the model's
   catalogue, and the compatibility test pins the upstream inventory and schemas.
9. No Blender or MCP service on the workstation is externally reachable: loopback-only
   binding, no exposure or tunnelling through the control plane, no browser access, and
   the worker link still outbound.
10. Upstream telemetry is disabled by default and verified, and no user content is sent
    in a third-party analytics parameter.
11. **No mutation executes against a scene it was not reasoned against**, and a retry
    after a crash inspects before deciding rather than re-invoking an external mutation.
12. A colour change is verified in the project and visible in the preview, which remains
    headless, GPU-free and deterministic.
13. The migration state is recorded: which capabilities are MCP-backed, which remain
    platform-owned, and what retiring the duplicated implementation requires.

---

## Task 1 — implementation notes

Two deliberate deviations, disclosed rather than silently absorbed:

1. **No `required_scene_version` was added to `move-object-plan` yet.** That schema is
   the MCP tool's input schema and its parser rebuilds plans field by field, so
   declaring an optional precondition field would produce a schema a caller can populate
   and a parser that silently drops it. `scene-version.schema.json` exists as the single
   canonical form; the field lands with the enforcement (now Task 6).
2. **`SCENE_VERSION_MISMATCH` is not yet mapped in the API's HTTP table or the browser
   message map.** Both fall back safely (unmapped → `INTERNAL`/500, never a 200), and
   nothing produces the code yet. It must become a conflict reason (409) when enforcement
   lands.

Two things outside the task text were changed, both disclosed:

3. **`studio_fixtures.scene_state_digest` now delegates to the canonical digest**, so
   there is one definition of "scene state digest". The value is now prefixed and uses
   the 1e-6 quantum, and `scene.name` no longer participates — the Blender fixture tests
   assert the scene name directly, which is stronger than folding it into a hash.
4. **A latent race was fixed** in
   `test_redelivering_the_same_job_over_the_socket_does_not_move_twice`: it waited with a
   helper that returns as soon as a collection is non-empty, then asserted a count of 2.
   It now waits with the existing count-aware helper.

Known gap, pre-existing: the shared TypeScript packages have no `tsconfig.json`, so
`node --test` type-strips without type-checking. New files are type-checked explicitly
against the repo's strict settings; three pre-existing cast errors remain in
`jobs.test.ts`.

## Task 2 — implementation notes

1. **No canonical schema was added for the resize factor or `(percent, direction)`.** The
   reviewed documents make the canonical resize mutation absolute dimensions in metres,
   so a factor never crosses a boundary and a schema for it would be a contract with no
   wire. `(percent, direction)` becomes canonical vocabulary in the capability proposal
   schema (now Task 7).
2. **`AngleUnit` / `AngleMeasurement` live in the shared type packages**, not in
   `packages/spatial`, matching where `LengthUnit` and `Measurement` already live.
3. **`packages/spatial/python` was added to pytest `pythonpath`**: the shared corpus
   loader was previously importable only by collection-order accident.

Decisions worth recording:

- `radians_to_degrees` returns a bare float, not an `AngleResult` whose field is named
  `radians` — reusing it to carry degrees would be the exact unit-mislabelling the module
  prevents.
- The colour palette is authored in canonical LINEAR literals with encoded provenance
  test-verified, because `pow` is not guaranteed bit-identical across libm
  implementations. Angle and resize parity are exact; transfer-function parity is a
  documented 1e-12 tolerance.

## Uncommitted work from the superseded Task 3

Before this refactor, work had already been written for the OLD Task 3 (a platform-owned
`inspect_scene` operation, its Blender script, `SceneAdapter` read methods, the worker
read path, the `inspect-scene-result` contract, and the richer `studio_scene` fixture).
It is present in the working tree and **has been left in place deliberately**:

- deleting it would be a destructive action taken on assumption, and
- design §13 wants exactly this code as the **read oracle** and fallback until MCP-backed
  parity is proven (Requirement 18.4).

New Task 5 validates MCP normalisation against it. Whether any of it is later retired is
the deliberate step described in Requirement 18.5. It is not counted as Task 3 progress:
Task 3 is now the integration spike above, and remains `[ ]`.

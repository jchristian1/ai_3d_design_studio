# Tasks — 002 Intelligent Scene Agent

Implementation plan for making the assistant scene-aware and genuinely intelligent, with
Blender ability provided by the **official Blender Lab MCP** behind a platform-owned
capability provider.

Spec 001 is the baseline: reuse `packages/contracts` as the source of truth,
`packages/spatial` as the only conversion site, the canonical Job lifecycle, the outbound
worker link, the durable journal, and the project lock.

The default test suite must stay **offline and deterministic**: no network, no API key, no
paid model calls, no Blender, no MCP server. Real-MCP and live-provider tests are opt-in;
the live-provider gate is required for completion (Task 16).

---

## Refactor note (this plan was rewritten)

Tasks 1 and 2 are complete and unchanged. Tasks 3 onward are organised around three
decisions:

> 1. The **official Blender Lab MCP** is the primary Blender backend.
> 2. We are **not** building a second MCP server.
> 3. **No model-authored code.** Mutation Python comes only from a closed catalogue of
>    reviewed, parameterised, platform-owned templates.

The backend sits behind `BlenderCapabilityProvider` / `OfficialBlenderLabBackend`
(design §4). Community Blender MCP implementations are capability references, possible
optional future providers, or a review-gated temporary fallback — **not integrated in
Task 3** (Requirement 9.12).

The verified upstream reality that shapes this plan (design §2.3): the official MCP exposes
inspection, documentation, screenshots, rendering and generic code execution — and **no
semantic mutation tools at all**. So mutation is Python by necessity, and Requirement 11
governs exactly whose Python it is.

**The superseded read work is preserved on a branch, not lost.** The platform-owned
`inspect_scene` implementation written for the *old* Task 3 lives on
`wip/spec002-custom-blender-oracle` at `fde4115` (design §15). It is the read oracle Task 5
validates normalisation against. It is not Task 3 progress.

---

- [x] 1. SceneSnapshot canonical contracts
  - Add canonical language-neutral schemas in `packages/contracts/schemas/`:
    `scene-snapshot`, `scene-object`, `scene-units`, `scene-version`, `euler-radians`,
    `scale3`, `material-summary`, `material-color`, `inspect-scene-payload`, and extend
    `job-type` with the read operation `inspect_scene`.
  - `additionalProperties: false` throughout, so no filesystem path, project-file location,
    hostname, worker token, or arbitrary internal is *representable*.
  - Mirror in Python and TypeScript, extend the shared corpus, and keep `SCHEMA_FILES`
    parity plus cross-language verdict parity green.
  - Define `scene_version` as SHA-256 over the **explicit versioned projection**
    `studio-scene-v1`: an allow-list, not a hash of the snapshot and not a deny-list.
    Exclude `captured_at`, `scene_version` itself and `project_id`; normalise object order
    by a documented total order; quantise floats to 1e-6 and serialise them as
    fixed-decimal strings; refuse non-finite values.
  - Add the projection-completeness guard so a future field must be classified as
    informational or planning-relevant.
  - Add `SCENE_VERSION_MISMATCH` to `error-code`.
  - _Requirements: 1.7, 1.8, 1.9, 2.1, 2.6, 2.7, 2.8, 2.9, 19.6_

- [x] 2. Canonical angle, scale and colour utilities
  - Add `angles` to `packages/spatial` (both languages): degrees → radians as the single
    conversion site, with radians canonical. Conversion is NOT normalisation — nothing
    reduces modulo 2π.
  - Add canonical `angle-unit` and `angle-measurement` schemas; the enum holds normalised
    wire values only, with human spellings accepted as tokens above the boundary.
  - Add `sizing`: normalised `(percent, direction)` → absolute factor, with the boundary
    decisions enforced (100% smaller refused, >100% refused, negative percent refused, no
    invented growth ceiling) and a semantic size factor validated as strictly positive —
    deliberately stricter than observed `SceneObject.scale`.
  - Add `colors`: canonical linear sRGB RGBA, the real sRGB transfer function as the ONE
    conversion site, and a deliberately tiny named palette authored in canonical linear
    literals with its encoded provenance test-verified.
  - Add the single-conversion-site source guards (AST in Python, comment/string-stripped
    scan in TypeScript), each proved to detect real violations and to ignore legitimate
    ones.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 19.6_

- [ ] 3. Official Blender MCP integration spike
  - **A small, bounded spike whose purpose is to answer ONE question with evidence:**

    > *Can our existing safe, durable architecture use the official Blender MCP as its
    > Blender backend without giving the LLM unrestricted execution?*

    **If the answer is NO: stop and report the blocker.** Do not implement a community-MCP
    fallback, do not switch upstreams, and do not widen the execution boundary to make the
    answer yes.
  - Exactly these sixteen steps, in order:
    1. **Identify and record the exact official build.** The MCP server artefact and the
       Blender add-on build, as immutable identities: source (canonical
       `projects.blender.org/lab/blender_mcp`, reconciled with the mirror commit
       `4309a39646e644261624bfcd2bca669b343b7621` recorded in design §2.1), release or
       `.mcpb` version, add-on version and `blender_version_min`, declared dependencies,
       and licence. Record where each artefact was obtained. **Do not `pip install
       blender-mcp`** — that PyPI name belongs to a different, community project
       (design §2.1).
    2. **Run it on the actual development Blender 5.2.** Install and enable the official
       add-on, start its socket server, launch the MCP server, and confirm server and
       add-on agree before any capability is used. Record the Blender build string.
    3. **Connect programmatically from the worker / client side.** An MCP client in our
       code over stdio using the official `mcp` SDK (our own pinned dependency) — not a
       chat client, not a GUI, not a manual session. No importing of upstream internals.
    4. **Enumerate and inspect the real MCP interface.** `tools/list` at runtime: every
       tool name, description and input schema, plus any prompts or resources the server
       advertises. Commit the inventory artefact and assert it matches design §2.2; a
       difference must FAIL, not be absorbed. Classify every tool A / B / C.
    5. **Inspect one simple real scene** through read-only tools only, and record the raw
       shapes returned.
    6. **Derive enough for `SceneSnapshot`.** Determine precisely which `SceneObject` and
       `SceneUnits` fields the official reads DO supply and which they do not (world
       position and dimensions in metres, rotation, scale, material summary, visibility,
       scene unit settings). Decide whether a platform read template is required (design
       D2) and measure the per-object read cost (design D3).
    7. **Perform exactly ONE bounded mutation:** *Move Cube +0.50 m on X.* One object, one
       axis, one operation. Nothing else in the scene is touched.
    8. **The LLM is not involved.** No provider is called anywhere in this task. The
       operation is constructed by hand from canonical values.
    9. **If code is required, it comes from a fixed platform template.** One template
       module, fixed literal source, one typed parameter record of validated canonical
       values, substituted through the single safely-encoded placeholder — no
       interpolation of the object name or any value into Python source (Requirement 11.4,
       11.5). Do not copy upstream tool-code text into this repository.
    10. **Read back and verify.** Re-inspect through the backend and assert the object's
        world X equals 0.50 m within tolerance. A returned `status: ok` is not evidence;
        the observed post-state is.
    11. **Operate on a temporary fixture only — never a user project.** A generated
        throwaway `.blend` in a temp location, created and removed by the test. Assert no
        other project file is opened, modified, or left behind, including numbered sibling
        files (design §2.6.3).
    12. **Prove a retry cannot apply the change twice.** Run the same operation twice under
        the already-applied rule and assert the object is at 0.50 m — not 1.00 m — and that
        the second run reports `already_applied` without invoking the mutation.
    13. **Prove arbitrary model-authored Python is impossible through this boundary.**
        Assert: no code-execution capability is present in any model-facing catalogue; there
        is no code path that renders or sends Python derived from provider output, browser
        input, or an MCP response; and an adversarial parameter value cannot change the
        rendered program's structure.
    14. **Prove connect → disconnect → reconnect.** Including that a dead MCP server or a
        stopped add-on surfaces `BLENDER_UNAVAILABLE` promptly rather than hanging, and
        that the loopback posture holds (add-on on `127.0.0.1`, non-loopback host rejected
        by our configuration, nothing exposed).
    15. **Record the limitations found.** Session model (interactive vs `*_for_cli`, design
        D1), whether the CLI path persists changes and whether it creates sibling files,
        GUI dependence of screenshots (D7), deferred-response constraints, timeouts,
        headless viability, read cost, and any field `SceneSnapshot` needs that the
        official reads cannot supply.
    16. **STOP.** Report findings and wait. Nothing in this task becomes a dependency of a
        production code path: it lands as a documented, runnable, opt-in integration test
        plus the inventory and limitations record.
  - _Requirements: 9.1, 9.2, 9.8, 9.9, 9.10, 9.11, 9.12, 10.1, 10.2, 10.3, 10.5, 11.1,
    11.2, 11.4, 11.5, 11.6, 11.7, 12.2, 12.3, 12.7, 13.1, 13.4, 13.9, 16.1, 16.2, 16.6,
    16.7, 16.9_

- [ ] 4. `OfficialBlenderLabBackend` + `BlenderCapabilityProvider`
  - Add `BlenderCapabilityProvider` (Protocol) with `capabilities()`,
    `invoke(capability, arguments)` and `health()`. The worker talks ONLY to this. Nothing
    above it may name an official tool — AST-tested across `services/agent`,
    `services/api`, `apps/web` and the contracts.
  - Define `CapabilityArguments` as typed canonical records. No field may carry a path, a
    URL, a host, a tool name, or code — assert this structurally.
  - Add `OfficialBlenderLabBackend`: MCP over stdio to the pinned official server via the
    official client SDK, owning process lifecycle, timeouts, reconnect, tool discovery,
    startup agreement check, and identity translation (`studio_object_id` ↔ Blender name)
    as the single translation site. It must not import upstream internals.
  - Add `FakeBlenderBackend` replaying canned backend responses, so every layer above is
    testable with no Blender and no MCP server. This is the load-bearing offline seam.
  - Add the capability registry: platform capability → native official tool, ordered tool
    sequence, or catalogue template. An unmapped capability is UNAVAILABLE and yields a
    structured capability-named failure — never a substitution.
  - Add the tool policy with Tiers A/B/C and **fail-closed denial of anything
    unclassified**, evaluated on our side before any MCP call, with the model's permitted
    catalogue derived from it. Tier B destinations are platform-derived from `project_id`.
  - Assert in a test that no design decision treats the upstream weak sandbox as
    authorisation.
  - Add the compatibility test pinning the tool inventory and schemas, and the configuration
    guard (non-loopback host rejected; telemetry/off-machine transmission disabled;
    developer-only tooling absent from the production-safe configuration).
  - _Requirements: 9.3, 9.4, 9.5, 9.6, 9.7, 9.10, 9.11, 10.1–10.8, 12.2, 12.3, 12.7, 12.8,
    12.9, 15.5, 16.1, 16.5, 16.7, 17.3, 17.4_

- [ ] 5. SceneSnapshot through the official backend
  - Add `capability/normalize.py`: raw backend read output → validated canonical
    `SceneSnapshot`, then `compute_scene_version` (the Task 1 digest, unchanged — no second
    digest).
  - Treat raw backend output as UNTRUSTED: validate shape and types, reject unexpected or
    non-finite values, never default a missing field into a plausible one. Absent stays
    absent, so "no material" and "black" remain distinguishable. A backend value never
    becomes code, a structure-altering parameter, or a permission decision.
  - Compose read capabilities where one is insufficient (`get_objects_summary` +
    `get_object_detail_summary` per object, plus the read template if Task 3 step 6 requires
    one), and record the measured cost.
  - Assert units at the boundary and convert through `packages/spatial` if the backend
    disagrees.
  - Assert that raw backend output never reaches `AgentProvider` and that no upstream field
    name appears in provider context.
  - **Validate against the retained read oracle** (`wip/spec002-custom-blender-oracle`,
    `fde4115`): for the same project, the oracle path and the official-backend path must
    produce the SAME `scene_version`. That equality is the parity evidence Requirements
    19.4/19.5 depend on.
  - Wire `SceneContextService` (cache by `project_id` + `scene_version`, invalidate on
    successful mutation and on `SCENE_VERSION_MISMATCH`, fail closed on read failure) and
    dispatch the read as a job that HOLDS the project lock (Requirement 1.12) while writing
    no journal record, no recovery point, no save and no preview.
  - _Requirements: 1.1–1.6, 1.10–1.16, 2.5, 8.9, 15.7_

- [ ] 6. Durable `MutationGuard`
  - Implement the documented order: acquire lock → inspect authoritative state → verify
    `scene_version` → **already-applied?** → verify `expected_before` → persist intent +
    required version + expected/desired + recovery point → invoke the mutation capability →
    save deliberately → inspect again → verify desired-after → compute resulting version →
    journal completion.
  - Already-applied is evaluated BEFORE the version check, with a named regression test:
    reversing them makes a verbatim retry fail as stale and breaks Spec 001 idempotency.
  - Assume the backend call is NOT retry-idempotent. A retry after a crash inspects first
    and never blindly re-invokes.
  - Keep mutation identity `project_id + request_id + operation_index`.
  - Keep the guard free of Blender geometry logic and of template source — an AST test
    asserts it contains no bpy, no geometry maths and no Python-as-data.
  - The platform owns the save: a change that exists only in an unsaved session is not
    success. Verification failure preserves the recovery point and never reports success.
  - Assert project isolation: every mutation proves it is acting on the job's `project_id`.
  - _Requirements: 2.1–2.5, 13.1–13.9_

- [ ] 7. ContextBuilder + agent outcome and proposal model
  - Replace the boolean `AgentResult` with the discriminated
    `AgentOutcome = Answer | Clarification | PlanProposal | AgentError`; migrate
    `RuleBasedProvider` deliberately.
  - Add the untrusted `ProposedOperation` model over the **platform capability** catalogue,
    schema-validated before any resolution, with bounded size and operation count. Unknown
    capabilities, unknown fields, malformed and oversized output are all refused with zero
    Jobs.
  - The proposal schema declares **semantic operations only**: no code field, no expression,
    no operator name, no attribute path, no path, no URL, no tool name — structurally
    impossible, not merely rejected.
  - Add `ContextBuilder` as the only place that decides what the model sees: system rules,
    permitted CAPABILITIES (from the policy), trusted identity, SceneSnapshot, browser
    selection, pending clarification, bounded recent turns, current message.
  - Assert absence: no credential, no filesystem path, no worker token, no MCP transport
    detail, no official tool name, no template source, no other project's data. Route code
    never builds prompts.
  - Add `FakeLlmProvider` scripting exact proposals, so the whole pipeline is testable
    offline.
  - `ProviderMetadata` stays name/version/count; no chain-of-thought is stored or
    transmitted.
  - _Requirements: 3.7, 4.1–4.8, 5.1–5.5, 11.1, 11.2, 15.1–15.6_

- [ ] 8. ObjectResolver + clarification lifecycle
  - Add `ObjectResolver` with the documented ordered rules against the authoritative
    snapshot; a model-invented identifier is rejected; no fuzzy or semantic matching.
  - Resolved operations address stable ids and carry canonical values only, plus the
    `scene_version` they were resolved against. An identifier is never a free-form string
    from model output (Requirement 7.9).
  - Add `ClarificationStore`: session-scoped, bounded, expiring; zero Jobs; a clarification
    is a question, not an error; answering re-grounds against a fresh snapshot if the scene
    moved.
  - Prove with `FakeLlmProvider`: two chairs + "Move the chair right." → clarification, zero
    Jobs, project unchanged; then "the second one" → exactly one Job.
  - _Requirements: 6.1–6.8, 7.1–7.9_

- [ ] 9. Real LLM provider
  - Implement one real provider behind the existing registry (design D4), requesting
    structured output constrained to the capability proposal schema (design D5). Provider
    selection stays configuration, not code.
  - `RuleBasedProvider` remains available; credentials come from environment only and never
    leak.
  - Unavailable / unauthenticated / rate-limited → `PROVIDER_UNAVAILABLE`, never a
    fabricated plan.
  - Write the opt-in live acceptance tests (L-A grounded answer, L-B verified mutation, L-C
    ordered multi-operation, L-D clarification); they skip cleanly AND loudly without
    credentials and assert structured outcomes, never prose. Executed as a gate in Task 15.
  - _Requirements: 3.1–3.10, 4.1_

- [ ] 10. Guarded core modelling capabilities
  - Implement the **closed template catalogue** (design §6) for the core semantic
    capabilities: `move_object`, `rotate_object`, `set_object_dimensions`,
    `set_material_color`, `create_object`, `delete_object`, `duplicate_object`,
    `save_project` — plus `render` mapped to the official native render tool with a
    platform-derived destination.
  - Each template: fixed literal source, typed validated parameter record, one safely
    encoded substitution site, narrow to one operation, no eval/exec of model text, no
    shell, no subprocess, no arbitrary import, no arbitrary path, no arbitrary URL, no
    package install.
  - **The catalogue is closed.** An unknown semantic capability returns a structured DENY.
  - Tests that must exist: exact rendered-source assertions per template; the adversarial
    parameter test proving rendered program structure is invariant (scenario K); the AST
    guard proving no dynamic source construction and no sender of Python outside the
    catalogue; the proof that no code path renders a template from model, browser or MCP
    output.
  - Every mapping carries absolute desired-after targets, goes through the Task 6 guard, and
    is verified by reading authoritative state back.
  - Percentages, colour names, degrees and direction tokens never cross the boundary;
    conversion happens once, in `packages/spatial`.
  - **Parity against the retained oracle:** for each capability available on the oracle
    branch, execute the same operation both ways and assert the resulting `scene_version`
    agrees. That evidence is what later permits retiring the duplicate.
  - **Prove the replacement path (Requirement 17.5):** execute one capability through both a
    template and a simulated native semantic tool and assert identical canonical results,
    with no change to any module above `BlenderCapabilityProvider`.
  - _Requirements: 8.4, 8.5, 8.6, 8.9, 9.5, 9.6, 9.7, 11.1–11.12, 13.1–13.9, 17.4, 17.5,
    19.4, 19.5_

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
  - _Requirements: 14.1–14.9_

- [ ] 12. Multimodal file and reference foundation — scope decision
  - **Decide explicitly (design D6): deliver a minimal foundation in Spec 002, or defer to
    Spec 003 and record the boundary.** Do not leave it implicit.
  - Record the long-term target the decision must not foreclose: floor plans, architectural
    drawings, photographs, elevations, site and topography references, furniture and
    material references, feeding a multimodal agent that builds interiors, houses,
    buildings, furniture layouts and architectural models — composed **above**
    `BlenderCapabilityProvider`, never as another MCP.
  - If delivered: uploads are stored as project-scoped artifacts, references are addressed by
    platform id (never by path), and any import destination is platform-derived.
  - If deferred: record what Spec 003 must provide and assert nothing in Spec 002
    structurally prevents it (Requirement 17.1–17.2).
  - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.6, 10.3_

- [ ] 13. Browser intelligent-agent UX
  - Extend `lib/api` and the pure session reducer with the new outcome kinds.
  - One user message → exactly one user entry and one Studio reply slot, even for a
    multi-operation request; extend the existing stable-message-id collapsing rather than
    adding a second mechanism.
  - `Answer` renders as a reply with no mutation progress; `Clarification` renders as an
    answerable question; partial failure says what was and was not applied;
    `SCENE_VERSION_MISMATCH` reads as "the project changed, nothing was modified".
  - Nothing rendered may contain a path, credential, job id, provider prompt, official tool
    name, template source or MCP transport detail.
  - Accessibility maintained; `tsc --noEmit` clean and `next build` succeeding.
  - _Requirements: 18.1–18.9_

- [ ] 14. Official-MCP acceptance suite
  - Add the Spec 002 acceptance suite covering scenarios A–K (design §16) against the real
    pinned official MCP server and real Blender 5.2, marked `mcp` and opt-in, assembled
    through the existing slice-stack builder.
  - The same suite must also run fully offline against `FakeBlenderBackend` +
    `FakeLlmProvider`, so the default suite proves the platform's handling without any
    external process.
  - Failure cases at this tier: provider unavailable, malformed model output, denied
    capability, unavailable capability, unclassified tool, MCP server down, add-on
    disagreement, worker disconnected, normalisation failure, invented object, invalid
    units, duplicate job, interrupted operation, lock conflict, scene-version mismatch,
    mid-plan failure, render failure, preview failure — each with a structured outcome and
    an intact project.
  - Verify scene state after every mutating scenario. No scenario may pass on the absence of
    an exception.
  - _Requirements: 5.1–5.5, 9.5, 10.2, 10.6, 11.10, 15.1–15.9, 19.1, 19.2_

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
    normalisation → ContextBuilder → provider → proposal → validation → policy → resolver →
    PlannedOperation → JobFactory → Job → MutationGuard → BlenderCapabilityProvider →
    OfficialBlenderLabBackend → official MCP → Blender → verification → browser. Confirm no
    model-authored value reaches Blender unvalidated, no model-authored code exists anywhere,
    and no official tool name reaches the model.
  - Re-verify the revised security invariants: no Blender or MCP execution interface is
    externally reachable, loopback-only backend host, worker link outbound, protocol
    vocabulary closed, code execution never offered to a model, policy fails closed, template
    catalogue closed, telemetry off, developer-only tooling absent, jobs and artifacts
    project-scoped.
  - Re-verify single-conversion-site and schema parity; fresh-venv install and import; clean
    frontend type-check and build.
  - Re-run the upstream compatibility test and confirm the pin is unchanged.
  - Write `verification.md` mapping every Requirement 1–19 acceptance criterion to
    implementing modules and named tests. **Record the verified Blender MCP version**, the
    verified add-on version, the verified Blender version and the date (Requirement 16.6),
    and record the migration state: which capabilities are native-tool-backed, which are
    template-backed, which remain platform-owned, and what retiring the duplicates requires.
  - _Requirements: 9.10, 12.1–12.9, 15.8, 16.3, 16.4, 16.6, 19.1–19.8_

---

## Definition of done

Spec 002 is complete when, and only when:

1. All 16 tasks are `[x]` and every Requirement 1–19 acceptance criterion is verified by an
   executable test recorded in `verification.md`.
2. Acceptance scenarios A–K pass against the real pinned official MCP server and real
   Blender, and the same suite passes offline against the fake backend and fake provider.
3. The default test suite passes with no API key, no network, no Blender and no MCP server;
   opt-in tiers skip cleanly.
4. **The live-provider acceptance gate has passed** on the development environment against
   the configured real provider, recorded in `verification.md`.
5. Every Spec 001 test suite is still green, including the mandatory slice E2E, the
   failure-case E2E tests and idempotency.
6. No model-authored value reaches Blender without schema validation, capability-policy
   approval, object resolution against an authoritative snapshot, and platform-side Job
   construction.
7. **No model-authored code and no user-authored arbitrary Python exists in any path.** Every
   character of Python sent to Blender came from the closed, reviewed template catalogue,
   parameterised only with validated canonical values.
8. No official tool name, and no template source, appears in any prompt, proposal, plan, Job,
   API response or browser surface.
9. The policy denies every unclassified tool, no code-execution capability is in the model's
   catalogue, and the compatibility test pins the upstream inventory and schemas.
10. No Blender or MCP execution interface is externally reachable: loopback-only binding, no
    exposure, tunnelling or proxying through the control plane, no browser access, and the
    worker link still outbound.
11. Upstream telemetry or off-machine transmission is disabled by default and verified, and
    no user content is sent in a third-party analytics parameter.
12. **No mutation executes against a scene it was not reasoned against**, and a retry after a
    crash inspects before deciding rather than re-invoking a backend mutation.
13. Every mutating operation is verified by reading resulting scene state back; nothing passes
    on the absence of an exception.
14. A colour change is verified in the project and visible in the preview, which remains
    headless, GPU-free and deterministic.
15. The pinned official MCP version, add-on version and Blender version are recorded in
    `verification.md`, and the upgrade procedure (discover → compatibility → safety →
    acceptance → pin) is documented and exercised at least once.
16. The migration state is recorded: which capabilities are native-tool-backed, which are
    template-backed, which remain platform-owned, and what retiring the duplicated
    implementation requires.

---

## Task 1 — implementation notes

Two deliberate deviations, disclosed rather than silently absorbed:

1. **No `required_scene_version` was added to `move-object-plan` yet.** That schema is the
   legacy MCP tool's input schema and its parser rebuilds plans field by field, so declaring
   an optional precondition field would produce a schema a caller can populate and a parser
   that silently drops it. `scene-version.schema.json` exists as the single canonical form;
   the field lands with the enforcement (now Task 6).
2. **`SCENE_VERSION_MISMATCH` is not yet mapped in the API's HTTP table or the browser
   message map.** Both fall back safely (unmapped → `INTERNAL`/500, never a 200), and nothing
   produces the code yet. It must become a conflict reason (409) when enforcement lands.

Two things outside the task text were changed, both disclosed:

3. **`studio_fixtures.scene_state_digest` now delegates to the canonical digest**, so there is
   one definition of "scene state digest". The value is now prefixed and uses the 1e-6
   quantum, and `scene.name` no longer participates — the Blender fixture tests assert the
   scene name directly, which is stronger than folding it into a hash.
4. **A latent race was fixed** in
   `test_redelivering_the_same_job_over_the_socket_does_not_move_twice`: it waited with a
   helper that returns as soon as a collection is non-empty, then asserted a count of 2. It
   now waits with the existing count-aware helper.

Known gap, pre-existing: the shared TypeScript packages have no `tsconfig.json`, so
`node --test` type-strips without type-checking. New files are type-checked explicitly
against the repo's strict settings; three pre-existing cast errors remain in `jobs.test.ts`.

## Task 2 — implementation notes

1. **No canonical schema was added for the resize factor or `(percent, direction)`.** The
   reviewed documents make the canonical resize mutation absolute dimensions in metres, so a
   factor never crosses a boundary and a schema for it would be a contract with no wire.
   `(percent, direction)` becomes canonical vocabulary in the capability proposal schema
   (now Task 7).
2. **`AngleUnit` / `AngleMeasurement` live in the shared type packages**, not in
   `packages/spatial`, matching where `LengthUnit` and `Measurement` already live.
3. **`packages/spatial/python` was added to pytest `pythonpath`**: the shared corpus loader
   was previously importable only by collection-order accident.

Decisions worth recording:

- `radians_to_degrees` returns a bare float, not an `AngleResult` whose field is named
  `radians` — reusing it to carry degrees would be the exact unit-mislabelling the module
  prevents.
- The colour palette is authored in canonical LINEAR literals with encoded provenance
  test-verified, because `pow` is not guaranteed bit-identical across libm implementations.
  Angle and resize parity are exact; transfer-function parity is a documented 1e-12
  tolerance.

## The superseded custom-Blender read work

Work written for the **old** Task 3 — a platform-owned `inspect_scene` MCP tool, its Blender
script, `SceneAdapter` read methods, the worker read path, the `inspect-scene-result`
contract, and the richer four-object `studio_scene` fixture, with 52 passing Blender tests —
is **preserved on branch `wip/spec002-custom-blender-oracle` at commit `fde4115`**. `main`
does not carry it.

It is retained deliberately (design §15): Requirement 19.4 wants an incremental migration
with a test oracle rather than a destructive rewrite, and Task 5 validates official-backend
normalisation by requiring the same `scene_version` from both paths. Whether any of it is
later retired is the deliberate step described in Requirement 19.5.

It is **not** Task 3 progress. Task 3 is the official-MCP spike above, and remains `[ ]`.

# Tasks — 001 Core Vertical Slice

Implementation plan for the first end-to-end slice: "Move Cube 50 cm to the right."
Each task is incremental, references requirements, and ends in verifiable behavior.

- [x] 1. Scaffold monorepo structure and canonical contract layer
  - Create `apps/web`, `services/{api,agent,blender-worker,blender-mcp,preview}`, `packages/{contracts,types,validation}`, `tests/{integration,blender,agent,e2e}` per `structure.md`.
  - Define CANONICAL language-neutral schemas in `packages/contracts/schemas/`: `vec3`, `job`, `chat-request`, `chat-response`, `error-response`, `error-code`.
  - Provide TypeScript and Python representations that conform to the canonical schemas (neither language is the source of truth).
  - Add validation rules (presence of `project_id`, non-blank identifiers, finite meters, no unknown properties) in `packages/validation`.
  - Enforce conformance with a shared language-neutral case corpus + per-language conformance tests + a cross-language verdict-parity test.
  - Reserve `services/agent/providers/` so `AstraProvider`/`CodexProvider` can be added later without changing API, jobs, MCP, worker, or Blender code.
  - _Requirements: 2.1, 2.3_

- [x] 2. Unit conversion and interpretation utilities
  - Add canonical schemas for the new cross-boundary vocabulary: `length-unit`, `measurement`, `axis`, `direction`, `axis-direction`.
  - Implement `packages/spatial` (TypeScript + Python): cm→m / m→m conversion and world-space direction→axis+sign mapping.
  - `right` = Blender world-space +X. Camera-relative interpretation explicitly deferred.
  - No Blender calls and no natural-language/AI reasoning in these utilities; they stay pure and deterministic.
  - Reject invalid/non-finite measurements consistently with `INVALID_UNITS`; unknown directions with `VALIDATION_ERROR`.
  - Signed measurement semantics: generic conversion stays signed (`-25 cm → -0.25 m`), but `directionDeltaMeters` requires a non-negative distance magnitude so direction is never encoded twice (`left` + `-25 cm` → `VALIDATION_ERROR`). Zero is a valid magnitude.
  - Unit tests: `"50 cm" → 0.50`, `"100 cm" → 1.00`, `"-25 cm" → -0.25`; `right → axis x, sign +1`; plus shared-corpus and cross-language parity tests.
  - _Requirements: 3.2, 3.3_

- [x] 3. Structured job layer
  - Add canonical schemas: `job-type`, `object-ref`, `move-object-payload`, `job-claim`, and a rewritten `job` schema.
  - `Job` is a typed discriminated union on `job_type`, never an untyped dictionary: each type binds to exactly one payload schema.
  - Carry `job_id`, `job_type`, `project_id`, `session_id`, `user_id`, `payload`, `status`, `created_at`, `idempotency_key`, optional `claim`/`error`/`result`.
  - Lifecycle: `queued → claimed → running → succeeded | failed`, with schema-enforced invariants (failed requires an error; claimed/running require a claim; queued must not be owned; only succeeded may carry a result).
  - Project isolation: a job is unrepresentable without a non-blank `project_id`.
  - Payload carries fully resolved canonical meters only — no unit strings, no direction tokens, no natural language.
  - Mutation identity is derived from the ORIGINATING REQUEST — `(project_id, origin.request_id, origin.operation_index)` — not from payload contents, so a retry executes once while a repeated intentional command executes again. `request_id` added to `ChatRequest`.
  - Canonical content hash retained as `content_fingerprint` for diagnostics only; explicitly not the mutation identity.
  - `job_id` remains the job record identity and the execution identity a worker deduplicates queue deliveries on (`classifyDelivery`).
  - Persistence/queue boundary defined as `JobStore`/`JobClaimer` interfaces only. No Redis, no database, no worker loop.
  - _Requirements: 2.2, 2.3_

- [x] 4. MCP `move_object` tool
  - Layered as MCP handler → `move_object` service → `SceneAdapter` → bpy, so domain behaviour is testable with an in-memory fake and only a few tests need real Blender.
  - Retry-safe absolute execution: canonical `MoveObjectPlan` carries `expected_before_meters`, `delta_meters`, `desired_after_meters`. Three paths — already-applied (no mutation), apply (absolute write + read-back verification), conflict (`PRECONDITION_MISMATCH`, no mutation).
  - The Job keeps storing relative `delta_meters` unchanged; `plan_from_delta` derives the absolute endpoints.
  - One documented magnitude-aware position tolerance (`tolerance.py`) used for all three comparisons, calibrated against measured Blender float32 round-trip error.
  - Structured `MoveObjectResult` with `applied` / `already_applied` / `verified` / `error`; schema conditionals forbid a failure claiming success or an unverified apply.
  - Added error codes `PRECONDITION_MISMATCH`, `MUTATION_FAILED`, `OBJECT_NOT_MOVABLE`.
  - No `execute_python`, no network listener, no MCP SDK dependency; bpy isolated to one module (all AST-verified).
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 5. Seed test Blender project
  - Machine-readable `seed_project.spec.json` is the source of truth; `generate_seed_project.py` builds the scene from it inside Blender (factory startup, empty scene, explicit meter units).
  - Scene contains exactly one `Cube` at world origin with stable id `obj_cube001` in the adapter's `studio_object_id` custom property, resolvable by name and by object_id.
  - Generated `.blend` lives in git-ignored `tests/fixtures/blender/build/`; the generator and spec are committed instead of the binary.
  - `working_copy()` hands tests an isolated temp copy so the canonical fixture is never mutated; verified by SHA-256 before/after.
  - Determinism is asserted on the spec-relevant scene state (`scene_state_digest`), not on the non-reproducible `.blend` binary.
  - `python3 -m studio_fixtures.regenerate` generates / verifies / inspects; Blender located via the Task 4 runtime helper.
  - _Requirements: 8.1_

- [x] 6. Blender worker execution workflow
  - `WorkerExecutor` orchestrates: validate → resolve project → lock → load record → plan (once) → recovery copy → execute Task 4 op → verify durability from the saved file → complete → release.
  - Retry safety: the MoveObjectPlan is persisted BEFORE mutation and reused verbatim on retry; the worker never recomputes `expected_before` once a plan exists.
  - Internal execution phases are separate from the public Job lifecycle.
  - `WorkerExecutionStore` + local atomic filesystem journal (temp → fsync → `os.replace` → dir fsync); corrupt records refuse execution rather than replanning.
  - `ProjectLockProvider` + `flock` implementation (per project, Linux code confined to `locks.py`); `ProjectLocator` maps `project_id` to a trusted path so Jobs can never carry filesystem paths.
  - Recovery snapshot before every mutation, preserved on failure, never overwritten across attempts.
  - `BlenderOperationExecutor` boundary with a fake (fast tests, real Task 4 logic) and a headless-subprocess implementation using the Task 4 runtime resolver.
  - Crash windows A–D designed and tested; success requires the coordinates to be durable in the saved `.blend`, verified by a fresh Blender process.
  - No Redis, no PostgreSQL, no queue consumption, no agent, no web.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [x] 7. AgentProvider abstraction and deterministic RuleBasedProvider
  - `AgentProvider` Protocol + `AgentContext` (trusted identity) + provider-neutral `AgentPlan`/`AgentResult` in `services/agent/studio_agent`.
  - `RuleBasedProvider` interprets the narrow documented grammar and delegates ALL conversion to `packages/spatial` (no duplicated unit or axis math).
  - `AstraProvider`/`CodexProvider` boundaries exist and fail with `PROVIDER_UNAVAILABLE`; they never fabricate a plan.
  - `JobFactory` maps AgentPlan → canonical Job via the Task 3 builder, preserving `request_id`/`operation_index` idempotency without reimplementing hashing.
  - AgentPlan carries no identity and no unresolved language; `operations` is ordered so one request can represent multiple operations later.
  - Added error codes `UNSUPPORTED_INSTRUCTION` and `PROVIDER_UNAVAILABLE`.
  - `AgentPlan` kept as an internal Python boundary (no canonical schema) with the rationale documented.
  - Layering enforced by AST tests: no bpy, worker, MCP, subprocess or filesystem access anywhere in the agent package.
  - _Requirements: 3.1, 3.4, 3.5_

- [x] 8. Worker ↔ control-plane secure connection
  - Versioned worker protocol as a canonical schema (`worker-message`, `worker-capabilities`): closed message enum, `additionalProperties:false`, no path/shell/python field representable.
  - `WorkerTransport` abstraction with `InMemoryTransport` (fast tests) and outbound `WebSocketWorkerTransport`; nothing binds or listens on the workstation.
  - `WorkerLinkClient`: connection state machine (separate from execution phases), constant-time token auth, registration, heartbeats, bounded backoff reconnect, journal-driven reconciliation.
  - `WorkerConnectionManager` (framework-independent) + loopback `LocalControlPlaneServer`; FastAPI route binding deferred to the API task.
  - Control plane PUSHES offers to idle connected workers via a live connection registry. Regression test pins the invariant after a deadlock was found and fixed.
  - Dropped result channel never re-executes: the mutation is durable, the journal records non-delivery, reconnect resends as `duplicate`.
  - `websockets==15.0.1` declared in `pyproject.toml`, installed into a project-local `.venv`.
  - _Requirements: 7.1, 7.2, 7.3_

- [x] 9. API chat endpoint and job orchestration
  - FastAPI application FACTORY (`create_app(settings, dependencies)`) with an explicit dependency container on `app.state`; no module-level `app` and no import-time singleton, so configuration is validated per application and tests inject fakes.
  - Routes: `GET /health`, `GET /api/workers`, `POST /api/chat`, `POST /api/projects/{project_id}/chat` (spec-named form; path and body `project_id` must agree), `GET /api/projects/{project_id}/jobs/{job_id}`, `WS /ws/workers`. `/api` is the versionable prefix.
  - `ChatService.submit_chat(request, identity)` owns the workflow: authorize project → build AgentContext → `provider.interpret()` → `JobFactory` → canonical Job → record → select worker → offer. Routes only parse HTTP and render; no concrete provider is named in `routes/` or `chat_service.py` (AST-tested).
  - Trusted identity: `user_id` comes from an injected `IdentityResolver`, never the body. The canonical ChatRequest has no `user_id` and the HTTP model mirrors `additionalProperties:false` with `extra="forbid"`, so sending one is a 422. `DevelopmentIdentityResolver` fails closed outside `local`.
  - Control-plane project registry holds LOGICAL ids only; no filesystem path is known, stored, or accepted. Path-shaped `project_id` rejected before use; unknown project → 404, never guessed.
  - Project isolation of retrieval: job records are keyed by `(project_id, job_id)` and `JobRecordStore.get` requires `project_id` as a leading argument, so there is NO unscoped lookup. The route authorizes the project first, then scopes the lookup; a job in another project returns a byte-identical 404 to one that never existed, so cross-project existence cannot be probed. `status_url` is project-scoped.
  - Asynchronous submission model: `POST /api/chat` returns `202` with job identity + `status_url`; `GET /api/projects/{project_id}/jobs/{job_id}` reports the canonical Task 3 lifecycle (`queued → claimed → running → succeeded|failed`) and embeds the canonical `ChatResponse` once terminal. `accepted` is deliberately NOT a canonical job state.
  - `WorkerGateway` adds a transport-neutral live-connection registry over the Task 8 manager; the FastAPI `/ws/workers` binding uses one writer task independent of its receive loop, preserving the Task 8 anti-deadlock invariant (regression-tested at unit and integration tier). No protocol logic reimplemented.
  - In-memory `JobRecordStore` behind a Protocol honouring Task 3's atomic insert-or-return-existing keyed by `(project_id, idempotency_key)`. API state loss cannot cause re-execution: mutation identity is derived, and a `job_result` for an unknown job is ADOPTED (the worker journal is authoritative), never turned into new work.
  - Deliberate HTTP mapping via an API-level failure reason separate from the canonical error code (422/404/409/503/401/500), one structured error body everywhere, validation described by field not value, and no stack trace, path, or token in any response.
  - CORS middleware installed only when origins are configured explicitly; `*` refused outside `local`. All configuration centralized in `settings.py`; `fastapi`/`uvicorn` pinned in `pyproject.toml`.
  - Tests: 124 fast (`services/api/tests`), 19 integration with real HTTP + real WebSockets (`tests/integration`), 5 opt-in real-Blender E2E (`tests/e2e`). E2E proves `0.00 → 0.50`, retry of the same `request_id` stays `0.50`, a new `request_id` reaches `1.00`, and a stale retry still leaves `1.00`.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 10. Preview generation
  - New canonical schemas `artifact-type` (closed enum, `preview_image` only) and `preview-artifact`, mirrored in Python (`studio_types.PreviewArtifact`) and TypeScript, with corpus cases so cross-language verdict parity covers them.
  - `PreviewArtifact` deliberately carries NO path and NO url: `(project_id, artifact_id)` is the whole address, and the HTTP layer projects the logical URL. A worker must not know the control plane's route shape.
  - `services/preview` (`studio_preview`): `PreviewGenerator` Protocol returning bytes, `BlenderPreviewGenerator` (headless subprocess, no bpy), `blender_scripts/render_preview.py` (the only bpy code), `FakePreviewGenerator`, `ArtifactStore` Protocol + `LocalArtifactStore`.
  - Artifact identity is DERIVED from `(project_id, job_id, artifact_type)`, which supplies retry reuse and version history with no counters: a retry resolves to the same artifact, a new `request_id` produces a new one, and an earlier preview is never overwritten.
  - Camera: the render script adds a temporary camera in memory and NEVER saves the `.blend` (asserted by SHA-256 before/after and by the saved object list). Fixed computed three-quarter framing at (7, -7, 5) looking at the origin, so +X movement is visibly horizontal.
  - Render settings: `BLENDER_WORKBENCH`, fixed resolution, fixed flat background, fixed AA. No sampling, so output is byte-reproducible; no GPU required, so headless correctness never depends on RTX.
  - SECURITY FIX found during this task: Blender stamps the absolute `.blend` path into PNG `tEXt` metadata (`File\0/abs/path/project.blend`), and that image is served to the browser. All stamp flags are now disabled, which removed the leak AND made renders deterministic. Regression tests assert the served PNG contains no path, hostname, or text chunk.
  - Worker lifecycle extended: `mutate → verify → save → generate preview → record preview → complete`, with a new `preview_generated` phase. Preview generation is NON-FATAL (option B): it never raises and never touches `job_status`, so a saved change is never reported as failed. `preview_error` is recorded separately from `error`.
  - Worker protocol bumped to v2 with `SUPPORTED_PROTOCOL_VERSIONS = (1, 2)`: optional `preview`/`preview_error` on `job_result`, forbidden on other message types and on a failed result. Documented why an additive optional field is still breaking under `additionalProperties: false`, and why the on-disk journal record version was NOT bumped.
  - `GET /api/projects/{project_id}/artifacts/{artifact_id}` serves the PNG with the correct `Content-Type`, an ETag from the checksum, and immutable private caching. Project-scoped like Task 9 jobs: another project's artifact returns a byte-identical 404, and there is no unscoped route and no listing route.
  - Store safety: strict `^(preview)_[a-z0-9]{8,64}$` identifier pattern, store-derived filenames, closed media-type→extension map, containment re-checked after symlink resolution. Tests plant a `.blend`, a journal record, a recovery copy and an `.env` holding a token inside the artifact directory and prove none can be fetched.
  - Job status exposes preview metadata plus the logical URL and never a local path; bytes are never base64-inlined because status is polled.
  - Tests: 130 fast (50 artifact store, 29 worker preview, 51 API artifact routes), 11 real-Blender rendering, 8 HTTP→agent→worker→Blender→PNG E2E. Verified 0.00→0.50 (artifact A) →1.00 (artifact B, A still retrievable, images differ), and a retry reusing A with no new mutation and no new artifact.
  - _Requirements: 6.1, 6.2_

- [ ] 11. Web app chat + preview UI
  - Minimal Next.js/React chat input posting `ChatRequest` with `project_id`/`session_id`.
  - Show pending state, then success + updated preview, or a readable error.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 6.3_

- [ ] 12. Mandatory end-to-end test
  - Open test project (Cube X=0) → send "Move Cube 50 cm to the right".
  - Assert Cube X = 0.50 (tolerance), preview updated, response status = success.
  - Add failure-case E2E: Blender unavailable, lock conflict, invalid object ⇒ structured error, no corruption.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [ ] 13. Wire full path and verify slice
  - Connect all boundaries; run unit + MCP + worker + integration + E2E suites green.
  - Confirm meters canonical, AgentProvider-only access, no public Blender exposure.
  - _Requirements: 1.3, 3.4, 5.4, 7.2, 8.4_

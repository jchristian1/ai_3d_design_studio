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

- [ ] 6. Blender worker execution workflow
  - Implement the 10-step `blender.md` workflow: lock → recovery point → validate → execute → inspect → save → snapshot → preview hook → release.
  - Verify resulting state (`abs(new_x - (old_x+0.50)) < 1e-4`) before success.
  - Always release lock, including failure paths; rollback on verify mismatch.
  - Worker tests: lock acquire/release, recovery point, autosave, lock-conflict (`LOCK_CONFLICT`), verify-failure (`VERIFY_FAILED`).
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [ ] 7. AgentProvider abstraction and context builder
  - Define `AgentProvider` protocol, `AgentContext`, `AgentResult` in `services/agent`.
  - Implement `ContextBuilder` assembling context in the `agent-context.md` order.
  - Implement a deterministic `RuleBasedProvider` mapping the phrase to `move_object(delta_x=0.50)`; stub `AstraProvider` behind the same interface.
  - Structured error when object cannot be resolved.
  - Agent tests: phrase → correct tool call; unresolved object → structured error.
  - _Requirements: 3.1, 3.4, 3.5_

- [ ] 8. Worker ↔ control-plane secure connection
  - Model the worker connection as outbound authenticated (WSS or in-process interface honoring the same contract); no inbound Blender ports.
  - Read secrets from environment variables only.
  - _Requirements: 7.1, 7.2, 7.3_

- [ ] 9. API chat endpoint and job orchestration
  - Implement `POST /projects/{project_id}/chat` in `services/api` (FastAPI).
  - Validate contract, create project-scoped `Job`, invoke `AgentProvider`, return `ChatResponse`.
  - Reject missing `project_id` with structured error.
  - Integration test: API → job → worker → MCP → Blender.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 10. Preview generation
  - Implement viewport screenshot in `services/preview`; write to object storage; return `preview_url`.
  - Wire worker step 9 to trigger preview after save.
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

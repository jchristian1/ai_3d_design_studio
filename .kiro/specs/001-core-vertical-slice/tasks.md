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

- [ ] 2. Unit conversion and interpretation utilities
  - Implement cm→m conversion and direction→axis mapping (right=+X, etc.).
  - Unit tests: `"50 cm" → 0.50`, `"40 cm" → 0.40`; `right → (+1,0,0)` etc.
  - _Requirements: 3.2, 3.3_

- [ ] 3. MCP `move_object` tool
  - Implement `move_object(object_ref, delta_x_m, delta_y_m, delta_z_m)` in `services/blender-mcp`.
  - Validate object existence and finite meter deltas; return resulting `position_m`.
  - Exclude arbitrary `execute_python` from the normal interface.
  - MCP test: Cube X=0 + delta_x=0.50 ⇒ X=0.50; invalid object ⇒ `OBJECT_NOT_FOUND`; bad units ⇒ `INVALID_UNITS`.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 4. Blender worker execution workflow
  - Implement the 10-step `blender.md` workflow: lock → recovery point → validate → execute → inspect → save → snapshot → preview hook → release.
  - Verify resulting state (`abs(new_x - (old_x+0.50)) < 1e-4`) before success.
  - Always release lock, including failure paths; rollback on verify mismatch.
  - Worker tests: lock acquire/release, recovery point, autosave, lock-conflict (`LOCK_CONFLICT`), verify-failure (`VERIFY_FAILED`).
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [ ] 5. Seed test Blender project
  - Create a headless-generatable `.blend` (or generation script) with `Cube` at X=0.
  - Provide a helper to read `Cube.location` for assertions.
  - _Requirements: 8.1_

- [ ] 6. AgentProvider abstraction and context builder
  - Define `AgentProvider` protocol, `AgentContext`, `AgentResult` in `services/agent`.
  - Implement `ContextBuilder` assembling context in the `agent-context.md` order.
  - Implement a deterministic `RuleBasedProvider` mapping the phrase to `move_object(delta_x=0.50)`; stub `AstraProvider` behind the same interface.
  - Structured error when object cannot be resolved.
  - Agent tests: phrase → correct tool call; unresolved object → structured error.
  - _Requirements: 3.1, 3.4, 3.5_

- [ ] 7. Worker ↔ control-plane secure connection
  - Model the worker connection as outbound authenticated (WSS or in-process interface honoring the same contract); no inbound Blender ports.
  - Read secrets from environment variables only.
  - _Requirements: 7.1, 7.2, 7.3_

- [ ] 8. API chat endpoint and job orchestration
  - Implement `POST /projects/{project_id}/chat` in `services/api` (FastAPI).
  - Validate contract, create project-scoped `Job`, invoke `AgentProvider`, return `ChatResponse`.
  - Reject missing `project_id` with structured error.
  - Integration test: API → job → worker → MCP → Blender.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 9. Preview generation
  - Implement viewport screenshot in `services/preview`; write to object storage; return `preview_url`.
  - Wire worker step 9 to trigger preview after save.
  - _Requirements: 6.1, 6.2_

- [ ] 10. Web app chat + preview UI
  - Minimal Next.js/React chat input posting `ChatRequest` with `project_id`/`session_id`.
  - Show pending state, then success + updated preview, or a readable error.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 6.3_

- [ ] 11. Mandatory end-to-end test
  - Open test project (Cube X=0) → send "Move Cube 50 cm to the right".
  - Assert Cube X = 0.50 (tolerance), preview updated, response status = success.
  - Add failure-case E2E: Blender unavailable, lock conflict, invalid object ⇒ structured error, no corruption.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [ ] 12. Wire full path and verify slice
  - Connect all boundaries; run unit + MCP + worker + integration + E2E suites green.
  - Confirm meters canonical, AgentProvider-only access, no public Blender exposure.
  - _Requirements: 1.3, 3.4, 5.4, 7.2, 8.4_

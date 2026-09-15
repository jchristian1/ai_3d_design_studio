# Spec 001 — Verification Record (Task 13)

Audit and requirement traceability for the core vertical slice.

Every "verified" claim below is backed by an executable test, named explicitly. A
requirement is only marked verified if a test asserts the behaviour — not if the code
merely appears to implement it. Deferred items are stated as deferred.

Audit date: 2026-09-15 · Blender 5.2 LTS · Python 3.14 · Node 24

---

## 1. The full path, verified against the code

Traced by inspecting what each layer actually imports and calls, not by restating the
architecture diagram.

| # | Boundary | Module / entry point |
|---|---|---|
| 1 | Browser UI | `apps/web/components/StudioShell.tsx` → `hooks/useDesignSession.ts` |
| 2 | Browser API client | `apps/web/lib/api/client.ts` (`createApiClient`) — the only place performing HTTP |
| 3 | HTTP route | `studio_api.routes.chat.submit_chat` |
| 4 | Trusted identity | `studio_api.routes.support.get_identity` → `IdentityResolver.resolve` |
| 5 | Orchestrator | `studio_api.chat_service.ChatService.submit_chat` |
| 6 | Agent boundary | `studio_agent.provider.AgentProvider.interpret` (resolved via `providers.registry.get_provider`) |
| 7 | Semantic plan | `studio_agent.plan.AgentPlan` |
| 8 | Job construction | `studio_agent.job_factory.JobFactory.build` → `studio_contracts.jobs.create_move_object_job` |
| 9 | Canonical Job | `job.schema.json` → `studio_types.Job` |
| 10 | Worker authorization | `studio_api.worker_link.manager.WorkerConnectionManager` |
| 11 | Dispatch | `studio_api.worker_link.gateway.WorkerGateway.offer` |
| 12 | WebSocket endpoint | `studio_api.routes.worker_ws.worker_link` (`/ws/workers`) |
| 13 | Outbound worker link | `blender_worker.link.client.WorkerLinkClient` + `WebSocketWorkerTransport` |
| 14 | Execution | `blender_worker.executor.WorkerExecutor._execute_locked` |
| 15 | Lock | `blender_worker.locks.FileLockProvider.hold` |
| 16 | Project path resolution | `blender_worker.registry.MappingProjectRegistry.blend_path_for` |
| 17 | MCP domain operation | `blender_mcp.tools.move_object` (`plan_from_delta`, `handle_move_object`) |
| 18 | Blender | `blender_mcp.adapters.blender_scene` + `blender_worker/blender_scripts/` (the only `bpy`) |
| 19 | Preview | `studio_preview.blender_preview.BlenderPreviewGenerator` → `blender_scripts/render_preview.py` |
| 20 | Artifact store | `studio_preview.artifacts.LocalArtifactStore.put` |
| 21 | Result reporting | `WorkerLinkClient._deliver_result` → `worker_protocol.job_result` |
| 22 | Reconciliation | `studio_api.reconciliation.JobReconciler.observe` |
| 23 | Job status (read) | `studio_api.routes.jobs.get_project_job` — project-scoped |
| 24 | Preview bytes (read) | `studio_api.routes.artifacts.get_artifact` — project-scoped |

Verified execution order inside the lock (line offsets within `_execute_locked`):

```
load record (13) → bind idempotency (55) → read position (117) → plan (130)
→ record.plan persisted (139) → PLAN_PERSISTED (140) → recovery copy (156)
→ execute_move  ← MUTATION (174) → verify from saved file (198)
→ PROJECT_SAVED (212) → preview (219) → COMPLETED (222)
```

---

## 2. Requirement traceability

### Requirement 1 — Natural-language move request from the browser

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 1.1 | Web app sends `project_id` + `session_id` | `lib/api/client.ts`, `hooks/useDesignSession.ts` | `ui.test.ts::3: the exact Spec 001 command submits the canonical ChatRequest` | **verified** |
| 1.2 | Pending/working state displayed | `MessageInput.tsx`, `StatusBar.tsx`, `lib/session/reducer.ts` | `ui.test.ts::6 / 7: a 202 starts polling…and shows progress`, `18: sending is disabled while a change is in progress` | **verified** |
| 1.3 | Success message + updated preview | `PreviewPanel.tsx`, `reducer.ts` | `ui.test.ts::9 / 14: a succeeded job sets the image src using the API base URL`, `REGRESSION: one successful command shows exactly one terminal Studio message` | **verified** |
| 1.4 | Readable failure, no stale success | `lib/api/errors.ts`, `reducer.ts` | `ui.test.ts::11: a failed job shows a friendly error and stops polling`, `12`, `13`, `10: preview_error shows a warning while still reporting the change as applied` | **verified** |

### Requirement 2 — API control-plane request handling

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 2.1 | Validate against the shared contract | `studio_api.models.ChatRequestModel` (mirrors `chat-request.schema.json`) | `test_control_plane_api.py::3_malformed_chat_request_is_rejected` (10 cases), `17_the_chat_request_model_mirrors_the_canonical_schema` | **verified** |
| 2.2 | Job explicitly bound to `project_id` | `JobFactory`, `create_move_object_job`, `job.schema.json` | `test_spec001_mandatory_slice.py::test_mandatory_slice_move_cube_50cm_right_end_to_end`, `test_jobs.py` | **verified** |
| 2.3 | Missing `project_id` rejected | `ChatRequestModel`, `InMemoryProjectRegistry` | `test_control_plane_api.py::3_malformed…`, `4_unknown_project_is_rejected_with_404` | **verified** |
| 2.4 | Forwarded to AgentProvider, not a model | `ChatService`, `providers.registry.get_provider` | `test_control_plane_api.py::8_routes_and_service_never_name_a_concrete_provider`, `8_the_service_accepts_any_agent_provider` | **verified** |

### Requirement 3 — Agent reasoning through AgentProvider

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 3.1 | Resolves to `move_object` `delta_x = +0.50 m` | `providers/rule_based.py` → `studio_spatial.direction_delta_meters` | `test_agent_provider.py`, `test_spec001_mandatory_slice.py` (asserts `delta_meters {x:0.5,y:0,z:0}`) | **verified** |
| 3.2 | Converts to canonical metres | `packages/spatial` (`cm_to_meters`) | `test_units.py`, `test_spatial_conformance.py`, cross-language parity | **verified** |
| 3.3 | "right" → +X | `studio_spatial.directions.DIRECTION_TO_AXIS` | `test_directions.py` (incl. immutability + opposite-pair tests) | **verified** |
| 3.4 | Reached only through `AgentProvider` | `provider.py` Protocol + registry | `test_control_plane_api.py::8_*` (4 tests); audit found **zero** code-level references to a concrete provider outside `studio_agent/providers/` | **verified** |
| 3.5 | Structured error rather than guessing | `AgentResult.failure`, `RuleBasedProvider` | `test_agent_provider.py`, `test_control_plane_api.py::9_unsupported_instruction_yields_a_structured_error` (5 cases) | **verified** |

### Requirement 4 — MCP semantic move operation

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 4.1 | Applies delta in metres to the resolved object | `blender_mcp.tools.move_object` | `test_move_object.py`, `test_move_object_blender.py` | **verified** |
| 4.2 | Not `execute_python` | `mcp_server.handle_move_object` — the only tool | `test_mcp_boundary.py`; audit confirms no shell/python/eval tool and a closed protocol enum | **verified** |
| 4.3 | Structured error for bad units / missing object | `move_object`, `studio_spatial` | `test_move_object.py`, `test_spec001_mandatory_slice.py::test_failure_invalid_object_…` | **verified** |
| 4.4 | Returns resulting position for verification | `MoveObjectResult.final_position_meters` | `test_move_object.py`, mandatory E2E asserts `verified: true` | **verified** |

### Requirement 5 — Blender Worker executes safely

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 5.1 | Acquires project lock before modifying | `WorkerExecutor.execute` → `FileLockProvider.hold` | `test_worker_executor.py`, `test_spec001_mandatory_slice.py::test_failure_lock_conflict_prevents_concurrent_mutation` | **verified** |
| 5.2 | Creates a recovery point | `_create_recovery_copy` (line 156, before mutation at 174) | `test_worker_executor.py` | **verified** |
| 5.3 | Moves `Cube` +0.50 m on X | `execute_move` → Blender | mandatory E2E: saved `.blend` read by a fresh Blender = `0.50` | **verified** |
| 5.4 | Verifies resulting state, not absence of exception | re-reads the SAVED file (line 198), requires `verified` | `test_worker_executor.py`, `test_worker_blender.py`, mandatory E2E | **verified** |
| 5.5 | Saves the project | `execute_move` saves; durability re-read confirms | mandatory E2E | **verified** |
| 5.6 | Releases the lock, including on failure | `with self.locks.hold(...)` context manager | `test_worker_executor.py`, `test_the_lock_is_released_so_a_later_change_still_succeeds` | **verified** |
| 5.7 | Reports lock conflict rather than proceeding | `LockConflictError` → `LOCK_CONFLICT` | `test_failure_lock_conflict_prevents_concurrent_mutation` | **verified** |

### Requirement 6 — Preview generation and return

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 6.1 | Preview generated after save | `_attach_preview` (line 219, after `PROJECT_SAVED` at 212) | `test_worker_preview.py::1_preview_is_requested_after_the_mutation_not_before`, `test_preview_blender.py` | **verified** |
| 6.2 | Made available to the API and returned | worker protocol `preview` field → `JobReconciler` → `GET …/artifacts/{id}` | `test_artifact_routes.py` (51 tests), mandatory E2E | **verified** |
| 6.3 | Browser displays the updated result | `PreviewPanel.tsx` | `ui.test.ts::9 / 14`, `test_web_api_contract.py` (real PNG over real HTTP) | **verified** |

### Requirement 7 — Worker/control-plane security boundary

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 7.1 | Outbound authenticated connection, no inbound Blender port | `WebSocketWorkerTransport` (connect only), `worker_hello` + constant-time token | `test_worker_link.py::20_transport_module_never_listens_or_binds` (now scans the **whole** package), `20_the_worker_package_imports_no_server_machinery`, `test_worker_link_websocket.py` | **verified** |
| 7.2 | Blender ports / Python console / MCP not publicly exposed | closed protocol enum; `bpy` confined to `adapters/blender_scene.py` + `*/blender_scripts/` | `test_mcp_boundary.py`, `test_worker_link.py::18_*`, `test_api_worker_websocket.py::the_worker_endpoint_cannot_be_used_to_run_code_or_read_files` | **verified** |
| 7.3 | Secrets from environment, never committed | `link/identity.py`, `settings.py` (token excluded from `repr`) | `test_worker_link.py::19_*` (5 tests), `test_control_plane_api.py::2_workers_endpoint_does_not_leak_secrets_paths_or_environment` | **verified** |

### Requirement 8 — End-to-end verification

| # | Criterion | Implementation | Test | Status |
|---|---|---|---|---|
| 8.1 | Test project with `Cube` at X = 0 | `tests/fixtures/blender/generate_seed_project.py`, `working_copy()` | `test_seed_project_spec.py`, `test_seed_project_blender.py` | **verified** |
| 8.2 | Agent selects the correct operation | `RuleBasedProvider` + `JobFactory` | mandatory E2E asserts `provider`, `job_type`, `delta_meters`, `origin` | **verified** |
| 8.3 | Blender moves the cube and saves | `WorkerExecutor` + `SubprocessBlenderOperationExecutor` | mandatory E2E | **verified** |
| 8.4 | Cube X = 0.50 within tolerance, preview updated, response reports success | full slice | `test_mandatory_slice_move_cube_50cm_right_end_to_end` (all 11 outcomes) | **verified** |
| 8.5 | Failure cases structured, no corruption | executor error mapping + `assert_project_intact` | `test_failure_no_design_machine_connected_…`, `test_failure_blender_unavailable_on_the_worker_…`, `test_failure_invalid_object_…`, `test_failure_lock_conflict_…` | **verified** |

**All 33 acceptance criteria across 8 requirements: verified.** None deferred.

---

## 3. Audit findings

Two defects were found and fixed. Both were boundary erosion introduced by later
tasks, which is exactly what this audit exists to catch.

### Finding 1 — the no-listen guard did not cover the whole worker package

`test_20_transport_module_never_listens_or_binds` scanned only
`blender_worker/link/`. Task 11 added `blender_worker/main.py` — a new module in the
same package — which therefore entered the codebase **without ever being checked
against Requirement 7.1**. The outbound-only guarantee is a property of the
workstation, not of one directory.

*Fixed:* the guard now walks every module in `blender_worker`, checks both attribute
and bare calls, asserts a minimum scan count so silent scope loss fails, and a
companion test forbids importing any server library (`socketserver`, `http`,
`flask`, `fastapi`, `starlette`) anywhere in the package.

### Finding 2 — a worker function named `serve()`

`main.py` defined `serve(client, stopping)` for the worker's inbound loop. It opens
no socket, but the name is indistinguishable from `serve_forever()` /
`websockets.serve()` to any audit — it tripped this one. A name that has to be
explained away is a latent hazard next to a security invariant.

*Fixed:* renamed to `run_worker_loop`, with a docstring stating why. No behaviour
change.

No other production code was modified.

---

## 4. Audit results by area

### Agent boundary
- Zero code-level references to `RuleBasedProvider` / `AstraProvider` /
  `CodexProvider` outside `studio_agent/providers/` (AST scan of api, worker, mcp,
  preview, contracts, spatial).
- The API imports only `AgentProvider` (Protocol), `AgentContext`, `JobFactory`, and
  `providers.registry.get_provider`.
- No natural-language parsing outside `RuleBasedProvider`. The only direction/unit
  token tables live in `packages/spatial`, which parses **tokens, not prose**.
- Astra/Codex are placeholders: the two tests that name them assert they are
  *registered and unavailable* (`PROVIDER_UNAVAILABLE`), never that they function.
- No chain-of-thought is stored or transmitted: `ProviderMetadata` carries only
  `provider_name`, `provider_version`, `operation_count`.

### Metres / spatial
- Zero `/ 100`, `* 0.01`, or cm→m tables in runtime code outside `packages/spatial`.
- One conversion site: `studio_spatial.units.cm_to_meters`, documented as division
  (not multiplication by the inexact `0.01`), with a recorded counterexample.
- Metres flow unchanged: `AgentPlan` → `Job.payload.delta_meters` → protocol →
  `MoveObjectPlan` → Blender (scene `length_unit: METERS`, asserted in the mandatory
  E2E) → `MoveObjectResult.final_position_meters` → `ChatResponse.object_position`.
- Axis semantics: `right +X`, `left -X`, `forward +Y`, `back -Y`, `up +Z`, `down -Z`.
  Camera-relative interpretation remains **explicitly deferred** (documented in
  `directions.py` and `tasks.md`).

### Idempotency
- Mutation identity = `sha256(v2 | project_id | request_id | operation_index)`. The
  function's signature admits no payload.
- `content_fingerprint` is diagnostic: two requests with byte-identical payloads
  share a fingerprint but have **different** idempotency keys.
- Same `request_id` → same key; new `request_id` → new key; same `request_id` in
  another project → different key.
- `target` lives inside `payload`; `job.schema.json` has no path-like field and is
  closed.
- `job_id` is not a tenancy boundary: every lookup requires `project_id`, and
  `JobRecordStore.get(project_id, job_id)` makes omission a `TypeError`.
- Frontend: a new instruction generates a fresh `request_id`; an explicit retry of an
  *uncertain* submission reuses the original.

### Durability
- Plan persisted (line 139–140) **before** mutation (174); recovery copy (156) before
  mutation.
- Retry reuses the persisted plan — `read_object_position` is guarded by
  `if record.plan is None`, and the `else` branch calls `_plan_from_record` and
  provably does **not** re-read the scene.
- Preview runs after `PROJECT_SAVED`, and never affects `job_status`.
- Result-delivery failure never re-executes: the journal records non-delivery and
  reconnect resends as `duplicate`.
- Locks are per `project_id`; project paths come only from `ProjectLocator`, never
  from a job or a network message.

### Blender / MCP exposure
- One semantic MCP tool: `handle_move_object`. No `execute_python`, shell, `eval`,
  `exec`, or file tool.
- Protocol vocabulary is a closed 12-value enum with `additionalProperties: false`;
  none of `path`, `script`, `code`, `command`, `subprocess` is representable.
- `bpy` appears only in `adapters/blender_scene.py` and `*/blender_scripts/`.
- Absolute desired-after write with precondition check
  (`PRECONDITION_MISMATCH` blocks unsafe mutation); stable `studio_object_id`
  supported.
- Blender is located in exactly one place: `blender_mcp.blender_runtime`.

### Network posture
- The workstation never listens (whole-package AST guard).
- Local development: `ws://127.0.0.1:8000/ws/workers`.
- Production expectation: `wss://…` terminated in front of the route, with
  `WebSocketWorkerTransport(require_secure=True)` refusing plaintext. **Not
  implemented** — deployment is out of scope for Spec 001.

### API tenancy / security
Verified against a live application:

| Check | Result |
|---|---|
| Route surface | 7 paths, all project-scoped for jobs and artifacts |
| `/api/jobs/{id}` (unscoped) | 404 — route does not exist |
| `/api/artifacts/{id}` (unscoped, ±query) | 404 — route does not exist |
| Artifact in another **real** project | 404, body **byte-identical** to never-existed |
| `blend_path` / `path` / `user_id` in body | 422 |
| `project_id` = `../../etc/passwd`, `..`, `proj_seed/../proj_other` | 404 |
| `.blend`, `.env`, journal file requested as an artifact | 404, no secret in body |
| Token / paths in `/health`, `/api/workers`, `/openapi.json`, `preview/latest` | none |

### Preview / artifacts
- Generated after durable save; failure is non-fatal (`job_status` stays `succeeded`
  with `preview_error` set).
- Identity derived from `(project_id, job_id, artifact_type)`: a completed job reuses
  its artifact, a new job creates a new one, and earlier artifacts are never
  overwritten.
- Checksum is `sha256:<64 hex>`, verified equal to the served bytes.
- `PreviewArtifact` contract has **no** path and no url field — `(project_id,
  artifact_id)` is the whole address.
- Only registered artifacts are servable (strict id pattern + store-derived
  filename + containment re-check after symlink resolution).
- PNG metadata stripped, so the image bytes contain no `.blend` path or hostname.

### Frontend / session
- One command → one user transcript item; one submission → one Studio reply slot that
  transitions progress → terminal (replace-by-derived-id).
- No duplicate terminal replies, including under React Strict Mode; the reducer is
  pure.
- `session_id` stable per browser session; new command → fresh `request_id`; explicit
  retry → same `request_id`.
- A second mutation is disabled while one is non-terminal.
- All HTTP is centralized in `lib/api`; no `fetch()` elsewhere.
- No filesystem path, token, or internal worker detail is rendered, and the built
  bundle contains no secret. `NEXT_PUBLIC_API_BASE_URL` is the only public variable.

### Packaging
- A genuinely fresh venv (`python3 -m venv` → `pip install -e ".[dev]"`) installs
  cleanly and all nine packages import with **no `PYTHONPATH`**:
  `studio_types`, `studio_contracts`, `studio_validation`, `studio_spatial`,
  `studio_agent`, `studio_api`, `studio_preview`, `blender_mcp`, `blender_worker`.
- `studio_preview` **is** in editable discovery (added with Task 10) — no packaging
  defect found.
- Non-package runtime resources resolve via `__file__`: 22 canonical schemas, the
  preview render script, and both worker Blender scripts.

### Schemas / contracts
- `SCHEMA_FILES` parity: 22 entries in both languages, identical keys and values.
- Every declared schema exists on disk; no schema is unreferenced.
- Cross-language verdict parity passes (TypeScript executed in Node vs Python over
  the same corpus).
- `AgentPlan` remains an internal Python boundary with **no** wire schema — the
  earlier decision is preserved and its rationale is still documented in `plan.py`.
- Worker protocol v2, `SUPPORTED = (1, 2)`; schema minimum 1.

---

## 5. Test totals

| Suite | Command | Result |
|---|---|---|
| Full fast Python | `pytest` | **1250 passed**, 78 deselected |
| All Blender-marked | `pytest -m blender` | **78 passed** |
| MCP | `pytest services/blender-mcp` | **92 passed** |
| Worker | `pytest services/blender-worker` | **161 passed** |
| Worker transport / integration | `pytest tests/integration` | **30 passed** |
| API | `pytest services/api` | **191 passed** |
| Mandatory Spec 001 E2E | `pytest -m blender tests/e2e/test_spec001_mandatory_slice.py` | **7 passed** |
| All E2E | `pytest -m blender tests/e2e` | **20 passed** |
| Shared TypeScript | `npm run test:ts` | **460 passed** |
| Frontend | `npm test --workspace @studio/web` | **88 passed** |
| TypeScript compile | `npx tsc --noEmit` | clean |
| Next.js build | `npm run build` | success |

---

## 6. Known limitations (deliberate, documented)

These are scope boundaries, not gaps in Spec 001:

| Limitation | Scope |
|---|---|
| One project (`proj_seed`), no picker or creation flow | later milestone |
| No authentication — a fixed development user, refused outside `local` | authentication task |
| In-memory control-plane job records — reporting is lost on restart; durability is the worker's journal | Task 3 `JobStore` (PostgreSQL/Redis) |
| Single worker; `SingleReadyWorkerSelector` is not a scheduler | multi-worker task |
| `flock` is single-machine and will not coordinate multiple worker hosts | distributed lock |
| `RuleBasedProvider` understands one narrow grammar | Astra/Codex task |
| Still-image Workbench preview; no Cycles, GLB, or Three.js | render / interactive tasks |
| Polling, not server-pushed updates (behind `JobUpdateSource`) | SSE/WebSocket task |
| Camera-relative direction interpretation | explicitly deferred |
| No TLS in local run; production `wss://` not implemented | deployment task |
| `expected_before` captured at execution, not against a scene version | collaborative editing |
| Subprocess-per-Blender-operation (~1 s each) | persistent worker process |

---

## 7. Conclusion

All 33 acceptance criteria across Requirements 1–8 are **verified by executable
tests**. The full path from browser to Blender and back is implemented, connected,
and protected by regression tests at unit, MCP, worker, integration, and end-to-end
levels. Two boundary-erosion defects found by this audit were fixed.

**Spec 001 — Core Vertical Slice: COMPLETE.**

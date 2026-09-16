# Spec 002 — Verification Record

What is actually proven about the multimodal design studio MVP, and what is not.

Every "verified" row names an executable test. A requirement is marked verified only
when a test asserts the behaviour — not when the code appears to implement it. Where a
claim cannot be tested in this repository, it says so plainly.

Audit date: 2026-09-15 · Blender 5.2.2 LTS · Python 3.14 · Node 24 ·
Codex CLI 0.154.0 · official Blender MCP `ff54e4d8` (server 1.0.2)

Suite state at audit time:

| Tier | Command | Result |
|---|---|---|
| Python, offline | `pytest` | **2495 passed**, 97 deselected |
| Shared TypeScript | `npm run test:ts` | **1131 passed** |
| Browser | `npm test --workspace @studio/web` | **130 passed** |
| Browser build | `npm run build --workspace @studio/web` | clean (type check included) |
| Official MCP + real Blender | `pytest -m mcp` | **19 passed** |
| Real Blender (Spec 001) | `pytest -m blender` | see Spec 001 record |
| Real Astra | `pytest -m codex` | **NOT RUN — blocked, see §4** |

---

## 1. The path, traced through the code

| # | Boundary | Module / entry point |
|---|---|---|
| 1 | Browser workspace | `apps/web/components/WorkspaceShell.tsx` → `hooks/useWorkspace.ts` |
| 2 | Browser API client | `apps/web/lib/api/workspace.ts` (the only place performing workspace HTTP) |
| 3 | 3D viewer + selection | `apps/web/components/ModelViewer.tsx` (three.js, GLB, click → `selected_object_id`) |
| 4 | Upload route | `studio_api.routes.workspace.upload_reference` |
| 5 | Ingestion | `studio_api.ingest.service.ReferenceIngestService.ingest` → `ingest.documents.ingest_pdf` |
| 6 | Chat route | `studio_api.routes.workspace.design_chat` |
| 7 | Context construction | `studio_api.context_builder.ContextBuilder.build` → `studio_agent.agent_input.AgentInput` |
| 8 | Agent boundary | `studio_agent.design_provider.DesignAgentProvider.respond` (resolved by name) |
| 9 | Astra | `studio_agent.providers.codex_astra.CodexAstraProvider` → `studio_agent.codex.CodexClient` → `codex exec` |
| 10 | Output validation | `studio_agent.proposal.parse_agent_response` (closed capability table) |
| 11 | Code risk classification | `studio_validation.code_risk.classify_python` |
| 12 | Turn orchestration | `studio_api.design_chat.DesignChatService.submit` / `.decide_approval` |
| 13 | Job construction | `studio_contracts.jobs.create_capability_job` → `apply-capabilities-payload.schema.json` |
| 14 | Dispatch | `studio_api.worker_link.gateway.WorkerGateway.offer` → `/ws/workers` |
| 15 | Worker link | `blender_worker.link.client.WorkerLinkClient` |
| 16 | Routing | `blender_worker.dispatch.DispatchingExecutor` |
| 17 | Plan execution | `blender_worker.capability_executor.CapabilityPlanExecutor._execute_locked` |
| 18 | Capability boundary | `blender_worker.capability.provider.BlenderCapabilityProvider` |
| 19 | Official MCP backend | `blender_worker.backends.official.backend.OfficialBlenderLabBackend` |
| 20 | Official MCP session | `backends.official.session.OfficialMcpSession` (stdio, `execute_blender_code_for_cli`) |
| 21 | Blender | real Blender, headless, launched by the official MCP |
| 22 | GLB export | `capability_executor._export_model` → `studio_preview.artifacts.LocalArtifactStore.put` |
| 23 | Result reporting | `WorkerLinkClient._deliver_result` / `.reconcile` → `journal.merge_report_result` |
| 24 | Scene + artifact cache | `studio_api.scene_reporting.SceneReporter.observe` |
| 25 | Reads | `routes.workspace.workspace` / `.scene` / `.latest_model`, `routes.artifacts.get_artifact` |

Order inside the project lock, per step (`CapabilityPlanExecutor`):

```
load record → bind idempotency → read authoritative scene → check scene version
→ ONE recovery point → per step: already satisfied? → invoke → VERIFY by re-reading
→ persist step → … → save → inspect scene → export GLB → COMPLETED
```

---

## 2. Requirement traceability

| Req | Subject | Verified by | Status |
|---|---|---|---|
| 1 | Authoritative scene grounding | `test_capability_executor.py` (scene read before every plan), `test_selected_object_editing.py::test_a_second_relative_edit_builds_on_the_first` | verified |
| 2 | Scene version identity + enforcement | `test_selected_object_editing.py::test_a_plan_made_against_a_stale_scene_is_refused_rather_than_applied`, `tests/contracts/test_scene_digest_cross_language_parity.py` | verified |
| 3 | Real LLM behind the existing abstraction | `test_agent_provider.py` (layering guards), `services/agent/tests/test_codex_client.py` | verified |
| 4 | Constrained structured output | `test_agent_proposal.py`, `tests/security/test_untrusted_model_output.py` (unknown capability, bad shapes, non-finite, malformed, runaway) | verified |
| 5 | Read-only questions | `test_mvp_acceptance.py::test_a_question_is_answerable_without_any_upload_or_mutation` | verified |
| 6 | Clarification as an outcome | `test_design_chat.py`, `test_mvp_acceptance.py` (steps 3–4: asked, then resolved) | verified |
| 7 | Object resolution | `test_capability_backend.py`, `test_selected_object_editing.py::test_editing_the_selected_wall_changes_only_that_wall`, `tests/mcp/test_full_stack_against_real_blender.py` (stable id written into the `.blend`) | verified |
| 8 | Canonical units, angles, colour | `packages/spatial` suites incl. `conversion-site-guard.test.ts`, `test_platform_scripts.py`, `test_mvp_acceptance.py` (metric geometry read back) | verified |
| 9 | Capability provider on the official MCP | `tests/mcp/test_official_blender_mcp.py` (16 tests, real Blender), `tests/mcp/test_full_stack_against_real_blender.py` | verified |
| 10 | Platform-owned policy over the tool surface | `test_capability_backend.py`, `test_capability_classifier.py` | verified |
| 11 | Model-authored Python, classified + approved | `test_capability_classifier.py`, `tests/security/test_untrusted_model_output.py` (parked, rejected, approved, stolen token) | verified |
| 12 | Blender / MCP exposure | `test_mcp_boundary.py`, `test_worker_main.py` (no listening socket), `tests/security/` layering guards | verified — except §3.4 |
| 13 | Durable mutation guard | `test_capability_executor.py`, `tests/e2e/test_failure_cases.py` (mid-plan failure, resume, lock conflict) | verified |
| 14 | Multi-operation requests | `test_mvp_acceptance.py` (5 operations, ONE job), `test_progress_reporting.py` | verified |
| 15 | Safety and threat resistance | `tests/security/test_untrusted_model_output.py` (injection via upload, hostile output) | verified at layers 1–2; layer 3 is the OS, see §4 |
| 16 | Upstream pinning + upgrade governance | `tests/mcp/test_official_blender_mcp.py::test_step_1_the_pinned_identity_is_recorded_and_installed`, `external/official-blender-mcp.pin.json` | verified |
| 17 | Long-term capability not foreclosed | architecture review only — `BlenderCapabilityProvider` has a second implementation (`FakeBlenderCapabilityProvider`), which is the evidence that it is a real seam | partially verified |
| 18 | Browser experience | `apps/web/tests/workspaceUi.test.ts`, `workspace.test.ts` (130 tests: composer at the bottom, one reply slot, selection, approvals) | verified |
| 19 | Spec 001 regression compatibility | the whole Spec 001 suite still passes unchanged; `test_dispatch.py` proves routing | verified |
| 20 | Astra via local Codex, no API key | `tests/security/test_no_api_key.py` (whole source tree), `test_codex_client.py`, `test_codex_login.py` | verified |
| 21 | Project-scoped references | `test_workspace_routes.py`, `test_storage.py` | verified |
| 22 | PDF and image ingestion | `test_ingest.py` (real PDFs, real page rasterisation), `test_mvp_acceptance.py` step 2 | verified |
| 23 | Structured design understanding | `test_context_builder.py`, `test_design_chat.py`, `test_mvp_acceptance.py` (fact recorded and reused) | verified |
| 24 | Interactive 3D workspace | `apps/web/tests/workspaceUi.test.ts`; GLB pipeline in `test_mvp_acceptance.py` and the `-m mcp` tier | verified |
| 25 | Graceful degradation | `test_mvp_acceptance.py::test_a_project_with_no_worker_connected_refuses_instead_of_pretending`, `test_failure_cases.py` (backend offline, failed export), `AstraConnect` states | verified |

---

## 3. Audit findings

### 3.1 The dispatching executor hid the journal from reconciliation — FIXED

`WorkerLinkClient` marks delivery and resends undelivered results through
`executor.store`. When `DispatchingExecutor` became the top-level executor it exposed
no `store`, so `reconcile()` raised `AttributeError` and `_mark_delivery` — which
swallows exceptions to protect the link — silently stopped recording delivery. Nothing
failed until a report was lost, which is precisely when redelivery matters.

Fixed by exposing the shared journal, refusing two journals at construction, and
logging instead of swallowing silently. Regression:
`test_dispatch.py::test_the_dispatcher_exposes_the_shared_journal`,
`::test_two_journals_are_refused_at_construction`.

Found by `tests/e2e/test_failure_cases.py`, not by review.

### 3.2 A resent result dropped the scene and the model — FIXED

Reconciliation resent `record.result` alone. The scene snapshot and the GLB are
journalled in their own fields, so a recovered success arrived without them: the
browser would show a completed job and no new model, and the next agent turn would be
grounded on a stale scene. Live and resent reports are now assembled by one function,
`journal.merge_report_result`. Regression:
`test_dispatch.py::test_a_resent_result_carries_the_scene_and_the_model`.

### 3.3 Reference text was not marked as data in the prompt — FIXED

`build_prompt` passed extracted PDF/document text into the prompt without stating that
it is content rather than instruction. Astra's rules now say so, and the section header
says it inline. This is defence in depth: the protection against a confused turn is
that nothing the model returns executes without validation
(`tests/security/test_untrusted_model_output.py`).

### 3.4 The classifier is not a sandbox — ACCEPTED, DOCUMENTED

Model-authored Python is allowed by design (Requirement 11). The AST classifier
provides visibility and friction; it does not contain anything, and the official MCP
executes what it is given — upstream's own `weak_sandbox.py` says as much. The real
boundary is the operating system: run the worker and Blender as a restricted user or in
a container with only the project directory mounted. Stated in
`requirements.md` R12.10, in `code_risk.py`'s own module docstring, and in `README.md`.
No test in this repository can assert it.

### 3.5 A stale artifact row can outlive its bytes — OPEN, LOW

If `runtime/artifacts/` is deleted while `runtime/studio.sqlite3` is kept, the workspace
response still advertises the last preview and the browser gets a 404 for it. Observed
during the live run below. Cosmetic (a missing image), never incorrect geometry. A fix
would have the workspace route confirm existence in the artifact store before
advertising it.

---

## 4. What is NOT verified

**The real Astra path has not been run.** `codex login status` reports "Not logged in",
and signing in requires Christian's browser and ChatGPT account. Therefore:

- No test has confirmed that `gpt-6-astra` returns output satisfying
  `agent-response.schema.json` in practice.
- No test has confirmed how well it reads a real floor plan.
- The `-m codex` tier exists and is not run.

Everything *around* the model is verified: the prompt assembly, the schema handed to
Codex, the parsing, the validation, the failure modes when Codex is missing, out of
date, not signed in, or returning nonsense. What is unverified is the model's own
behaviour.

To close this: click **Sign in with ChatGPT** in the workspace (or run `codex login`),
then run `pytest -m codex`.

---

## 5. Live run on this machine

Not a test — an actual run of the three processes, recorded because "the suite passes"
and "the product runs" are different claims.

```
./scripts/run_studio.sh
```

Observed:

| Check | Result |
|---|---|
| `GET /health` | `registered_workers: 1`, `ready_workers: 1`, `blender_capable_workers: 1` |
| `GET /api/status/blender` | `connected`, `Blender 5.2.2 LTS`, `supports_modelling: true` |
| `GET /api/status/astra` | `login_required` — "Sign in to ChatGPT to connect Astra" (the known blocker) |
| Web app | Next.js serving on `http://localhost:3000`, workspace + status polling `200` |
| `POST /api/chat` "Move Cube 50 cm to the right." | job `succeeded`, `verified: true`, X 1.0 → 1.5 m |
| Saved `.blend`, reopened by a fresh Blender | `Cube` at X = 1.5 m — durable, not worker memory |
| Preview artifact over HTTP | `200`, `image/png`, 640×360, 180 906 bytes, valid PNG |

One real defect surfaced only by running it: a **stale API process from an earlier
session** was still bound to port 8000 with an older copy of the contracts, and refused
the worker with `supported_job_types[1]: value not in enum`. The code was correct; the
running process was old. Worth knowing because the symptom looks exactly like a
contract bug.

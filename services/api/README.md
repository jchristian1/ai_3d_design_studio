# Control Plane — `services/api` (Spec 001, Task 9)

The first application-facing API. It accepts a natural-language design change over
HTTP, resolves it to a canonical Job through the AgentProvider boundary, and
dispatches it to an outbound-connected Blender worker.

```
HTTP ChatRequest
      ↓
FastAPI control plane            app.py, routes/
      ↓
trusted application context      identity.py, projects.py
      ↓
AgentProvider                    studio_agent (replaceable AI boundary)
      ↓
AgentPlan
      ↓
JobFactory                       studio_agent
      ↓
canonical Job                    studio_contracts.jobs (Task 3)
      ↓
WorkerConnectionManager          worker_link/manager.py (Task 8)
      ↓
outbound-connected Blender Worker
```

## Local run

```bash
source .venv/bin/activate
uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`--factory` is required: this package deliberately exposes a factory rather than a
module-level `app`, so configuration is validated per application and tests can
inject their own dependencies. Accessing `studio_api.app.app` raises a message
saying so.

The worker connects to `ws://127.0.0.1:8000/ws/workers` (see
`STUDIO_CONTROL_PLANE_URL` in the worker's environment).

Interactive documentation is at `/docs`; the machine-readable schema is at
`/openapi.json`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | control-plane process health |
| `GET` | `/api/workers` | connected workers, safe development view |
| `POST` | `/api/chat` | submit a design change |
| `POST` | `/api/projects/{project_id}/chat` | project-scoped form of the above |
| `GET` | `/api/projects/{project_id}/jobs/{job_id}` | status and result of a change |
| `WS` | `/ws/workers` | worker link (Task 8 protocol) |

Job retrieval is project-scoped **by path**, and there is no unscoped variant. A
`job_id` is an identifier, not a capability.

`/api` is the versionable prefix. A future breaking change becomes `/api/v2`
without disturbing `/health` (an infrastructure probe) or `/ws/workers`, whose
compatibility is governed by the protocol's own `protocol_version`.

## Architecture

```
routes/            parse HTTP, call a service, render the result — nothing else
  health.py  chat.py  jobs.py  workers.py  worker_ws.py  support.py
      |
chat_service.py    ChatService.submit_chat(request, identity) -> ChatSubmission
      |            the whole workflow, testable without a web framework
      +-- projects.py          authorize a LOGICAL project id
      +-- identity.py          establish user_id server-side
      +-- (AgentProvider)      interpret language -> AgentPlan
      +-- (JobFactory)         AgentPlan -> canonical Job
      +-- job_records.py       record with a derived mutation identity
      +-- worker_selection.py  choose a worker
      +-- worker_link/gateway.py  push the offer
      |
reconciliation.py  apply worker reports back onto job records
```

Supporting modules: `settings.py` (all configuration), `dependencies.py` (the
explicit container), `errors.py` (structured failures → HTTP), `models.py` (HTTP
view models).

### App factory and dependency injection

```python
create_app(settings=None, dependencies=None) -> FastAPI
```

`build_dependencies(settings, ...)` assembles the real graph; `create_app` stores it
once on `app.state.dependencies`. There is no module-level mutable state and no
import-time singleton, so two applications can run in one process without sharing
anything.

Every collaborator is a Protocol with an in-memory implementation today:

| Concern | Spec 001 | Replaced by |
|---|---|---|
| identity | `DevelopmentIdentityResolver` | real authentication task |
| projects | `InMemoryProjectRegistry` | PostgreSQL registry with permissions |
| job records | `InMemoryJobRecordStore` | Task 3 `JobStore` (Redis/PostgreSQL) |
| agent provider | resolved by name from the registry | `AstraProvider` / `CodexProvider` |
| worker selection | `SingleReadyWorkerSelector` | multi-worker scheduler |

## Chat flow

`POST /api/chat` → `ChatService.submit_chat`:

1. **authorize the project** — unknown or path-shaped `project_id` → 404
2. **build `AgentContext`** — `user_id` from the identity resolver, never the body
3. **`provider.interpret()`** — the replaceable AI boundary → `AgentPlan`
4. **`JobFactory.build()`** — canonical Job with a derived `idempotency_key`
5. **`store.submit()`** — atomic insert-or-return-existing, keyed by
   `(project_id, idempotency_key)`
6. **`selector.select()`** — a ready, compatible worker, or 503
7. **`gateway.offer()`** — the offer is pushed to the worker

The route itself only parses HTTP and renders the resulting `ChatSubmission`. No
concrete provider class is named anywhere in `routes/` or `chat_service.py`, which a
test enforces.

### Why the response is 202, and why there is a separate status endpoint

A successful submission means *understood, recorded with a stable identity, and
handed to a worker* — not *Blender is finished*. Blender takes seconds to minutes,
and an HTTP request must not own a mutation's lifetime. So `POST /api/chat` returns
`202 Accepted` with the job identity, and the client polls `GET /api/projects/{project_id}/jobs/{job_id}`.

`ChatService.await_terminal()` exists for tests and local scripting. No route calls
it: the architecture is asynchronous, not synchronous with a convenience wrapper.

### Why the chat response is not the canonical `ChatResponse`

`chat-response.schema.json` describes the *final answer* about a change and declares
`additionalProperties: false`, so it cannot carry a `job_id`. The submission
acknowledgement is a different message and gets its own envelope. Once a job is
terminal, `GET /api/projects/{project_id}/jobs/{job_id}` embeds the canonical
`ChatResponse` verbatim under `chat`, so a browser consumes the contract rather than
an API-shaped variant.

## Worker WebSocket endpoint

`/ws/workers` binds Task 8's link to Starlette. No protocol logic is
reimplemented: message construction, parsing, validation and redaction come from
`studio_contracts.worker_protocol`, and authentication, liveness and eligibility come
from `WorkerConnectionManager`.

`worker_link/gateway.py` adds the one thing the manager deliberately lacks — a
registry of *where* a connected worker is — while itself importing no web framework.
A connection is just a `sink`: a callable that delivers one message.

**The control plane listens; the workstation still connects out.** That is the
security posture, not a weakening of it: the workstation needs no inbound port, so
Blender, `bpy` and the local MCP boundary stay unreachable from the network. What is
exposed is only the constrained worker protocol, whose canonical schema has a closed
message enum and `additionalProperties: false` at every level — it cannot express a
shell command, a Python expression, a script, or a filesystem path. Exposing this
route does not expose Blender or MCP.

Authentication is the protocol's: `worker_hello` carries the pre-shared token, the
manager compares it in constant time and **discards** it, and an unauthenticated
worker is rejected and disconnected without ever being registered.

### Concurrency, and the deadlock this preserves a fix for

Two properties drive the implementation:

1. **One writer.** Starlette does not serialize concurrent sends, so *every*
   outbound message — protocol replies and pushed job offers alike — goes through a
   single queue drained by one sender task. Ordering is preserved, so
   `worker_registered` always precedes offers flushed at registration.

2. **Sending must not depend on receiving.** The sender task is independent of the
   receive loop.

Task 8 found a deadlock where offers were flushed only *in reaction to* an inbound
message, so an idle worker and the server waited on each other. The invariant, pinned
by regression tests at both the unit and integration tier:

> Dispatch of a job to a connected ready worker MUST NOT depend on an inbound
> heartbeat or any other worker message.

## Trusted identity

`user_id` is never read from the request body. The canonical `ChatRequest` has no
`user_id` field and closes its object, so a client cannot send one — and because the
HTTP model mirrors that with `extra="forbid"`, attempting it is a 422 rather than a
silently ignored field.

```
HTTP request  ->  IdentityResolver  ->  TrustedIdentity  ->  AgentContext
                  ^^^^^^^^^^^^^^^^
                  the ONLY source of user_id
```

`DevelopmentIdentityResolver` returns one fixed configured user. It is **temporary**
and performs no verification. It fails closed outside the `local` environment, so a
deployment that forgets to install a real resolver returns 401 instead of silently
authenticating every caller as the development user. Replacing it is a one-line
change in `dependencies.py`; no route, service, or provider is touched.

## Local project registry

The control plane knows a **logical project id** and nothing else:

```
proj_seed  ->  ControlPlaneProject(project_id="proj_seed", display_name="Seed")
```

It does not know, store, or accept a filesystem path. The worker resolves
`project_id` to a `.blend` on the machine that owns the file
(`blender_worker.registry.MappingProjectRegistry`). The control plane authorizes;
the worker locates.

```
browser --project_id--> control plane --project_id--> worker --path--> .blend
                        (no paths)                   (owns paths)
```

Three independent defences against a client naming a file:

1. the canonical `ChatRequest` schema has no path field and is closed;
2. `project_id` must be a safe single path segment — `../../etc/passwd` is rejected
   before anything downstream sees it;
3. the id must be in an explicit allow-list; an unknown project is refused, never
   guessed at.

`is_safe_project_id` restates a check that also exists in the worker. That is
deliberate: two processes on two trust boundaries each validate for themselves, and
the API does not import worker internals to check its own input.

## Job status model

The canonical Task 3 lifecycle, not an API-specific vocabulary:

```
queued  ->  claimed  ->  running  ->  succeeded | failed
```

These five are the **only** canonical public job states. `accepted` is deliberately
not one of them.

| Worker message | Recorded status |
|---|---|
| `job_accepted` | `claimed` |
| `job_progress` | `running` |
| `job_rejected` | `failed` |
| `job_result` `succeeded` | `succeeded` |
| `job_result` `duplicate` | `succeeded` + `reconciled: true` |
| `job_result` `failed` | `failed` |

The worker's protocol word "accepted" is reported as the contract word `claimed` —
the same event, named by the contract. `duplicate` is a *reporting* status: the
worker already had the job completed in its durable journal and resent the stored
result without touching Blender, so the mutation happened exactly once.

Terminal states never regress: a late `job_progress` cannot move `succeeded` back to
`running`.

### Project isolation of job retrieval

A `job_id` is an identifier, **not a capability**. Knowing one grants no access.

Records are keyed by `(project_id, job_id)` and `JobRecordStore.get` takes
`project_id` as a required, leading argument, so there is no unscoped lookup to
call — isolation is structural rather than a check a route could forget. On the
route:

1. `project_id` is authorized through the trusted project registry **first**; an
   unknown or path-shaped project is 404 and no lookup happens;
2. the store is queried with `(project_id, job_id)`, so another project's job simply
   is not found;
3. a job that exists in a *different* project returns a byte-identical 404 to a job
   that never existed, so the response cannot be used to probe for cross-project
   existence.

`status_url` on the submission response is built by the jobs route itself
(`job_status_path`), so the advertised URL and the actual route can never disagree.

### In-memory state, and why it cannot cause a re-execution

`InMemoryJobRecordStore` holds the control plane's *observation* of a job. The
worker's durable journal remains authoritative for whether Blender was mutated.
Losing this state loses **reporting**, never **durability**:

- mutation identity is **derived**, not stored: `(project_id, request_id,
  operation_index)` hashes to the same `idempotency_key` after a restart, so a
  resubmitted request cannot become a new mutation;
- the worker independently refuses to re-execute a completed `job_id` from its own
  journal (Task 6), so even a re-offer after a restart mutates nothing.

An empty store means "I do not remember", never "it did not happen". That is why a
`job_result` for an unknown job is **adopted** into the store rather than discarded
or answered with new work — the control plane is catching up to a mutation that
already happened. Adoption starts no work; a `job_progress` for an unknown job
creates nothing, because progress is not evidence of a completed mutation.

### Reconciliation

Reconciliation never creates work. A resent result updates the existing record and
sets `reconciled`. Retrying a completed `request_id` returns the existing job with
`duplicate: true` and does not re-offer it. Reusing a `request_id` for a *different*
instruction is a client error → 409, because mutation identity would otherwise claim
two different changes are the same one.

## Error mapping

Two vocabularies, kept separate on purpose:

- **`ChatError.code`** — canonical, cross-boundary, in the response body
  (`error-code.schema.json`, shared with MCP and the worker);
- **`FailureReason`** — API-level, decides the HTTP status only; never serialized.

The canonical codes intentionally do not encode HTTP semantics: "unknown project"
and "malformed field" are both `VALIDATION_ERROR` to a worker, yet 404 and 422 to a
browser. Adding a `PROJECT_NOT_FOUND` code would change a cross-language contract
for a purely transport concern, so the service reports a reason alongside the
canonical error instead.

| Situation | HTTP | Canonical code |
|---|---|---|
| malformed request / unknown field / blank id | 422 | `VALIDATION_ERROR` |
| unsupported instruction | 422 | `UNSUPPORTED_INSTRUCTION` |
| unsupported units | 422 | `INVALID_UNITS` |
| unknown or unsafe project | 404 | `VALIDATION_ERROR` |
| unknown job id, or a job in another project | 404 | `VALIDATION_ERROR` |
| `request_id` reused for different content | 409 | `PRECONDITION_MISMATCH` |
| no ready worker | 503 | `BLENDER_UNAVAILABLE` |
| provider unavailable | 503 | `PROVIDER_UNAVAILABLE` |
| identity could not be established | 401 | `VALIDATION_ERROR` |
| unexpected exception | 500 | `INTERNAL_ERROR` |

Every failure returns the same body:

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "…" }, "request_id": "req_…" }
```

FastAPI's `{"detail": …}` never appears. Validation failures are described by
**field**, never by submitted **value**, so a secret a client mistakenly sent is not
reflected back. Unexpected exceptions become a fixed `INTERNAL_ERROR` body while the
traceback goes to the server log: no stack trace, exception class, filesystem path,
configuration value, or token reaches a client.

## Health and worker endpoints

`GET /health` reports what this process can know. `api: healthy` means the control
plane is serving and says nothing about Blender. `blender_capable_workers` counts
connected workers that advertised a usable Blender, so it is `0` when none have
connected — chat submissions then fail with 503 while `api` stays `healthy`, which is
the honest answer rather than a contradiction.

`GET /api/workers` returns only what a worker chose to advertise, projected through
an allow-list model that drops unknown keys. It cannot expose the token (the manager
never stores one), filesystem paths, the home directory, environment variables, or
any other secret.

## Configuration

All configuration is read in `settings.py` and nowhere else; route handlers never
touch `os.environ`.

| Variable | Default | Purpose |
|---|---|---|
| `STUDIO_API_ENVIRONMENT` | `local` | enables development-only affordances |
| `STUDIO_API_HOST` | `127.0.0.1` | bind host (loopback by default) |
| `STUDIO_API_PORT` | `8000` | bind port |
| `STUDIO_API_ALLOWED_ORIGINS` | *(empty)* | explicit CORS origins, comma-separated |
| `STUDIO_API_AGENT_PROVIDER` | `rule_based` | which AgentProvider to resolve |
| `STUDIO_API_DEVELOPMENT_USER_ID` | `user_dev_local` | the temporary development user |
| `STUDIO_API_PROJECT_IDS` | `proj_seed` | logical project allow-list |
| `STUDIO_API_HEARTBEAT_INTERVAL_SECONDS` | `15` | liveness cadence given to workers |
| `STUDIO_WORKER_TOKEN` | *(none)* | pre-shared worker token — **never committed** |

The worker token deliberately reuses the worker's own variable name: it is the same
secret and must not have two names. It is excluded from `repr`, so it cannot reach a
log line or traceback.

`Settings.validate()` runs at application construction, so a misconfigured process
fails at startup rather than at the first request. It refuses a blank worker token
outside `local`, and a `*` CORS origin outside `local`.

### CORS

No origins configured means the middleware is **not installed at all** — the correct
posture while no browser client exists. When the web task arrives, its origin is
listed explicitly; `*` is refused outside local development.

## Testing

```bash
# fast: no sockets, no Blender  (124 tests)
pytest services/api/tests/

# integration: real HTTP + real WebSockets, fake Blender  (19 tests)
pytest tests/integration/test_api_worker_websocket.py

# end to end: real HTTP + real WebSockets + REAL BLENDER  (5 tests, opt-in)
pytest -m blender tests/e2e/test_api_blender_e2e.py
```

The Blender tier is opt-in because it launches Blender processes. Every wait in the
integration and E2E tests is bounded by wall-clock time, so a deadlock fails fast
and visibly instead of hanging the suite.

## Boundaries that remain temporary

These are known gaps, not oversights. Each is scoped to a later task:

| Temporary | Until |
|---|---|
| `DevelopmentIdentityResolver` — a fixed user, no verification | authentication task |
| `InMemoryJobRecordStore` — reporting lost on restart | Task 3 `JobStore` (PostgreSQL/Redis) |
| `InMemoryProjectRegistry` — a configured allow-list, no ownership or permissions | database + permissions task |
| `SingleReadyWorkerSelector` — first ready worker, no scheduling | multi-worker task |
| `RuleBasedProvider` — a tiny grammar, not real language understanding | Astra/Codex provider task |
| No TLS in the local run — loopback only | deployment task (`wss://`, terminated in front) |
| No preview or render — `preview_url` is never populated | Task 10 |
| No CORS origins configured | Task 11 (web app) |
| One operation per request; multi-operation plans are refused | future planning work |
| `InMemoryProjectRegistry` has no per-user ownership check, so any caller may read any *registered* project | authentication + permissions task |

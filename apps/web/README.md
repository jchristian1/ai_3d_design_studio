# Browser Interface — `apps/web` (Spec 001, Task 11)

The first usable browser experience: describe a change in plain language, watch it
happen, see the result.

```
open browser
     ↓
Spec 001 project is already open
     ↓
current preview loads (or an empty state)
     ↓
type "Move Cube 50 cm to the right."
     ↓
POST /api/chat                         → 202 { job_id }
     ↓
queued → claimed → running             (polled)
     ↓
succeeded → preview.url                → <img> updates
```

The user never sees Blender, Python, a terminal, a job identifier, or the word
"worker".

## Structure

```
apps/web/
├── app/
│   ├── layout.tsx          root layout, fonts, metadata
│   ├── page.tsx            the studio (one route: Spec 001 has one project)
│   └── globals.css         design tokens + resets
├── components/
│   ├── StudioShell.tsx     owns the session, passes plain data down
│   ├── ChatPanel.tsx       transcript + aria-live announcements
│   ├── MessageInput.tsx    labelled textarea, Enter to send
│   ├── PreviewPanel.tsx    image with empty/loading/stale/unavailable states
│   ├── StatusBar.tsx       project · change · preview
│   ├── ConnectionIndicator.tsx
│   └── *.module.css        CSS modules, no UI framework
├── hooks/
│   └── useDesignSession.ts binds reducer + client + job updates; owns cleanup
├── lib/
│   ├── api/                the ONLY place that performs HTTP
│   │   ├── client.ts       typed methods, timeouts, error parsing
│   │   ├── errors.ts       backend codes → human messages
│   │   ├── types.ts        API view models
│   │   └── index.ts        the boundary components import
│   ├── session/
│   │   ├── reducer.ts      pure (state, event) => state
│   │   ├── jobUpdates.ts   JobUpdateSource + polling implementation
│   │   └── types.ts        session vocabulary + status wording
│   ├── config.ts           browser-safe configuration
│   └── ids.ts              crypto.randomUUID-based request/session ids
└── tests/                  node --test
```

Two deliberate shapes:

**All HTTP lives in `lib/api`.** Components receive typed values or an `ApiFailure`;
they never see a `Response`, a status code, or an error body. An API change has one
place to land.

**The session is a pure reducer.** The interesting decisions in this UI are its state
transitions — when to keep the old preview, when a retry may reuse a `request_id`,
when a missing preview is a warning rather than a failure. Those live in
`lib/session/reducer.ts` as data transformations and are tested without a DOM.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `http://127.0.0.1:8000` | where the control plane is listening |

```bash
cp .env.local.example .env.local
```

`NEXT_PUBLIC_*` variables are compiled into the **browser bundle** and are therefore
public. No secret may ever go here: the worker token, Blender paths, journal and
recovery locations, and API credentials all stay server-side. The UI needs exactly
one value — where to send requests.

## API client

```ts
const client = createApiClient();            // or { baseUrl, fetchImpl, timeoutMs }

client.getHealth():        Promise<HealthView>
client.getWorkers():       Promise<WorkersView>
client.submitChat(body):   Promise<ChatSubmission>
client.getJobStatus(projectId, jobId): Promise<JobStatusView>
client.getLatestPreview(projectId):    Promise<PreviewView | null>   // null = none yet
client.getArtifactUrl(relativeUrl):    string                        // absolute
```

`fetch` and the base URL are injected, so tests exercise the real client against a
stub transport rather than mocking a global.

Every request is bounded by a timeout. Failures become an `ApiFailure` with a
`kind` (`offline` · `timeout` · `no_worker` · `unsupported` · `invalid` ·
`not_found` · `conflict` · `malformed` · `unknown`), the canonical backend `code`,
and a message written for a designer.

`getLatestPreview` returns `null` for 404 on purpose: "nothing rendered yet" is a
normal starting condition, not an error to show someone.

### Types

Anything shared with the backend is imported from `@studio/types`, so the browser
cannot drift from the canonical contracts:

```ts
import type { ArtifactType, JobStatus, Vec3 } from "@studio/types";
```

The API's HTTP responses are *not* the canonical contracts, and are defined
explicitly in `lib/api/types.ts` as view models. Two examples of why they differ:

- `ChatSubmission` carries a `job_id`; the canonical `ChatResponse` describes the
  final answer about a change and closes its object, so it cannot.
- `PreviewView` adds `url`; the canonical `PreviewArtifact` deliberately has none,
  because a worker must not know the control plane's route shape.

## Request / job / preview lifecycle

1. **Submit.** A new user message gets a fresh `request_id`
   (`crypto.randomUUID`). `session_id` is generated once per browser session and
   stays fixed. The user's message appears immediately.
2. **Accept.** `POST /api/chat` returns `202` with a `job_id` and `status_url`. The
   transcript shows *"Change understood. Sending to the design machine…"*.
3. **Follow.** `GET /api/projects/{project_id}/jobs/{job_id}` is polled every 750 ms
   through `JobUpdateSource`. Progress lines replace one another rather than piling
   up.
4. **Settle.** Polling stops at `succeeded` or `failed`. Nothing is scheduled
   afterwards, and unmounting stops the subscription.
5. **Show.** On success, `preview.url` is joined onto the API base and set as the
   image `src`.

Guarantees the polling implementation makes, each a bug people actually ship:

- **Stops at terminal.** No request after `succeeded`/`failed`.
- **No overlapping loops.** One request in flight; the next is scheduled only after
  the previous settles. A fixed `setInterval` would stack requests on a slow API.
- **Cancellable.** `stop()` prevents any further callback, so an unmounted component
  cannot dispatch into a dead tree or keep hitting the API in a forgotten tab.
- **Bounded.** A job that never terminates times out instead of polling forever.
- **Transient-tolerant.** A network blip is retried; a definite answer (404) ends it.

### Replacing polling later

`JobUpdateSource` is the seam:

```
useDesignSession → JobUpdateSource
                    ├── createPollingJobUpdates   (Spec 001)
                    └── (later) SSE / WebSocket
```

Server-pushed updates mean writing one more implementation of that interface. The
hook, the reducer, and every component stay untouched, because none of them know how
an update arrived.

### Identifiers

`request_id` is the **mutation identity**: the backend derives `idempotency_key` from
`(project_id, request_id, operation_index)`. So

- a **new** user message gets a **new** id — the change applies again, correctly;
- an **explicit retry** of an uncertain submission reuses the **original** id — the
  backend resolves it to the existing change instead of applying it twice.

`crypto.randomUUID()` is the source. `Math.random()` is not acceptable here: a
collision would make two different commands look like the same mutation.

## Preview loading

| State | What is shown |
|---|---|
| Nothing rendered yet | *"No preview yet. Send a design instruction to begin."* |
| Loading | the image, plus a *"Loading preview…"* caption |
| A change is in flight | the **previous** image, dimmed, with *"Updating preview…"* |
| Available | the current image, aspect ratio preserved |
| Change saved, no image | the change is reported as applied, with a warning |
| Image fails to load | *"The preview image could not be loaded. The change itself was saved."* |

The previous image is kept on screen during a change rather than blanking the panel,
because flashing empty makes the app feel like it is losing the user's work.

A plain `<img>` is used rather than `next/image`: the source is a runtime API URL on
another origin, so none of the optimisation `next/image` provides applies.

## Error states

| Condition | What the user reads |
|---|---|
| API unreachable | *"Can't reach the design studio service. Check that it is running…"* |
| Request timeout | *"The request took too long and was stopped. Nothing was changed."* |
| No worker (503 `BLENDER_UNAVAILABLE`) | *"The design machine is currently unavailable, so nothing was changed."* |
| Unsupported instruction (422) | *"That instruction isn't supported yet. Try something like 'Move Cube 50 cm to the right.'"* |
| Bad units (`INVALID_UNITS`) | *"That measurement wasn't understood. Use centimetres or metres…"* |
| Object missing (`OBJECT_NOT_FOUND`) | *"That object isn't in this project, so nothing was changed."* |
| Job failed | the mapped message for its canonical code |
| Preview generation failed | change reported as **applied**, with a warning |
| Artifact unavailable | *"The preview image could not be loaded. The change itself was saved."* |
| Malformed response | *"The service sent an unexpected response."* |
| Unknown code | a generic message; the raw code is never rendered |

No stack trace, no HTTP status, no backend wording, and no filesystem path reaches
the screen. Most messages state that nothing was changed — the thing a designer most
needs to know after a failure.

### Connection indicator

Two independent lights, because the facts are independent: the control plane can be
perfectly healthy while no design machine is connected, in which case commands will
fail. `Design machine` is derived from `ready_workers` and
`blender_capable_workers`, never from the API answering 200.

## Running locally

Three processes. See the root `README.md` for the full copy-paste sequence.

```bash
# once, to create a project for the worker to open
python scripts/bootstrap_local_project.py

# terminal 1 — control plane
source .venv/bin/activate
export STUDIO_WORKER_TOKEN=local-dev-token
uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000

# terminal 2 — Blender worker
source .venv/bin/activate
export STUDIO_WORKER_ID=worker_local_1
export STUDIO_WORKER_TOKEN=local-dev-token
export STUDIO_CONTROL_PLANE_URL=ws://127.0.0.1:8000/ws/workers
python -m blender_worker

# terminal 3 — web app
cd apps/web
npm install
npm run dev
```

Then open **http://localhost:3000**.

CORS: the API allows `http://localhost:3000` and `http://127.0.0.1:3000` by default
in the `local` environment — an explicit two-entry allow-list, never a wildcard, and
no default at all outside local.

## Manual acceptance scenario

1. Run `python scripts/bootstrap_local_project.py` (resets the project to Cube X = 0).
2. Start the API (terminal 1). It prints `Uvicorn running on http://127.0.0.1:8000`.
3. Start the worker (terminal 2). It prints `registered; waiting for design changes`.
4. Start the web app (terminal 3) and open http://localhost:3000.
5. **Confirm both indicators are green**: `Service` and `Design machine`.
6. Type `Move Cube 50 cm to the right.`
7. Click **Send** (or press Enter).
8. **Observe** the status change through *Sending → picked up → Applying → Done*, and
   the status bar reach `Succeeded`.
9. **See the preview appear**, showing the cube moved to the right.
10. Send the same sentence again: the cube moves a further 50 cm and a **new**
    preview appears, while the previous one remains valid.

At no point is a terminal, a Blender window, or any knowledge of MCP, Python, or job
architecture required from the user.

## Testing

```bash
cd apps/web
npm test        # 71 tests: node --test
```

Split by what each layer actually needs:

- **`apiClient.test.ts`** — the real client against a stub transport: request bodies,
  error translation, URL building, path encoding.
- **`session.test.ts`** — the reducer and the polling loop, with a manual clock.
- **`ui.test.ts`** — the real `StudioShell` in jsdom: layout, send gating, progress
  display, image `src`, the degraded-preview case, failures.

The DOM tests need `tsx` (JSX) and a 40-line CSS-module hook (`tests/hooks.mjs`);
everything that does not need a DOM is tested without one. That keeps the whole
repository on `node --test` instead of introducing a second runner and config.

An API-backed contract test lives at `tests/e2e/test_web_api_contract.py`: it runs
**this client** against a real FastAPI, a real worker, and real Blender, so a drift
between the API's actual responses and the shapes the UI is built on fails there.

```bash
pytest -m blender tests/e2e/test_web_api_contract.py
```

There is deliberately **no browser-automation test**. Playwright plus a browser
download would verify the same HTTP conversation that test already verifies, plus
rendering the component tests already cover. What remains genuinely un-automated —
that the assembled page looks and feels right — is the manual scenario above, and is
labelled manual rather than claimed as automated.

## Spec 001 limitations

Known and scoped, not oversights:

| Limitation | Until |
|---|---|
| One hardcoded project, no picker or creation flow | multi-project task |
| No authentication; the API resolves a fixed development user | authentication task |
| Still-image preview only; no Three.js, no interactive scene, no object selection | interactive-preview task |
| Polling, not server-pushed updates | SSE/WebSocket task (behind `JobUpdateSource`) |
| One mutation at a time; sending is disabled while a change runs | deliberate — concurrent mutations would race on one project |
| No conversation history across reloads; the transcript is in memory | project memory task |
| Only the narrow move grammar is understood | Astra/Codex provider task |
| No render controls, no quality settings, no final Cycles render | final-render task |
| No voice, annotations, or drag-and-drop editing | later milestones |
| Basic responsiveness only (two-panel desktop, stacked narrow) | design-system task |
| Session id is per page load, not persisted | session task |

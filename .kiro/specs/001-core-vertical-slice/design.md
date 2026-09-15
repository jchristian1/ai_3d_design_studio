# Design — 001 Core Vertical Slice

## Overview

This design implements the first vertical slice: a natural-language object move that
travels through every logical boundary of the system and returns an updated preview.

The design honors the steering rules:

- Logical boundaries exist from the start, even if colocated in one process (`structure.md`).
- Meters are canonical (`blender.md`).
- The agent is reached only via `AgentProvider` (`agent-context.md`).
- MCP exposes focused semantic tools, not arbitrary Python (`security.md`, `tech.md`).
- Blender is never publicly exposed; the worker connects outbound (`security.md`).
- Operations verify resulting state, not just absence of errors (`testing.md`).

## Architecture

```
Browser (Next.js / React / Three.js)
   │  POST /projects/{id}/chat   { message, session_id }
   ▼
services/api (FastAPI)  ── validates contract, creates job (project-scoped)
   │  AgentProvider.handle(context)
   ▼
services/agent  ── ContextBuilder → provider reasoning → MCP tool selection
   │  MCP: move_object(target="Cube", delta_x=0.50)
   ▼
services/blender-mcp  ── semantic tool → worker command
   │  job dispatched over outbound WSS
   ▼
services/blender-worker  ── lock → recovery point → execute → verify → save → preview
   │  Blender (bpy)
   ▼
services/preview  ── viewport screenshot / GLB → object storage
   │
   ▼
API returns { status, object_position, preview_url } → Browser renders result
```

### Boundary strategy for the slice

For the first slice, `agent`, `blender-mcp`, and `blender-worker` MAY run in a single
process/container to reduce moving parts, BUT each SHALL be a separate module behind a
typed interface so they can be split later without changing callers. The API↔worker link
is modeled as an outbound worker connection even if implemented in-process via an
interface, to keep the security contract intact.

## Components and Interfaces

### 1. Canonical contracts — `packages/contracts/schemas` (language-neutral)

Cross-service contracts are defined once, in language-neutral JSON Schema. Neither
TypeScript nor Python is the source of truth:

```
packages/contracts/schemas/*.schema.json      <-- CANONICAL
                │
                ├──> TypeScript representation   packages/*/src
                └──> Python representation       packages/*/python
```

Canonical schemas: `vec3`, `job`, `chat-request`, `chat-response`, `error-response`,
`error-code`. Conformance tests in both languages load these files at runtime and verify
enum parity, field parity, round-trip wire validity, and agreement between the runtime
validation rules and the declared contract. A shared corpus
(`schemas/conformance-cases.json`) plus a cross-language verdict-parity test make
one-sided contract changes fail loudly. See `schemas/README.md`.

No code-generation framework is used at this milestone; the schemas are codegen-ready if
the contract surface grows.

```ts
// TypeScript representation of chat-request.schema.json / chat-response.schema.json
type ChatRequest = {
  project_id: string;
  session_id: string;
  message: string;
  selected_object_id?: string;
};

type ChatResponse = {
  status: "success" | "error";
  summary: string;               // human-readable result
  object_position?: Vec3;        // meters, for verification/UI
  preview_url?: string;
  error?: { code: ErrorCode; message: string };
};

type Vec3 = { x: number; y: number; z: number }; // meters
```

### 2. API control plane — `services/api` (FastAPI)

- `POST /projects/{project_id}/chat` — validates `ChatRequest`, ensures `project_id`
  is present and authorized, creates a `Job` bound to the project, invokes `AgentProvider`.
- Rejects missing/invalid fields with `422`/structured error.
- Returns `ChatResponse`.

Job model (persisted per `architecture.md`):

```
Job { id, project_id, session_id, type, status, created_at, result }
status ∈ { queued, running, succeeded, failed }
```

### 3. Agent orchestration — `services/agent`

```python
class AgentProvider(Protocol):
    def handle(self, context: AgentContext) -> AgentResult: ...

class AgentContext:  # built by ContextBuilder (agent-context.md order)
    system_rules; project_brief; constraints; decisions
    scene_snapshot; references; session_summary; recent_messages
    selected_object; user_request

class AgentResult:
    status; summary; tool_calls; object_position; error
```

- `ContextBuilder` assembles context in the documented order.
- The default `AstraProvider` (or a deterministic `RuleBasedProvider` for the slice/tests)
  maps the phrase to a `move_object` tool call. A rule-based provider keeps the E2E test
  deterministic and provider-agnostic while the real provider is wired in.
- **Interpretation rules for the slice:**
  - Distance parsing: `"50 cm" → 0.50 m`, `"40 cm" → 0.40 m`.
  - Direction: `right → +X`, `left → −X`, `up → +Z`, `down → −Z`,
    `forward → +Y`, `back → −Y`. (Documented convention; revisit with camera-relative later.)

### 4. MCP semantic tools — `services/blender-mcp`

`move_object` tool:

```
move_object(
  object_ref: { name?: str, object_id?: str },
  delta_x_m: float = 0.0,
  delta_y_m: float = 0.0,
  delta_z_m: float = 0.0,
) -> { object_id, name, position_m: Vec3 }
```

- Validates object exists and deltas are finite meters.
- Returns resulting position for verification.
- No `execute_python` in the normal interface. Any dangerous dev tooling is isolated and
  disabled in production (per `security.md`).

### 5. Blender Worker — `services/blender-worker`

Executes the `blender.md` modification workflow:

```
1. identify project
2. acquire project lock            (Redis lock; conflict → error)
3. create recovery point           (copy .blend or incremental save)
4. validate operation
5. execute (bpy: obj.location.x += 0.50)
6. inspect resulting state         (read obj.location back)
7. save (.blend)
8. update scene snapshot
9. generate preview
10. release lock                   (always, incl. failure)
```

- Connects outbound (WSS) to the control plane; no inbound Blender ports.
- Verification: assert `abs(new_x - (old_x + 0.50)) < 1e-4` before reporting success.

### 6. Preview — `services/preview`

- For this slice: viewport screenshot (PNG) written to object storage; return `preview_url`.
- GLB/interactive preview deferred (later step in `blender.md` progression).

## Data Models

```
Project { id, name, blend_path, scene_snapshot }
Session { id, project_id, user_id, provider, summary }
Job     { id, project_id, session_id, type, status, result }
SceneObject { object_id, display_name, blender_name, position_m }
```

Object identity (per `blender.md`): stable IDs, e.g.
`object_id=obj_8d83f`, `display_name="Cube"`, `blender_name="Cube_obj_8d83f"`.
For the seed test project, `Cube` maps directly to keep the slice simple.

## Error Handling

| Case | Detected by | Response |
|------|-------------|----------|
| Missing `project_id` | API validation | 422 structured error |
| Object not found | MCP validation | `error.code = OBJECT_NOT_FOUND` |
| Invalid units / non-finite delta | MCP validation | `error.code = INVALID_UNITS` |
| Lock conflict | Worker | `error.code = LOCK_CONFLICT`, no mutation |
| Blender unavailable | Worker | `error.code = BLENDER_UNAVAILABLE` |
| Verification mismatch | Worker | rollback to recovery point, `error.code = VERIFY_FAILED` |

All failures release the lock and leave the `.blend` uncorrupted.

## Testing Strategy

Per `testing.md`:

- **Unit**: unit conversion (cm→m), direction→axis mapping, contract validation,
  job state transitions, provider adapter selection.
- **MCP**: given Cube X=0, `move_object(delta_x=0.50)` ⇒ Cube X=0.50.
- **Worker**: lock acquire/release, recovery point creation, autosave, failure reporting,
  lock-conflict path.
- **Integration**: API → job → worker → MCP → Blender.
- **E2E (mandatory)**: open test project → "Move Cube 50 cm to the right" → assert
  Cube X = 0.50, preview updated, browser response = success. Plus failure-case simulation
  (Blender unavailable, lock conflict, invalid object) reporting structured errors without
  corruption.

## Open Questions / Deferred

- Real `AstraProvider` wiring vs. deterministic provider for CI (design allows both).
- GLB interactive preview and browser object selection (later slices).
- Multi-worker coordination and GPU render path (later).

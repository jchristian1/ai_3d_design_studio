# Design — 002 Intelligent Scene Agent

Spec 001 is the baseline. This document describes only what changes and what is added.
Where a Spec 001 boundary works, it is reused as-is and named rather than restated.

Tasks 1 and 2 are **complete and retained**. They are the reason this refactor is cheap:
the canonical scene contracts, the `studio-scene-v1` digest, and the deterministic
metres / radians / sizing / colour utilities are all platform-owned and
implementation-independent. Nothing below replaces them.

## The two governing ideas

> **1. The model gains language ability, not authority.**
>
> **2. We do not reinvent Blender MCP.**

The first is unchanged from the original design. The second is this refactor: an
existing Blender MCP implementation becomes the Blender **capability engine**, behind a
platform-owned adapter, reached over the real Model Context Protocol.

```
UNTRUSTED                          TRUSTED (ours)                    EXTERNAL
────────────────────────────       ──────────────────────────        ──────────────
user message                  ─┐
SceneSnapshot (safe, read-only)├─→ ContextBuilder ─→ LLM ─→ Proposal
conversation / clarification  ─┘                              │
                                                              ▼
                                         schema validation + object resolution
                                         + capability policy + unit conversion
                                                              │
                                              AgentOutcome ───┤
                                                              ▼
                                         JobFactory → canonical Job → worker
                                                              │
                                                 durable guard (scene_version,
                                                 expected-before, desired-after)
                                                              │
                                                  BlenderMcpGateway
                                                              │  MCP (stdio)
                                                              ▼
                                                    external Blender MCP server
                                                              │  loopback TCP
                                                              ▼
                                                    Blender add-on → Blender
```

---

## 1. Who owns what

This table is the point of the refactor. If a future task starts writing something in
the right-hand column, it is duplicating work.

| Concern | Our platform | Existing Blender MCP |
|---|---|---|
| Natural-language conversation | **ours** | — |
| LLM provider selection and abstraction | **ours** | — |
| Prompt/context construction | **ours** | — |
| Scene grounding as a canonical contract | **ours** (`SceneSnapshot`) | supplies raw scene/object data |
| `scene_version` identity and preconditions | **ours** (`studio-scene-v1`) | — |
| Clarification and ambiguity handling | **ours** | — |
| Object identity (`studio_object_id`) and resolution | **ours** | supplies Blender names |
| Canonical units, angles, colour | **ours** (`packages/spatial`) | consumes converted values |
| Proposal validation and capability policy | **ours** (`McpToolPolicy`) | — |
| Job durability, retry, idempotency | **ours** (journal) | — |
| Project locks, recovery points, project isolation | **ours** | — |
| Cloud worker connectivity (outbound, authenticated) | **ours** | — |
| Browser UX, transcript, preview presentation | **ours** | — |
| **Blender object manipulation implementation** | — | **existing MCP** |
| **Object creation / geometry** | — | **existing MCP** |
| **Materials implementation** | — | **existing MCP** |
| **Rendering / viewport screenshot** | — | **existing MCP** |
| **Asset integrations** (Poly Haven, Sketchfab, Poly Pizza, Hyper3D, Hunyuan3D) | — | **existing MCP**, Tier B |
| **Export (GLB/FBX)** | — | **existing MCP**, Tier B |
| **Blender API / node schema lookup** | — | **existing MCP** |
| **Arbitrary Python in Blender** | **our policy DENIES by default** | existing MCP exposes it |

---

## 2. The selected implementation, pinned

| | |
|---|---|
| Implementation | `ahujasid/blender-mcp` ("MCP for Blender") |
| Distribution | PyPI package `blender-mcp` |
| **Pinned version** | **`blender-mcp==1.9.4`** (published 2026-09-15) |
| **Pinned commit** | `7684c6b3ad2aa0710bbdb1cb06b497c90899ae00` |
| Licence | MIT |
| Its dependencies | `mcp<2,>=1.9.0`, `httpx>=0.27.0`; requires Python ≥ 3.10 |
| Add-on | `addon.py`, `bl_info` version `(1, 7)`, installed by `uvx blender-mcp install-addon` from the SAME pinned package |
| Add-on↔server handshake | `ADDON_PROTOCOL_VERSION` / `EXPECTED_ADDON_PROTOCOL_VERSION`, reported by `get_addon_status` |
| Transport, client→server | MCP over stdio (`uvx blender-mcp`) |
| Transport, server→Blender | plain JSON over TCP to the add-on, default `localhost:9876`, configurable via `BLENDER_HOST` / `BLENDER_PORT` or `--port` |
| Upstream safe mode | `BLENDER_MCP_SAFE_MODE=1` — validates scripts before execution |
| Upstream telemetry | **ON by default**; disabled with `DISABLE_TELEMETRY=true` |

Pinning is possible and required in both dimensions: the version pins the SERVER, and
because the add-on ships inside the same package, `uvx blender-mcp==1.9.4 install-addon`
pins the ADD-ON too. Those two must agree — the server checks the add-on's protocol
version at startup — so they are always pinned as a pair.

### 2.1 Discovered tool inventory (31 tools, at 1.9.4)

Read from the pinned source. Task 3 re-derives this list at runtime via `tools/list`
and fails if it differs.

| Tool | Tier | Note |
|---|---|---|
| `get_scene_info` | **A** | the primary snapshot source |
| `get_object_info` | **A** | per-object detail |
| `get_viewport_screenshot` | **A** | needs a GUI viewport (see §2.3) |
| `describe_node_type` | **A** | schema lookup, creates a throwaway node tree |
| `bpy_api_lookup` | **A** | RNA/API reference lookup, read-only |
| `get_addon_status` | **A** | version handshake |
| `get_polyhaven_status`, `get_hyper3d_status`, `get_sketchfab_status`, `get_polypizza_status`, `get_hunyuan3d_status` | **A** | capability probes, no external fetch |
| `disable_telemetry` | **A** | used by us at startup, not offered to the model |
| `search_polyhaven_assets`, `get_polyhaven_categories`, `download_polyhaven_asset`, `set_texture` | **B** | remote asset service |
| `search_sketchfab_models`, `get_sketchfab_model_preview`, `download_sketchfab_model` | **B** | remote asset service, credentials |
| `search_polypizza_models`, `download_polypizza_model` | **B** | remote asset service, credentials |
| `generate_hyper3d_model_via_text`, `generate_hyper3d_model_via_images`, `poll_rodin_job_status`, `import_generated_asset` | **B** | external AI generation, credentials |
| `generate_hunyuan3d_model`, `poll_hunyuan_job_status`, `import_generated_asset_hunyuan` | **B** | external AI generation, credentials |
| `export_scene` | **B** | writes files outside the project |
| **`execute_blender_code`** | **C** | arbitrary Python in Blender |

### 2.2 THE DECISIVE FINDING: this upstream has no semantic mutation tools

At 1.9.4 there is **no** `create_object`, `modify_object`, `delete_object`,
`set_material`, `transform_object` or equivalent tool. Earlier versions had some; they
are gone. The README's "create, delete and modify shapes" and "apply or create
materials" capabilities are delivered **through `execute_blender_code`**.

The consequence has to be stated plainly, because it contradicts the naive form of this
refactor:

> If `execute_blender_code` is Tier C and denied by default, then adopting
> `ahujasid/blender-mcp` gives the platform **read, screenshot, asset download and
> export — and zero ability to move, rotate, resize or recolour anything.**

So "reuse the MCP for mutations, and deny arbitrary Python" is not simultaneously
satisfiable against this particular upstream. Three options exist. **This is an open
decision (§14, D1) and Task 3 exists to inform it — not to quietly pick one.**

**Option 1 — Hybrid: external MCP for reads and assets, platform-owned semantic
mutations.** Keep the Spec 001 `move_object` implementation and add the small remaining
transforms as platform capabilities; use the MCP for inspection, screenshots, node/API
lookup, asset import and export. Honest about scope, keeps Tier C denied, and the
mutation code we own is small and already proven. Cost: we still own a little bpy, which
is what the refactor wants to stop.

**Option 2 — Platform-generated code through `execute_blender_code`, never
model-authored.** Treat the tool as a private transport: the platform emits
*parameterised code from a fixed, reviewed template catalogue* (e.g. "set object X
rotation_euler to (0,0,0.785398)"), with values already validated and converted. The
model never sees the tool and never authors a character of it.
- Preserves "the model never authors code" and reuses upstream's execution path.
- But the templates ARE a Blender implementation living in our repo, so it only
  partly achieves the refactor's goal, and it re-enables the single most dangerous
  upstream tool for our own use — acceptable only with upstream safe mode ON,
  templates reviewed, and no interpolation of model-supplied strings.

**Option 3 — Select a different upstream that exposes semantic mutation tools.**
Candidates seen while researching: `djeada/blender-mcp-server` (~27 tools across
namespaces, explicit create/material/render/export), `glonorce/Blender_mcp` (~69 tools),
`RFingAdam/mcp-blender` (~218 tools). These would satisfy "Tier A covers ordinary
creation, transforms and materials" directly. Cost: less popular, less proven, and each
needs the same governance review from scratch. Per the instruction not to switch
silently, this is reported rather than adopted.

The gateway and capability registry are designed so that **all three options are
implementable behind the same interface**, which is why Task 4 does not depend on this
decision.

### 2.3 Integration risks found in the pinned source

Recorded now so no later task discovers them as surprises.

1. **Interactive Blender session, not headless subprocess.** The documented flow is:
   open Blender, enable the add-on, press `N`, click *Start MCP Server*. Spec 001's
   worker launches a fresh headless Blender per operation and passes it a `.blend` path.
   Adopting this MCP means a **long-lived Blender process** whose *currently open file*
   is the thing being operated on. Consequences: the worker must own opening the right
   project into that session, project isolation becomes a property of session state
   rather than of a path argument, and the project lock must serialise session use.
   Whether the add-on can be started reliably in a headless `--python` session is
   unverified and is a Task 3 spike item.
2. **`get_viewport_screenshot` needs a viewport.** Spec 001's deterministic Workbench
   preview does not; it renders offscreen. The preview pipeline therefore stays
   platform-owned for now, and the MCP screenshot is treated as an additional
   capability rather than a replacement.
3. **The add-on socket has no authentication.** Upstream says so directly: anyone who
   can reach the port can run Python in Blender. That is exactly why Requirement 11
   makes loopback binding mandatory and forbids exposing or tunnelling the port.
4. **Telemetry is ON by default** and collects prompts, generated code, screenshots and
   scene data, and may be used to train models. Every tool also takes a `user_prompt`
   parameter whose documented purpose is capturing the user's verbatim words. For a
   product holding users' private design work this is a governance blocker:
   `DISABLE_TELEMETRY=true` is mandatory (Requirement 15.2) and `user_prompt` is never
   populated with user content (Requirement 15.3).
5. **Blender version support** is documented as 3.0+; the development machine runs 5.2.
   Verified only by the spike.

---

## 3. Architecture

New and changed components are marked. Everything unmarked is Spec 001 or Task 1/2,
unchanged.

```
┌─ apps/web ───────────────────────────────────────────────────────────┐
│  StudioShell · ChatPanel · PreviewPanel                              │
│  lib/api · lib/session/reducer.ts   [CHANGED: outcome kinds]         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP
┌─ services/api ────────────────▼──────────────────────────────────────┐
│  routes/chat.py                                                      │
│  ChatService                  [CHANGED: outcome dispatch]            │
│  SceneContextService          [NEW]  snapshot fetch + cache          │
│  ContextBuilder               [NEW]  assembles provider context      │
│  ObjectResolver               [NEW]  proposal → resolved reference   │
│  ClarificationStore           [NEW]  short session-scoped state      │
│  PlanCoordinator              [NEW]  ordered multi-Job orchestration │
│  worker_link/{manager,gateway}                                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ canonical Job (capability-named)
┌─ services/agent ──────────────┴──────────────────────────────────────┐
│  provider.py            AgentProvider protocol (unchanged shape)     │
│  outcome.py             [NEW] AgentOutcome union                     │
│  proposal.py            [NEW] untrusted ProposedOperation model      │
│  capabilities.py        [NEW] platform capability catalogue          │
│  providers/rule_based.py         retained: deterministic tests       │
│  providers/<real>.py    [NEW] the real LLM adapter                   │
│  providers/fake_llm.py  [NEW] scripted provider for offline tests    │
└──────────────────────────────────────────────────────────────────────┘

┌─ services/blender-worker ────────────────────────────────────────────┐
│  WorkerExecutor         [CHANGED] read vs mutate dispatch            │
│  MutationGuard          [NEW] scene_version + expected/desired +     │
│                               already-applied detection (OURS)       │
│  mcp/gateway.py         [NEW] BlenderMcpGateway (protocol boundary)  │
│  mcp/policy.py          [NEW] McpToolPolicy (tiers, fail closed)     │
│  mcp/registry.py        [NEW] capability → tool(s) mapping           │
│  mcp/normalize.py       [NEW] MCP output → canonical SceneSnapshot   │
│  mcp/session.py         [NEW] MCP client lifecycle (stdio child)     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ MCP stdio
┌─ EXTERNAL (not in this repo) ─▼──────────────────────────────────────┐
│  blender-mcp==1.9.4 server  →  loopback TCP 127.0.0.1:9876           │
│                             →  MCP for Blender add-on  →  Blender    │
└──────────────────────────────────────────────────────────────────────┘

┌─ services/blender-mcp  [LEGACY, retained during migration] ──────────┐
│  tools/move_object.py · adapters/blender_scene.py                    │
│  Spec 001's own semantic implementation. Kept as FALLBACK and as a    │
│  TEST ORACLE until MCP-backed parity is proven (Requirement 18.4).    │
│  Retiring it is a separate, deliberate step.                          │
└──────────────────────────────────────────────────────────────────────┘
```

### Why the gateway lives in the worker

The MCP server is a local child process talking to a local Blender. Only the worker is
on that machine. The control plane never speaks MCP, and the agent never speaks MCP —
it speaks capabilities.

---

## 4. BlenderMcpGateway and the capability registry

```python
class BlenderMcpGateway(Protocol):
    def capabilities(self) -> tuple[Capability, ...]: ...
    def invoke(self, capability: Capability, arguments: Mapping) -> CapabilityResult: ...
    def health(self) -> GatewayHealth: ...
```

- `ExistingBlenderMcpGateway` — the implementation for `blender-mcp==1.9.4`. Speaks MCP
  over stdio using the official `mcp` client SDK. Does **not** import
  `blender_mcp.*` internals.
- `FakeBlenderMcpGateway` — records invocations and replays canned MCP responses, so
  every layer above is testable offline with no Blender and no MCP server. This is the
  load-bearing test seam, exactly as `FakeLlmProvider` is for the model.

### Capability registry

```
CAPABILITY                 → external tool(s) at 1.9.4          policy
inspect_scene              → get_scene_info (+ get_object_info…)  A
inspect_object             → get_object_info                      A
screenshot                 → get_viewport_screenshot              A (GUI-dependent)
describe_node_type         → describe_node_type                    A
api_lookup                 → bpy_api_lookup                        A
create_object              → (none at 1.9.4 — see §2.2)            A when available
transform_object           → (none at 1.9.4 — see §2.2)            A when available
set_material_color         → (none at 1.9.4 — see §2.2)            A when available
render                     → (none at 1.9.4)                       A when available
import_asset               → download_polyhaven_asset | download_sketchfab_model |
                             download_polypizza_model | import_generated_asset…   B
export                     → export_scene                          B
```

A capability with no mapping in the configured implementation is **unavailable**, and
the platform reports a structured capability-named failure rather than substituting
`execute_blender_code`. That is what makes §2.2's gap visible instead of silently routed
around.

The agent only ever sees capability names. `AgentProvider`, routes, `JobFactory`,
canonical Jobs and the browser never contain `get_scene_info` or any other upstream
name; a test asserts that.

---

## 5. McpToolPolicy

```
discovered tools ──→ McpToolPolicy ──→ permitted capability catalogue ──→ prompt
                          │
                          ├─ Tier A  allowed by default
                          ├─ Tier B  requires explicit platform configuration
                          ├─ Tier C  denied by default, never offered to a model
                          └─ unlisted → DENY (fail closed)
```

Properties, all test-enforced:

- **Discovery is not permission.** The policy is a platform-owned table; the MCP's
  `tools/list` is an input to it.
- **Fail closed.** A tool that appears in a future upstream version and is not
  classified is denied, and the compatibility test fails so a human classifies it.
- **Evaluated on our side.** The policy runs in the worker before any MCP call. It does
  not depend on the external server enforcing anything.
- **Upstream safe mode is defence-in-depth.** `BLENDER_MCP_SAFE_MODE=1` is enabled
  because it is free, but it never authorises anything: "safe mode is on, therefore
  arbitrary execution is safe" is explicitly not a decision this design makes. Upstream
  safe mode still permits file save, import/export and render operators, and is a
  static validator of code we would not be sending anyway.
- **Tier B needs a destination policy.** A model must never choose a filesystem
  destination or an external URL; import/export paths are platform-derived from
  `project_id`.

---

## 6. SceneSnapshot: same contract, new producer

The Task 1 contract is unchanged. What changes is where the data comes from.

```
get_scene_info  (+ get_object_info per object as needed)
      ↓  raw MCP JSON  — UNTRUSTED external integration data
mcp/normalize.py
      ↓  validate shape, coerce units, map identity, reject anything unexpected
canonical SceneObject[] + SceneUnits
      ↓
compute_scene_version()          ← the single Task 1 digest, unchanged
      ↓
SceneSnapshot  →  SceneContextService  →  ContextBuilder  →  provider
```

Normalisation rules:

- Raw MCP output is validated before a snapshot exists. A missing field, an unexpected
  type, or a non-finite number is a structured failure, not a defaulted value.
- Identity: the platform's `studio_object_id` remains authoritative. The adapter is the
  only place it is translated to and from a Blender object name.
- Units: metres and radians are asserted at the boundary; if the upstream reports
  something else, `packages/spatial` converts (Requirement 8.9).
- **Raw MCP output never reaches `AgentProvider`.** A test asserts the provider's
  context contains no upstream field names.
- Anything the upstream cannot report is ABSENT in the snapshot rather than guessed.
  `SceneObject` already distinguishes "no material" from "black", which is exactly the
  distinction a normaliser must not blur.

Known normalisation gaps to resolve in Task 5, from the pinned source: `get_scene_info`
returns a scene summary whose per-object detail is shallower than `SceneObject`
requires, so the adapter will likely need `get_object_info` per object — an N+1 read
pattern whose cost must be measured, and which is the reason `inspect_scene` may map to
a tool SEQUENCE rather than a single tool.

---

## 7. Mutation: our guard, their implementation

The guard is the part worth owning, and it is not a Blender implementation.

```
PlannedOperation (capability-named, canonical units, resolved stable id)
        ↓
acquire project lock                                     ← ours
        ↓
inspect authoritative state via gateway (read capability) ← theirs, normalised by us
        ↓
verify scene_version                                     ← ours (Requirement 2)
        ↓
already-applied? desired-after already present → done, DO NOT invoke  ← ours
        ↓
verify expected-before                                   ← ours
        ↓
persist intended operation + required version + expected/desired + recovery point ← ours
        ↓
invoke MUTATION capability through the gateway            ← THEIRS
        ↓
inspect authoritative state again                         ← theirs, normalised by us
        ↓
verify desired-after reached; compute resulting scene_version ← ours
        ↓
durable result, journal completion                        ← ours
```

**Why the guard must exist even though we are reusing an MCP.** An external MCP
mutation tool makes no retry-safety promise, and a crash between "tool returned" and
"we recorded it" is precisely the window Spec 001 was built to survive. The guard turns
a non-idempotent third-party call into an idempotent platform operation by *reading
before deciding* — the same reasoning as Spec 001, with the read now coming from the
gateway. The decision order (already-applied → scene version → expected-before) is
unchanged and is still the thing that stops a verbatim retry being rejected as stale.

Absolute desired-after targets remain mandatory for every mutating capability, so a
retry never re-applies a relative delta.

---

## 8. Units, angles, colour, sizing — unchanged (Task 2)

Canonical: metres, radians, unitless absolute scale, absolute dimensions in metres,
linear sRGB RGBA. One conversion site (`packages/spatial`), enforced by the source
guard, now also covering the MCP boundary: percentages and colour names never cross it.

`set_object_dimensions` remains the primary resize representation (design-space size
intent); `scale_object` remains for explicit transform-scale intent.

---

## 9. Object resolution, clarification, multi-operation — unchanged in substance

Ordered resolution rules (explicit stable id → exact name → case-insensitive name →
browser selection → clarification answer → several matches = clarification → none =
`OBJECT_NOT_FOUND`), the session-scoped `ClarificationStore`, and the sequential,
non-atomic, resumable multi-operation semantics with chained scene versions all carry
over from the pre-refactor design unchanged. The only difference is that a resolved
operation is dispatched to a capability rather than to a bespoke tool.

---

## 10. Failure semantics

| Failure | Outcome | Jobs | Project |
|---|---|---|---|
| Provider unreachable / unauthenticated | `AgentError` `PROVIDER_UNAVAILABLE` | none | untouched |
| Model output malformed / unknown capability | `AgentError` `VALIDATION_ERROR` | none | untouched |
| Capability denied by policy | `AgentError` `VALIDATION_ERROR`, refusal recorded | none | untouched |
| Capability unavailable in this implementation | structured capability-named failure | none | untouched |
| MCP server not running / add-on not connected | `BLENDER_UNAVAILABLE` | none | untouched |
| MCP add-on/server version mismatch | `BLENDER_UNAVAILABLE` at startup handshake | none | untouched |
| Raw MCP output fails normalisation | `VALIDATION_ERROR` | none | untouched |
| Ambiguous referent | `Clarification` | none | untouched |
| Scene changed since planning | `SCENE_VERSION_MISMATCH`, snapshot refreshed, ≤1 auto re-plan | that Job fails | untouched |
| Target object moved | `PRECONDITION_MISMATCH` | that Job fails | untouched |
| MCP mutation returned but verification failed | `VERIFY_FAILED`, recovery point preserved | that Job fails | consistent |
| Lock conflict (read or mutate) | `LOCK_CONFLICT` | that Job fails | untouched |
| Mutation fails mid-plan | per-operation status | earlier applied, later not attempted | consistent |
| Preview fails | job still `succeeded` + `preview_error` | — | mutation durable |

---

## 11. Security

### Threat model

| Threat | Control |
|---|---|
| Prompt injection → code execution | `execute_blender_code` is Tier C and never in the model's catalogue; model output is data validated against a capability schema and never forwarded to a code-execution tool |
| Model names a third-party tool directly | The catalogue contains platform capability names only; the adapter is the sole translator |
| Unknown upstream tool becomes reachable | Policy fails closed; compatibility test fails on inventory change |
| Model supplies a path or URL | No path/URL field exists in any proposal, plan, Job or capability argument; import/export destinations are platform-derived |
| Model overrides identity | `project_id`/`user_id`/`session_id` come from `TrustedIdentity`; the proposal schema has no identity fields |
| Model bypasses object resolution | Every reference resolved server-side against the snapshot |
| MCP add-on port reachable from off-machine | Loopback-only binding, config guard rejects non-loopback, never exposed or tunnelled, no browser access (Requirement 11) |
| Third party receives user design data | Telemetry disabled by default and verified; `user_prompt` never populated with user content |
| Tier B service exfiltrates or imports arbitrary content | Disabled unless configured; bounded providers; platform-chosen destinations |
| Raw MCP output poisons the agent | Normalised and validated first; provider never sees raw output |

### The revised Spec 001 invariant

Spec 001 asserted **"the workstation never listens"** and enforced it with an AST guard
over the worker package. Spec 002 must relax the letter of that rule, because the
external add-on legitimately opens a local socket, while keeping the property that
actually matters:

> **No Blender or MCP service on the workstation is externally reachable.**

Concretely: loopback binding only; never `0.0.0.0`; never a LAN or public address by
default; the add-on port is never exposed through the control plane, tunnelled, or
port-forwarded; the browser has no access to it; and the worker→control-plane connection
stays outbound and authenticated. The old AST guard is re-scoped from "no bind calls
anywhere" to "our code opens no listening socket, and the external MCP host must be
loopback" — a configuration guard plus a narrower code guard, replacing a rule that is
no longer literally true.

---

## 12. Testing architecture

Four tiers. **The default suite stays deterministic and offline** — no network, no API
key, no Blender, and no MCP server.

```
1. Unit / contract        pure logic, schema conformance, cross-language parity
                          (Task 1 + Task 2, unchanged)

2. Fake-gateway pipeline  FakeBlenderMcpGateway replays canned MCP responses and
                          FakeLlmProvider scripts proposals, so the WHOLE pipeline —
                          context → proposal → validation → resolution → policy →
                          plan → Job → guard → gateway → normalisation → outcome —
                          is tested with neither Blender nor MCP present.
                          Policy, fail-closed denial, idempotency and injection
                          resistance are all proven here.

3. Real MCP + real Blender  marked `mcp`, opt-in: a running pinned MCP server and a
                          real Blender session. Includes the compatibility test that
                          pins the tool inventory and schemas.

4. Live provider          marked separately, opt-in, credential-gated, and a REQUIRED
                          gate before Spec 002 is product-complete.
```

The Spec 001 Blender tier (`-m blender`) remains as-is while the legacy implementation
is retained as the migration oracle.

### Acceptance scenarios (capability-named, unchanged in intent)

| | Instruction | Expected |
|---|---|---|
| A | "What objects are in this scene?" | truthful `Answer` from a normalised snapshot; zero Jobs |
| B | "Move Cube 30 cm left." | verified −0.30 m; preview; success |
| C | "Rotate Cube 45 degrees around Z." | verified π/4 rad |
| D | "Make Cube 20% smaller." | absolute 1.6 m dimensions; verified |
| E | "Make Cube a warm beige." | verified base colour, visible in the preview |
| F | "Move Cube 20 cm right and make it beige." | ordered ops 0,1; chained versions; ONE browser reply |
| G | two chairs; "Move the chair right." | `Clarification`; zero Jobs |
| H | instruction demanding Python/shell/file access | refused; no Tier C call attempted |
| I | scene modified externally after the snapshot | `SCENE_VERSION_MISMATCH`; nothing mutated |
| J | a discovered-but-unclassified upstream tool | denied, and the compatibility test fails |

---

## 13. Migration strategy

Incremental, and explicitly not a rewrite (Requirement 18.4).

1. **Now:** Spec 001's `move_object` path and `services/blender-mcp` remain the working
   implementation. Nothing is deleted.
2. **Task 3:** integration spike against the pinned upstream. Read-only. Proves or
   disproves the architecture, and resolves §2.2's mutation question.
3. **Tasks 4–6:** gateway, policy, normalisation and guard land behind the abstraction,
   with the legacy path still serving mutations.
4. **Task 10:** MCP-backed mutation capabilities land where the chosen option provides
   them. The legacy implementation becomes the **test oracle**: the same operation is
   executed both ways and the resulting `scene_version` must agree.
5. **After parity:** retiring the duplicated platform implementation is a separate,
   deliberately reviewed step — not part of Spec 002 unless parity is proven early.

The Task 3 work already written in the working tree (a platform-owned `inspect_scene`,
its Blender script, the richer fixture, and the read-path guard) is **retained** under
this strategy: it is the read oracle the normalisation work in Task 5 is validated
against, and the fixture is needed either way.

---

## 14. Decisions requiring review

### Closed by review

- **Scene-version enforcement** — enforced in-lock precondition with chained
  per-operation requirements (§7, Requirement 2).
- **Digest projection** — explicit versioned allow-list `studio-scene-v1` (Task 1).
- **Resize representation** — `set_object_dimensions` with absolute metres primary.
- **Canonical colour** — linear sRGB RGBA, with the transfer function decoded.
- **Reuse over reinvention** — an existing Blender MCP is the capability engine.
- **Policy fails closed** — an unclassified tool is denied.
- **Loopback invariant** — replaces "never listens" with "not externally reachable".
- **Telemetry** — disabled by default, verified in configuration tests.
- **Read consistency** — a read holds the project lock.

### Open

**D1 — How mutations are performed, given §2.2.** `ahujasid/blender-mcp` 1.9.4 exposes
no semantic mutation tool; all modelling goes through `execute_blender_code`, which our
policy denies. Choose Option 1 (hybrid: keep our small mutation set), Option 2
(platform-generated templates through the code tool, never model-authored), or Option 3
(different upstream). **Task 3 gathers the evidence; this decision gates Task 10, not
Task 4.**

**D2 — Blender session model.** Interactive long-lived session with the add-on versus
Spec 001's headless subprocess-per-operation. Affects project isolation, lock scope,
crash recovery and how a project file is opened. Task 3 spike item.

**D3 — Which real provider** (Astra, Codex, or another `AgentProvider`-compatible
implementation) and **D4 — the structured-output mechanism**. Unchanged from before;
deferred to Task 9.

**D5 — Snapshot read cost.** If `inspect_scene` requires `get_object_info` per object,
a large scene costs N+1 MCP round trips. Measure in Task 3; decide caching and
summarisation in Task 5.

**D6 — File and reference ingestion in Spec 002 or Spec 003** (Requirement 16.4).
Task 12 decides explicitly.

**D7 — Preview ownership.** Spec 001's deterministic offscreen Workbench preview versus
the upstream's GUI-dependent `get_viewport_screenshot`. Current intent: keep ours, treat
theirs as an extra capability.

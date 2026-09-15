# Design — 002 Intelligent Scene Agent

Spec 001 is the baseline. This document describes only what changes and what is added.
Where a Spec 001 boundary works, it is reused as-is and named rather than restated.

Tasks 1 and 2 are **complete and retained**, and they are the reason this refactor is
cheap: the canonical scene contracts, the `studio-scene-v1` digest, and the deterministic
metres / radians / sizing / colour utilities are all platform-owned and
backend-independent. Nothing below replaces them.

## The three governing ideas

> **1. The model gains language ability, not authority.**
>
> **2. The official Blender Lab MCP is our Blender backend. We are not building a second
> MCP server.**
>
> **3. No model-authored code. Every character of Python that reaches Blender was written
> and reviewed by us.**

```
Browser
  → FastAPI control plane
    → AgentProvider                       (language only; proposes semantic operations)
      → authoritative scene context       (SceneSnapshot, scene_version)
        → validated semantic proposal     (schema + resolution + policy)
          → platform safety / policy / durability layer
             (locks · recovery points · journal · idempotency · verification)
            → BlenderCapabilityProvider   (platform-owned, backend-agnostic)
              → OfficialBlenderLabBackend (the only component that knows the official MCP)
                → official Blender MCP server  ⇐ MCP/stdio
                  → official Blender MCP add-on ⇐ loopback TCP
                    → Blender
```

Everything above `BlenderCapabilityProvider` is backend-agnostic. That line is where the
architecture's value is concentrated: it is what makes a future native-semantic-tool
upgrade (§7) a local change, and it is what keeps a community MCP a *possible provider*
rather than a rewrite.

---

## 1. Who owns what

If a future task starts writing something in the right-hand column, it is duplicating
work. If it starts writing something in the left-hand column *inside* the backend, it is
leaking the boundary.

| Concern | Our platform | Official Blender MCP |
|---|---|---|
| Natural-language conversation | **ours** | — |
| LLM provider selection and abstraction | **ours** | — |
| Prompt / context construction | **ours** | — |
| Scene grounding as a canonical contract | **ours** (`SceneSnapshot`) | supplies raw scene/object summaries |
| `scene_version` identity and preconditions | **ours** (`studio-scene-v1`) | — |
| Clarification and ambiguity handling | **ours** | — |
| Object identity (`studio_object_id`) and resolution | **ours** | addresses objects by Blender name |
| Canonical units, angles, colour | **ours** (`packages/spatial`) | consumes converted values |
| Proposal validation and capability policy | **ours** | — |
| **Semantic capability vocabulary** | **ours** | — (upstream has none — §2.3) |
| **Guarded execution templates** | **ours** (closed catalogue, §6) | executes what we send |
| Job durability, retry, idempotency | **ours** (journal) | — |
| Project locks, recovery points, project isolation, save | **ours** | — |
| Cloud worker connectivity (outbound, authenticated) | **ours** | — |
| Browser UX, transcript, preview presentation | **ours** | — |
| **Blender process integration and add-on** | — | **official** |
| **Code execution transport into Blender** | — | **official** |
| **Scene / object / blend-file summaries** | — | **official** |
| **Blender Python API + manual documentation search** | — | **official** |
| **Screenshots, thumbnail and viewport render to path** | — | **official**, Tier B |
| **Generic `execute_blender_code`** | **never offered to a model** | official exposes it |

---

## 2. The official implementation, as verified

### 2.1 Identity

| | |
|---|---|
| Project | **Blender MCP**, maintained by the **Blender Lab** |
| Documentation | `https://www.blender.org/lab/mcp-server/` |
| Canonical source | `https://projects.blender.org/lab/blender_mcp` |
| Read via | GitHub mirror `Wenri/blender_mcp`, branch `main`, commit **`4309a39646e644261624bfcd2bca669b343b7621`** (2026-08-06) — the identity actually inspected for this design |
| Components | a Blender **add-on** (`addon/blender_mcp_addon/`) and an **MCP server** (`mcp/blmcp/`) |
| Add-on | extension id `mcp`, name `MCP`, version `1.0.0`, maintainer `Blender Lab`, `blender_version_min = "5.1.0"` |
| Add-on permission | declares `network`: "Runs a local TCP socket server for MCP client communication" |
| MCP server | distribution name `blender-mcp`, version `1.0.0`, console entry point `blender-mcp`, `requires-python >= 3.10`, dependencies `docutils`, `mcp[cli]>=1.2.0`, `pyyaml` |
| Licence | `SPDX: GPL-3.0-or-later` (© Blender Authors) on both add-on and server sources |
| Transport, client→server | MCP over **stdio**; the MCP client launches the server process |
| Transport, server→Blender | NUL-delimited JSON over **TCP** to the add-on: `{"type": "execute", "code": …, "strict_json": …}` |
| Host / port | `BLENDER_MCP_HOST` (default `localhost`), `BLENDER_MCP_PORT` (default `9876`), socket timeout 300 s |
| Background variants | `*_for_cli` tools run `blender --background <blend_file> --python-expr …`; Blender binary from `BLENDER_PATH`, timeout 120 s |
| Documented install | add-on by drag-and-drop into Blender (repository, then add-on) or *Install from Disk*; MCP server from an `.mcpb` MCP Bundle on the release page, or from source |

> **Distribution-name collision — read before installing anything.** PyPI's
> `blender-mcp` is the **community** project `ahujasid/blender-mcp` (1.9.4), which is a
> different implementation. The official server declares the same distribution name
> `blender-mcp` but is distributed from Blender's own repository and release bundles.
> `pip install blender-mcp` therefore installs the **wrong** project. Task 3 step 1 must
> record precisely which artefact was installed and from where.

Because Blender's own Forgejo instance refused automated fetches during this design pass,
the inspected identity above is the **mirror** commit. Task 3 step 1 must reconcile it
with the canonical `projects.blender.org` identity and record whichever is authoritative
for the pin.

### 2.2 Tool inventory as verified (26 tools)

Derived from the upstream `readme_tools.rst` and `mcp/blmcp/tools/` at the commit above.
Task 3 re-derives this at runtime via `tools/list`; a difference must FAIL
(Requirement 9.10, 16.5).

| Tool | Tier | Purpose |
|---|---|---|
| `get_objects_summary` | **A** | scene collection hierarchy and its objects — primary snapshot source |
| `get_object_detail_summary` | **A** | per-object detail by `name`: type, transforms, parent/children, modifiers, constraints, materials, visibility, data-block, collections |
| `get_blendfile_summary_path_info` | **A** | blend-file path, save status, age, backups |
| `get_blendfile_summary_datablocks` | **A** | data-block counts, active workspace, render engine |
| `get_blendfile_summary_missing_files` | **A** | missing external references |
| `get_blendfile_summary_of_linked_libraries` | **A** | linked-library tree |
| `get_blendfile_summary_usage_guess` | **A** | heuristic use-case guess |
| the five `get_blendfile_summary_*_for_cli` variants | **A** | same, by opening a `blend_file` in background Blender |
| `get_python_api_docs`, `search_api_docs`, `search_manual_docs` | **A** | bundled API / manual reference |
| `get_screenshot_of_window_as_json` | **A** | window layout, active object, selection |
| `get_screenshot_of_area_as_image`, `get_screenshot_of_window_as_image` | **A** | PNG screenshots (require a GUI window) |
| `jump_to_tab_by_name`, `jump_to_tab_by_space_type`, `jump_to_view3d_object_by_name`, `jump_to_view3d_object_data_by_name` | **B** | mutate UI state |
| `render_thumbnail_to_path`, `render_viewport_to_path` | **B** | render to an `output_path` — platform-derived destination only |
| **`execute_blender_code`** | **C** | arbitrary Python in the connected Blender instance |
| **`execute_blender_code_for_cli`** | **C** | arbitrary Python in a background Blender opened on `blend_file` |

Both Tier C tools are annotated `destructiveHint=True` upstream.

### 2.3 The decisive finding: the official MCP has no semantic mutation tools

At the verified commit there is **no** `move`, `rotate`, `scale`, `set_material`,
`create_object`, `delete_object` or `duplicate` tool — and no namespaced equivalent. The
project describes itself as *"a natural language interface with Blender's Python API,
improving access to documentation, and allowing users to explore and understand complex
setups"*, and its tool surface matches that description exactly: **inspection,
documentation, screenshots, rendering, viewport navigation — plus generic code
execution.**

The consequence, stated plainly:

> Using the official MCP as our Blender backend means **every mutation is Python**. There
> is no third option in which we both mutate the scene and never send code.

This is precisely why Requirement 11 exists. The platform sends Python — but only Python
it wrote, from a closed reviewed catalogue, parameterised with validated canonical values.
The model never authors, edits, selects or sees a character of it.

### 2.4 The upstream's own execution model — which is the same template pattern

This is the most useful thing found in the source, because it means our template
mechanism is not a workaround; it is the upstream project's own convention:

- Every non-`execute_*` tool is a pair of modules: `<tool>.py` (the MCP-facing tool) and
  `<tool>_toolcode.py` (the Python that runs inside Blender). Modules ending in
  `_toolcode` are excluded from tool discovery.
- The tool loads its fixed tool-code text, appends a calling-convention footer, and
  substitutes a **single placeholder** (`__BLMCP_PARAMS__`) with `repr()` of a typed
  `Params` named tuple, then ships the result over the socket via `send_code(...)`.
- There is no interpolation of values into arbitrary positions in the source; parameters
  arrive as one Python literal in one place.

So the official server's read tools are *fixed reviewed templates parameterised with
typed values*. Our mutation capabilities adopt exactly that discipline, on our side of the
boundary. We do **not** copy upstream tool-code text into this repository (§14, licence).

### 2.5 The upstream's stated safety posture

Recorded verbatim in substance, because the design must not depend on it:

- The documentation warns that the MCP server executes LLM-generated code in Blender
  **without guards**, that data can be removed or sent remotely, and recommends running it
  on a VM or a system without sensitive information.
- The add-on ships `weak_sandbox.py`, whose own docstring says it *"isn't really a
  sandbox, more guidance that some things should not be done"* and that a motivated LLM or
  user can work around it. It blocks a small list — `sys.exit`, `wm.quit_blender`,
  `wm.read_factory_settings` and similar — chosen for "guaranteed to cause problems",
  explicitly not for security.

Therefore: **the upstream sandbox is defence-in-depth and never authorisation**
(Requirement 10.5). The reason our product can be safe on top of an unguarded execution
transport is that *no untrusted text ever reaches it* — not that the transport is
protected.

### 2.6 Integration facts that shape later tasks

1. **Two session models exist, and they behave differently.** The interactive path
   (`execute_blender_code`, all `get_*` tools) acts on the Blender instance's *currently
   open file*. The `*_for_cli` path opens a `blend_file` per call in
   `blender --background`. Spec 001's worker is a headless subprocess-per-operation model
   with an explicit `.blend` path, which maps onto `_for_cli`; the richer read tools and
   deferred responses are interactive-only. §16, D2.
2. **`_for_cli` discards changes by default.** `run_blender_cli` runs
   `--background <blend> --python-expr <wrapper>` and returns a `result` dict; it never
   saves. A mutation on that path only persists if our template saves deliberately — which
   is the platform-owned save of Requirement 13.8.
3. **`synced_blend_for_cli` can create a sibling file.** When a live Blender instance has
   the same file open with unsaved changes, it saves a numbered copy
   (`<base>_mcp_0001.blend`), yields that, and deletes it on exit. Spec 001 asserts "no
   sibling file appears next to the project", so this interaction must be measured
   (Task 3 step 15) rather than discovered later.
4. **Deferred responses are interactive-only.** Background mode requires synchronous
   completion; long operations must be bounded accordingly.
5. **Screenshots need a GUI window.** Spec 001's deterministic offscreen Workbench
   preview does not. The preview pipeline stays platform-owned; the official screenshot
   and render tools are additional capabilities. §16, D7.
6. **Blender version floor is 5.1.0.** The development machine runs 5.2, so it qualifies —
   verified in Task 3 step 2, recorded in `verification.md` (Requirement 16.6).
7. **The add-on's socket has no authentication.** Anyone who can reach the port can run
   Python in Blender. That is exactly why Requirement 12 makes loopback mandatory and
   forbids exposure, tunnelling, proxying and browser access.

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
│  WorkerExecutor              [CHANGED] read vs mutate dispatch       │
│  MutationGuard               [NEW] scene_version + expected/desired  │
│                                    + already-applied (OURS)          │
│  capability/provider.py      [NEW] BlenderCapabilityProvider (Protocol)│
│  capability/registry.py      [NEW] capability → native tool | template│
│  capability/policy.py        [NEW] tool policy, fail closed          │
│  capability/normalize.py     [NEW] backend output → SceneSnapshot    │
│  backends/official/session.py   [NEW] MCP stdio client lifecycle     │
│  backends/official/backend.py   [NEW] OfficialBlenderLabBackend      │
│  backends/official/templates/   [NEW] CLOSED template catalogue      │
│        move_object.py · rotate_object.py · set_object_dimensions.py  │
│        set_material_color.py · create_object.py · delete_object.py   │
│        duplicate_object.py · save_project.py · read_scene.py         │
│  backends/fake/backend.py       [NEW] FakeBlenderBackend (offline)   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ MCP stdio
┌─ EXTERNAL (never in this repo) ▼─────────────────────────────────────┐
│  official Blender MCP server  →  loopback TCP 127.0.0.1:9876         │
│                               →  official add-on  →  Blender 5.2     │
└──────────────────────────────────────────────────────────────────────┘

┌─ Spec 001 custom Blender path [ORACLE / FALLBACK] ───────────────────┐
│  services/blender-mcp/tools/move_object.py                           │
│  services/blender-mcp/adapters/blender_scene.py                      │
│  services/blender-worker/blender_ops.py                              │
│  Retained on `main` as the migration oracle and fallback until        │
│  official-backend parity is proven (Requirement 19.4). §15.           │
└──────────────────────────────────────────────────────────────────────┘
```

### Why the provider lives in the worker

The official MCP server is a local child process talking to a local Blender. Only the
worker is on that machine. The control plane never speaks MCP; the agent never speaks MCP
— it speaks capabilities.

---

## 4. BlenderCapabilityProvider

```python
class BlenderCapabilityProvider(Protocol):
    def capabilities(self) -> tuple[Capability, ...]: ...
    def invoke(self, capability: Capability, arguments: CapabilityArguments) -> CapabilityResult: ...
    def health(self) -> BackendHealth: ...
```

`CapabilityArguments` is a **typed canonical record**, never a free-form mapping: metres,
radians, unitless scale, linear sRGB RGBA, and validated `studio_object_id`s. A capability
argument type that could carry a path, a URL, a host, a tool name or a code string does not
exist (Requirement 15.3).

Implementations:

- **`OfficialBlenderLabBackend`** — the primary backend. Speaks MCP over stdio to the
  pinned official server using the official `mcp` client SDK (our own pinned dependency).
  Owns process lifecycle, timeouts, reconnect, tool discovery, identity translation, and
  the template catalogue. Does not import `blmcp.*`.
- **`FakeBlenderBackend`** — records invocations and replays canned backend responses, so
  every layer above is testable offline with no Blender and no MCP server. This is the
  load-bearing test seam, exactly as `FakeLlmProvider` is for the model.
- **`LegacyCustomBackend`** *(optional, migration only)* — wraps the retained Spec 001
  implementation so the same capability can be executed both ways for parity evidence
  (§15).

### Capability registry, as mapped against the verified inventory

| Capability | Official native tool | Template needed |
|---|---|---|
| `inspect_scene` | `get_objects_summary` (+ `get_object_detail_summary` per object) | possibly, to supply unit settings and world dimensions the summaries omit — Task 3 step 6 decides |
| `inspect_object` | `get_object_detail_summary` | no |
| `move_object` | *none* | **yes** |
| `rotate_object` | *none* | **yes** |
| `set_object_dimensions` | *none* | **yes** |
| `set_material_color` | *none* | **yes** |
| `create_object` | *none* | **yes** |
| `delete_object` | *none* | **yes** |
| `duplicate_object` | *none* | **yes** |
| `save_project` | *none* | **yes** (platform-owned save, Requirement 13.8) |
| `render` | `render_thumbnail_to_path` / `render_viewport_to_path` | no — Tier B, platform-derived path; Spec 001's deterministic preview remains the product preview |
| `screenshot` | `get_screenshot_of_*` | no — Tier B, GUI-dependent |
| `api_lookup` | `get_python_api_docs` / `search_api_docs` | no — developer-facing, not in the model catalogue |

A capability with neither a native mapping nor a catalogue entry is **unavailable**: the
platform returns a structured capability-named failure. It is never approximated by
handing anything to `execute_blender_code` outside the catalogue (Requirements 9.5, 11.8).

The agent only ever sees capability names. `AgentProvider`, routes, `JobFactory`, canonical
Jobs and the browser never contain `get_objects_summary`, `execute_blender_code`, or any
other upstream name; a test asserts that.

---

## 5. Capability policy

```
discovered tools ──→ policy table ──→ permitted capability catalogue ──→ prompt
                          │
                          ├─ Tier A  read-only, allowed
                          ├─ Tier B  side effects, requires configuration + bounded args
                          ├─ Tier C  code execution — NEVER offered to a model
                          └─ unlisted → DENY (fail closed)
```

Properties, all test-enforced:

- **Discovery is not permission.** `tools/list` is an input to a platform-owned table.
- **Fail closed.** A tool appearing in a future upstream version and not classified is
  denied, and the compatibility test fails so a human classifies it.
- **Evaluated on our side**, in the worker, before any MCP call. It does not depend on the
  server or add-on enforcing anything.
- **Tier C is reachable only by the template catalogue**, never by the model, never by the
  browser, never by an MCP response, and never with text that was not rendered from a
  fixed template (§6).
- **Tier B needs a destination policy.** `render_*_to_path` takes an `output_path`; it is
  derived from `project_id` by the platform. No model, user or MCP response may influence
  it.

---

## 6. Guarded execution templates — the mechanism

This section is the heart of Requirement 11. It exists because §2.3 leaves no alternative:
mutation through the official MCP is Python, so the question is not *whether* code is sent
but *who wrote it*.

### 6.1 Shape

Each template is a module in `backends/official/templates/` containing exactly two things:

```python
# move_object.py  — illustrative shape, not final code

class Params(NamedTuple):          # typed, validated, canonical
    object_name: str               # a validated identifier present in the snapshot
    x: float                       # metres
    y: float
    z: float

SOURCE = """\
import bpy
def main(p):
    obj = bpy.data.objects.get(p.object_name)
    if obj is None:
        return {"status": "object_not_found"}
    obj.location = (p.x, p.y, p.z)
    bpy.context.view_layer.update()
    return {"status": "ok",
            "location": tuple(obj.matrix_world.translation)}
"""                                # a FIXED literal, reviewed like production code
```

Rendering is a single, shared, audited function:

```
render(template, params) -> str
    assert type(params) is template.Params      # typed record, nothing else accepted
    validate(params)                            # ranges, finiteness, identifier existence
    return template.SOURCE + FOOTER.replace(PARAMS_PLACEHOLDER, repr(params))
```

That is the same convention the upstream project uses for its own tools (§2.4): fixed
source text, one placeholder, one `repr()` of a typed record. It is adopted deliberately
rather than invented.

### 6.2 Rules, each with a test

| Rule | Enforcement |
|---|---|
| Template source is a fixed literal, never assembled at runtime | AST guard: no f-string, no `%`, no `.format`, no `+` on source, no `join` producing source, in any template module |
| **No interpolation of object names or any value into Python source** | The only substitution site is the single parameter placeholder; a test renders with adversarial values (`"'; import os"`, embedded newlines, `__import__`) and asserts the rendered program's AST is *structurally identical* to the benign rendering, differing only in one literal |
| Parameters are structured and safely encoded | `repr()` of a `NamedTuple` of validated scalars and validated identifiers; a free-form string field that is not a validated identifier cannot exist |
| Objects are addressed by validated stable identity | Identifier must resolve in the authoritative SceneSnapshot before rendering (Requirements 7.9, 11.6) |
| No `eval`/`exec` of model text, no shell, no subprocess, no arbitrary import, no arbitrary path, no arbitrary URL, no package install | AST guard over template sources plus a rendered-source scan; a positive-control test proves the guard fires |
| The catalogue is **closed** | The registry enumerates templates; an unknown semantic capability returns a structured DENY, tested |
| No model output can select or reach a template | A test asserts model output flows only into validated `CapabilityArguments`, and that no code path passes provider text to `render` |
| Templates carry no safety semantics | Templates are narrow single-operation programs; locks, journals, recovery points, preconditions and verification live in `MutationGuard` (Requirement 13.7) |

### 6.3 What a template is *not*

- It is not a Blender abstraction layer. It performs one bounded operation and reports the
  state it observed.
- It is not where safety lives. `MutationGuard` decides *whether* to invoke and *whether it
  worked*.
- It is not a general scripting facility. There is no template that takes code, an
  expression, an operator name, an attribute path, or a path/URL.
- It is not upstream code. No upstream tool-code text is copied into this repository (§14).

---

## 7. Replacing templates with native tools later

The whole point of the boundary. When the official MCP gains native semantic tools — for
example `object.move`, `material.set_color`, `scene.inspect` — the upgrade is:

```
   before:  platform capability  →  guarded execution template  →  execute_blender_code
   after:   platform capability  →  official semantic MCP tool
```

The change is confined to two files inside the backend: the registry entry for that
capability, and (eventually) the deletion of its template module. **Nothing above
`BlenderCapabilityProvider` changes** — not the agent, not the proposal schema, not the
capability vocabulary, not the guard, not the Jobs, not the API, not the browser.

How that is *proved* rather than asserted (Requirement 17.5):

1. Capability tests are written against `BlenderCapabilityProvider`, not against templates.
2. A registry-level test executes one capability through two implementations — template and
   a simulated native tool — and asserts identical canonical `CapabilityResult` and
   identical resulting `scene_version`.
3. An AST test asserts no module above the backend imports a template, names a template, or
   contains Python-as-data.
4. The compatibility test records the native tools that exist today, so the day a semantic
   tool appears upstream, it fails and prompts the swap instead of the swap being missed.

The same mechanism is what makes a community MCP a *possible provider* (Requirement 9.12):
it would be another `BlenderCapabilityProvider` implementation, reviewed on its own merits,
with nothing above the boundary changing.

---

## 8. SceneSnapshot: same contract, new producer

The Task 1 contract is unchanged. What changes is where the data comes from.

```
get_objects_summary  (+ get_object_detail_summary per object,
                      + read template if unit/dimension data is missing)
      ↓  raw backend JSON — UNTRUSTED integration data
capability/normalize.py
      ↓  validate shape, coerce units, map identity, reject anything unexpected
canonical SceneObject[] + SceneUnits
      ↓
compute_scene_version()          ← the single Task 1 digest, unchanged
      ↓
SceneSnapshot  →  SceneContextService  →  ContextBuilder  →  provider
```

Normalisation rules:

- Raw backend output is validated before a snapshot exists. A missing field, an unexpected
  type, or a non-finite number is a structured failure, not a defaulted value.
- Identity: `studio_object_id` remains authoritative. The backend is the only place it is
  translated to and from a Blender object name.
- Units: metres and radians are asserted at the boundary; anything else is converted through
  `packages/spatial` (Requirement 8.9).
- **Raw backend output never reaches `AgentProvider`.** A test asserts the provider's context
  contains no upstream field names.
- Anything the backend cannot report is ABSENT rather than guessed. `SceneObject` already
  distinguishes "no material" from "black", which is exactly the distinction a normaliser
  must not blur.
- A backend value never becomes code, a structure-altering template parameter, or a
  permission decision (Requirement 15.7).

Known gaps to resolve in Task 5, from the verified upstream: `get_objects_summary` returns a
collection hierarchy whose per-object detail is shallower than `SceneObject` requires, so
per-object `get_object_detail_summary` calls are likely needed (an N+1 read pattern whose
cost must be measured), and scene unit configuration plus world-space dimensions may require
the read template. This is why `inspect_scene` may map to a tool *sequence*.

---

## 9. Mutation: our guard, their transport

```
PlannedOperation (capability-named, canonical units, resolved stable id)
        ↓
acquire project lock                                          ← ours
        ↓
inspect authoritative state via read capability               ← official tools, normalised by us
        ↓
verify scene_version                                          ← ours (Requirement 2)
        ↓
already-applied? desired-after already present → done, DO NOT invoke   ← ours
        ↓
verify expected-before                                        ← ours
        ↓
persist intent + required version + expected/desired + recovery point   ← ours
        ↓
invoke mutation capability                                     ← ours (template) via official transport
        ↓
save the project deliberately                                  ← ours (Requirement 13.8)
        ↓
inspect authoritative state again                              ← official tools, normalised by us
        ↓
verify desired-after reached; compute resulting scene_version   ← ours
        ↓
durable result, journal completion                             ← ours
```

**Why the guard must exist even though we adopted an MCP.** The official transport makes no
retry-safety promise, and a crash between "the code ran" and "we recorded it" is precisely
the window Spec 001 was built to survive. The guard turns a non-idempotent call into an
idempotent platform operation by *reading before deciding*. The decision order
(already-applied → scene version → expected-before) is unchanged and is still the thing
that stops a verbatim retry being rejected as stale.

Absolute desired-after targets remain mandatory for every mutating capability, so a retry
never re-applies a relative delta. `MutationGuard` contains no bpy, no geometry maths and
no template source — asserted by an AST test.

**Do not assume an operation worked because no exception occurred.** A template returning
`{"status": "ok"}` is evidence of nothing. The observed post-state, read back and verified,
is the only evidence that counts.

---

## 10. Units, angles, colour, sizing — unchanged (Task 2)

Canonical: metres, radians, unitless absolute scale, absolute dimensions in metres, linear
sRGB RGBA. One conversion site (`packages/spatial`), enforced by the source guard, now also
covering the backend boundary: percentages, degrees, direction tokens and colour names never
cross it, and never appear as template parameters.

`set_object_dimensions` remains the primary resize representation (design-space size intent);
scale-carrying transform intent remains available for explicit unitless scale.

---

## 11. Object resolution, clarification, multi-operation — unchanged in substance

Ordered resolution rules (explicit stable id → exact name → case-insensitive name → browser
selection → clarification answer → several matches = clarification → none =
`OBJECT_NOT_FOUND`), the session-scoped `ClarificationStore`, and the sequential,
non-atomic, resumable multi-operation semantics with chained scene versions all carry over
unchanged. The only difference is that a resolved operation is dispatched to a capability.

---

## 12. Failure semantics

| Failure | Outcome | Jobs | Project |
|---|---|---|---|
| Provider unreachable / unauthenticated | `AgentError` `PROVIDER_UNAVAILABLE` | none | untouched |
| Model output malformed / unknown capability | `AgentError` `VALIDATION_ERROR` | none | untouched |
| Capability denied by policy | `AgentError` `VALIDATION_ERROR`, refusal recorded | none | untouched |
| Capability has no native tool and no template entry | structured capability-named failure (DENY) | none | untouched |
| MCP server not running / add-on not connected | `BLENDER_UNAVAILABLE` | none | untouched |
| Add-on / server disagreement at startup | `BLENDER_UNAVAILABLE` before any capability is served | none | untouched |
| Raw backend output fails normalisation | `VALIDATION_ERROR` | none | untouched |
| Ambiguous referent | `Clarification` | none | untouched |
| Scene changed since planning | `SCENE_VERSION_MISMATCH`, snapshot refreshed, ≤1 auto re-plan | that Job fails | untouched |
| Target object moved | `PRECONDITION_MISMATCH` | that Job fails | untouched |
| Template ran but verification failed | `VERIFY_FAILED`, recovery point preserved | that Job fails | consistent |
| Save failed after a successful mutation | `VERIFY_FAILED`, not reported as success | that Job fails | recovery point preserved |
| Lock conflict (read or mutate) | `LOCK_CONFLICT` | that Job fails | untouched |
| Mutation fails mid-plan | per-operation status | earlier applied, later not attempted | consistent |
| Preview fails | job still `succeeded` + `preview_error` | — | mutation durable |

---

## 13. Security

### Threat model

| Threat | Control |
|---|---|
| Prompt injection → code execution | No code-execution capability is in the model's catalogue; model output is data validated into typed `CapabilityArguments`; only fixed templates render Python, and never from model text |
| Model smuggles Python through a parameter | Parameters are validated scalars and validated identifiers encoded as one Python literal; the adversarial-rendering test proves program structure cannot change |
| Model names a backend tool directly | The catalogue contains platform capability names only; the backend is the sole translator |
| Unknown upstream tool becomes reachable | Policy fails closed; compatibility test fails on inventory change |
| Model supplies a path or URL | No path/URL field exists in any proposal, plan, Job, capability argument or template parameter; render destinations are platform-derived |
| Model overrides identity | `project_id`/`user_id`/`session_id` come from `TrustedIdentity`; the proposal schema has no identity fields |
| Model bypasses object resolution | Every reference resolved server-side against the snapshot before rendering |
| MCP response influences execution | Backend output is normalised, validated data only; it can never become code, a structure-altering parameter, or a permission decision |
| Add-on port reachable from off-machine | Loopback-only, config guard rejects non-loopback, never exposed / tunnelled / proxied, no browser access (Requirement 12) |
| Wrong project modified | Every job carries `project_id`; the backend proves it is acting on that project before mutating (Requirement 13.9) |
| Upstream telemetry or off-machine transmission | Disabled by default and verified in configuration tests; no user content in third-party analytics parameters |
| Developer-only dangerous tooling leaks to production | Isolated from the product path, disabled by default, absence asserted in the production-safe configuration |

### The revised Spec 001 invariant

Spec 001 asserted **"the workstation never listens"** and enforced it with an AST guard over
the worker package. Spec 002 must relax the *letter* of that rule, because the official
add-on legitimately opens a local socket, while keeping the property that matters:

> **No Blender or MCP execution interface is externally reachable.**

Concretely: loopback binding only; never `0.0.0.0`, LAN or public; the add-on port is never
exposed through the control plane, tunnelled, proxied or port-forwarded; the browser has no
access to it; and the worker→control-plane connection stays outbound and authenticated. The
old AST guard is re-scoped from "no bind calls anywhere" to "our code opens no listening
socket, and the configured backend host must be loopback" — a configuration guard plus a
narrower code guard, replacing a rule that is no longer literally true.

---

## 14. Third-party governance

- **Licence.** Both the official add-on and the MCP server carry
  `SPDX: GPL-3.0-or-later` (© Blender Authors). The integration is therefore deliberately a
  **separate-process protocol integration**: we launch the pinned server as a child process
  and speak MCP over stdio. We do not import `blmcp.*`, do not vendor the add-on, and do not
  copy upstream tool-code text into this repository. Task 3 step 1 records the licence for
  the exact artefact installed and confirms compatibility.
- **Pinning.** Server build and add-on build are pinned as a pair, `latest` is never used, and
  the pin records source, commit/release, add-on build, Blender floor and licence
  (Requirement 16.1–16.2).
- **Upgrades are never blind.** discover → compatibility suite → safety tests → Blender
  acceptance → only then move the pin. A failure anywhere leaves the pin untouched. The
  verified Blender MCP version, the verified Blender version and the date are recorded in
  `verification.md` (Requirement 16.3–16.6).
- **Telemetry / off-machine transmission** is disabled by default and verified; no tool
  parameter is populated with the user's verbatim words for third-party purposes
  (Requirement 16.7–16.8).
- **Community implementations** are references only until explicitly reviewed
  (Requirement 9.12). Not integrated in Task 3.

---

## 15. Migration strategy

Incremental, and explicitly not a rewrite (Requirement 19.4).

1. **Now:** Spec 001's `move_object` path (`services/blender-mcp`, `blender_ops.py`) remains
   the working implementation on `main`. Nothing is deleted.
2. **Task 3:** the official-MCP spike. Proves or disproves that our safe, durable
   architecture can use the official MCP as its backend without giving the LLM unrestricted
   execution.
3. **Tasks 4–6:** provider, backend, policy, normalisation and guard land behind the
   boundary, with the legacy path still available.
4. **Task 10:** the guarded core modelling capabilities land. The legacy implementation
   becomes the **test oracle**: the same operation is executed both ways and the resulting
   `scene_version` must agree.
5. **After parity:** retiring the duplicated platform implementation is a separate,
   deliberately reviewed step (Requirement 19.5) — not part of Spec 002 unless parity is
   proven early.

**The superseded read work is preserved, not lost.** The platform-owned `inspect_scene`
implementation written for the *old* Task 3 (its MCP tool, Blender script, `SceneAdapter`
read methods, worker read path, `inspect-scene-result` contract, and the richer
`studio_scene` fixture, with 52 passing Blender tests) is committed on branch
**`wip/spec002-custom-blender-oracle`** at **`fde4115`**. `main` does not carry it. It is the
read oracle Task 5's normalisation is validated against, and the fixture work is needed
either way. It is **not** Task 3 progress: Task 3 is the spike below and remains `[ ]`.

---

## 16. Testing architecture

Four tiers. **The default suite stays deterministic and offline** — no network, no API key,
no Blender, no MCP server.

```
1. Unit / contract        pure logic, schema conformance, cross-language parity
                          (Task 1 + Task 2, unchanged)

2. Fake-backend pipeline  FakeBlenderBackend replays canned backend responses and
                          FakeLlmProvider scripts proposals, so the WHOLE pipeline —
                          context → proposal → validation → resolution → policy →
                          plan → Job → guard → capability → normalisation → outcome —
                          is tested with neither Blender nor MCP present.
                          Policy fail-closed, template-catalogue closure, adversarial
                          template rendering, idempotency and injection resistance are
                          all proven here.

3. Real official MCP      marked `mcp`, opt-in: the pinned official server and a real
   + real Blender         Blender 5.2. Includes the compatibility test that pins the tool
                          inventory and schemas, and the real-mutation verification.

4. Live provider          marked separately, opt-in, credential-gated, and a REQUIRED
                          gate before Spec 002 is product-complete.
```

The Spec 001 Blender tier (`-m blender`) remains as-is while the legacy implementation is
retained as the migration oracle.

### Acceptance scenarios (capability-named)

| | Instruction | Expected |
|---|---|---|
| A | "What objects are in this scene?" | truthful `Answer` from a normalised snapshot; zero Jobs |
| B | "Move Cube 30 cm left." | verified −0.30 m; saved; preview; success |
| C | "Rotate Cube 45 degrees around Z." | verified π/4 rad |
| D | "Make Cube 20% smaller." | absolute 1.6 m dimensions; verified |
| E | "Make Cube a warm beige." | verified base colour, visible in the preview |
| F | "Move Cube 20 cm right and make it beige." | ordered ops 0,1; chained versions; ONE browser reply |
| G | two chairs; "Move the chair right." | `Clarification`; zero Jobs |
| H | instruction demanding Python / shell / file access | refused; no code-execution call attempted |
| I | scene modified externally after the snapshot | `SCENE_VERSION_MISMATCH`; nothing mutated |
| J | a discovered-but-unclassified upstream tool | denied, and the compatibility test fails |
| K | adversarial object name / parameter value | rendered program structurally identical; no execution change |

Failure cases required at tier 2 and, where meaningful, tier 3: Blender unavailable, worker
disconnected, MCP server down, add-on disagreement, project lock conflict, render failure,
invalid object, invalid units, duplicate job, interrupted operation.

---

## 17. Decisions requiring review

### Closed by review

- **Official Blender Lab MCP is the primary Blender backend**, behind
  `BlenderCapabilityProvider` / `OfficialBlenderLabBackend`.
- **We are not building a second MCP server.**
- **No model-authored code, no user-authored arbitrary Python.** Mutation Python comes only
  from a closed catalogue of reviewed, parameterised, platform-owned templates.
- **Unknown semantic capability → DENY.**
- **Community MCPs are references / possible future providers / review-gated fallback only.**
  Not integrated in Task 3.
- **Policy fails closed**; the upstream weak sandbox is defence-in-depth, never authorisation.
- **Loopback invariant** replaces "never listens" with "no execution interface is externally
  reachable".
- **Scene-version enforcement** — in-lock precondition with chained per-operation requirements.
- **Digest projection** — explicit versioned allow-list `studio-scene-v1` (Task 1).
- **Resize representation** — `set_object_dimensions` with absolute metres primary.
- **Canonical colour** — linear sRGB RGBA, transfer function decoded.
- **Read consistency** — a read holds the project lock.
- **High-level architectural capability is built above `BlenderCapabilityProvider`**, never as
  another MCP implementation.
- **Version pinning** — pair-pinned, never auto-upgraded, verified version recorded in
  `verification.md`.

### Open

**D1 — Session model: interactive add-on vs `*_for_cli` background.** The interactive path
has the full read surface and deferred responses but operates on whatever file is open; the
CLI path takes an explicit `blend_file` (matching Spec 001) but discards changes unless our
template saves, and can create a numbered sibling file. This choice drives project isolation,
lock scope, crash recovery and how a project is opened. **Task 3 steps 2, 7, 11 and 15
gather the evidence.**

**D2 — Does `inspect_scene` need a read template?** Depends on whether
`get_objects_summary` + `get_object_detail_summary` supply scene units and world-space
dimensions. Task 3 step 6.

**D3 — Snapshot read cost.** If a snapshot requires `get_object_detail_summary` per object, a
large scene costs N+1 round trips. Measure in Task 3; decide caching and summarisation in
Task 5.

**D4 — Which real provider** (Astra, Codex, or another `AgentProvider`-compatible
implementation) and **D5 — the structured-output mechanism**. Deferred to Task 9.

**D6 — File and reference ingestion in Spec 002 or Spec 003** (Requirement 17.6). Task 12
decides explicitly.

**D7 — Preview ownership.** Spec 001's deterministic offscreen Workbench preview versus the
official `render_*_to_path` / screenshot tools. Current intent: keep ours as the product
preview, treat theirs as extra capabilities.

**D8 — Canonical pin identity.** The design was verified against the GitHub mirror commit
`4309a396…`; the canonical `projects.blender.org` identity and the `.mcpb` release version
must be reconciled and recorded in Task 3 step 1.

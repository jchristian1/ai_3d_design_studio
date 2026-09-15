# Requirements — 002 Intelligent Scene Agent

## Introduction

Spec 001 proved the vertical slice: one hard-coded sentence shape, interpreted by a
deterministic `RuleBasedProvider`, moving one cube. Every boundary works and is
regression-tested (see `001-core-vertical-slice/verification.md`).

Spec 002 makes the assistant **intelligent** (a real LLM), **scene-aware** (an
authoritative snapshot) and **capable** (real Blender operations) — and it performs the
Blender work through the **official Blender MCP**, maintained by the Blender Lab,
behind a platform-owned capability provider.

### The backend decision (this refactor)

> The official Blender Lab MCP is the primary Blender backend.
> We are not building a second MCP server.

The full path is:

```
Browser
  → FastAPI control plane
    → AgentProvider
      → authoritative scene context
        → validated semantic proposal
          → platform-owned safety / policy / durability layer
            → BlenderCapabilityProvider
              → OfficialBlenderLabBackend
                → official Blender MCP (server + add-on)
                  → Blender
```

Everything above `BlenderCapabilityProvider` is platform-owned and backend-agnostic.
`OfficialBlenderLabBackend` is the only component that knows the official MCP exists.

Community Blender MCP implementations (`djeada/blender-mcp-server`,
`ahujasid/blender-mcp`, `RFingAdam/mcp-blender`) are **no longer primary
dependencies**. They remain useful as capability references, as possible optional
future providers, and as a possible temporary fallback **only after explicit review**.
They SHALL NOT be integrated during Task 3.

### The governing principles

1. **The model gains language ability, not authority.** It proposes; the platform
   validates, resolves, guards and executes.
2. **No model-authored code, ever.** `AgentProvider` produces only validated semantic
   operations. It never produces `bpy` code, never produces Python, and never chooses an
   arbitrary MCP tool.
3. **A discovered tool is not a permitted tool.** Discovery informs a platform-owned
   policy; the policy decides, and fails closed.
4. **The MCP is an integration boundary, not a library.** We speak protocol to it; we do
   not import it, fork it or copy it.

## Glossary

- **SceneSnapshot** — an authoritative, read-only, safe description of a project's
  current scene. Produced by NORMALISING backend read output; never model-authored, and
  never handed to a provider in raw backend form.
- **Scene version** — a content digest of an explicitly enumerated, versioned projection
  of semantic scene state (`studio-scene-v1`, design §2.3 of Task 1), used for caching
  and as an **enforced execution precondition**.
- **Capability** — a platform-named ability. The Spec 002 vocabulary is
  `inspect_scene`, `inspect_object`, `move_object`, `rotate_object`,
  `set_object_dimensions`, `set_material_color`, `create_object`, `delete_object`,
  `duplicate_object`, `render`, and further entries added by registry extension.
  Capabilities are what the agent reasons about; backend tool names never reach it.
- **BlenderCapabilityProvider** — the platform-owned interface that accepts a validated
  capability invocation and returns a canonical result. Backend-agnostic.
- **OfficialBlenderLabBackend** — the implementation of `BlenderCapabilityProvider` that
  speaks the Model Context Protocol to the official Blender MCP server.
- **Guarded execution template** — a fixed, platform-owned, reviewed Python source
  template, parameterised only with validated canonical values, used when the official
  MCP has no native semantic tool for a capability.
- **Template catalogue** — the closed set of guarded execution templates. Unknown
  semantic capability → DENY.
- **Proposal** — the model's *untrusted* structured output.
- **Plan** — a *validated, resolved* AgentPlan the platform will execute.
- **Clarification** — a first-class agent outcome that asks a question and emits no Jobs.
- **Canonical angle** — radians (Requirement 8). **Canonical colour** — linear sRGB RGBA
  (Requirement 8).

---

## Requirements

### Requirement 1 — Authoritative scene grounding

**User Story:** As a user, I want the assistant to know what is actually in my project,
so that its answers and changes reflect reality rather than a guess.

#### Acceptance Criteria

1. WHEN the agent needs scene knowledge THEN the platform SHALL obtain a `SceneSnapshot`
   derived from the current Blender project state, not from model memory or a database
   summary.
2. WHEN a SceneSnapshot is produced THEN it SHALL be read-only with respect to the
   project: producing it SHALL NOT mutate the scene, save the project, create a recovery
   point, write a mutation journal record, or create a preview.
3. WHEN a SceneSnapshot is produced THEN it SHALL be built by NORMALISING the output of
   read-only capabilities served by `BlenderCapabilityProvider`. The platform SHALL NOT
   implement its own Blender scene-inspection engine where the official MCP already
   exposes scene and object inspection.
4. WHEN backend output is received THEN it SHALL be treated as UNTRUSTED integration data
   and validated before a `SceneSnapshot` is constructed from it.
5. WHEN one backend read is insufficient THEN the backend MAY compose several read-only
   tools into one snapshot.
6. WHEN a provider receives scene context THEN it SHALL receive the canonical
   `SceneSnapshot` only. Raw backend output SHALL NOT be exposed to `AgentProvider`.
7. WHEN a SceneSnapshot is produced THEN it SHALL carry `project_id`, the scene's unit
   configuration, a `scene_version` identity, and a list of objects.
8. WHEN an object appears in a SceneSnapshot THEN it SHALL include its stable
   `studio_object_id` where one exists, its name, object type, world position in metres,
   dimensions in metres, rotation, scale, a basic material summary, and safe visibility
   state.
9. WHEN a SceneSnapshot is serialized THEN it SHALL NOT contain a filesystem path, a
   project-file location, worker credentials, hostnames, MCP transport detail, or
   arbitrary Blender internals.
10. IF the scene cannot be read THEN the platform SHALL return a structured error and
    SHALL NOT fall back to an assumed or stale scene for a mutation decision.
11. WHEN a mutation succeeds THEN any cached SceneSnapshot for that project SHALL be
    invalidated.
12. WHEN the authoritative scene is read THEN the read SHALL hold the project's EXISTING
    execution lock for the duration of the inspection, so the snapshot describes ONE
    consistent project state.

    Rationale, recorded because it looks like a contradiction: a lock-free read could run
    while another execution is mutating and saving the same project and observe a torn
    scene. The snapshot is what the agent reasons against and what a plan's
    `scene_version` precondition is compared to, so a torn snapshot would poison both. A
    shared-read / exclusive-write lock is explicitly deferred, and a second locking
    mechanism SHALL NOT be introduced.
13. IF the project lock cannot be acquired for a read THEN the platform SHALL return the
    existing structured `LOCK_CONFLICT` outcome, SHALL NOT introduce a second lock error
    model, and SHALL NOT bypass or downgrade the lock.
14. WHEN a read completes, successfully or not, THEN the lock SHALL be released, and an
    unrelated project SHALL remain independently lockable throughout.
15. WHEN the same unchanged project is inspected twice THEN the two snapshots SHALL carry
    an identical `scene_version` and MAY carry different `captured_at` values.
16. WHEN a read job is retried THEN inspection MAY execute again, because a read has no
    mutation side effect and the caller wants CURRENT state. Mutation-style idempotency
    machinery SHALL NOT be used to return a cached older snapshot, and read retry
    behaviour SHALL NOT weaken mutation idempotency.

### Requirement 2 — Scene version identity and enforcement

**User Story:** As the platform, I want to know exactly which scene a plan was reasoned
against, so that a change is never applied to a scene that has moved on.

#### Acceptance Criteria

1. WHEN a mutation plan is produced from a SceneSnapshot THEN the plan SHALL record the
   authoritative `scene_version` it was reasoned against.
2. WHEN the first mutation of a plan is about to execute THEN the platform SHALL verify
   that the authoritative scene state still matches the plan's recorded `scene_version`,
   while the project lock is held, before any recovery point or mutation.
3. IF the authoritative scene version no longer matches THEN the platform SHALL NOT
   mutate Blender, SHALL return the canonical error `SCENE_VERSION_MISMATCH`, SHALL
   refresh the SceneSnapshot, and SHALL require re-resolution and re-planning.
4. WHEN a plan reasons about a relationship between objects (for example "move the chair
   closer to the table") THEN per-object `expected_before` verification SHALL NOT be
   treated as sufficient: the scene-version precondition SHALL also hold, because an
   object used only in the reasoning may have moved.
5. WHEN a mutation completes THEN the resulting authoritative `scene_version` SHALL be
   computed from the observed post-mutation state and reported.
6. WHEN `scene_version` is computed THEN it SHALL use the single authoritative Task 1
   implementation (`compute_scene_version`) over the explicitly enumerated, versioned
   projection `studio-scene-v1`. A second digest implementation SHALL NOT exist.
7. WHEN the digest is computed THEN it SHALL exclude `captured_at`, `scene_version`
   itself, `project_id`, worker id, hostname, filesystem paths, tokens, request and
   session ids, preview metadata, and transport metadata.
8. WHEN the digest is computed THEN object enumeration order SHALL NOT affect it, and
   numeric serialization SHALL remain deterministic and shared across languages.
9. WHEN a new field is added to `SceneSnapshot` THEN it SHALL be classified explicitly as
   informational or planning-relevant, and an automated check SHALL fail if a field is
   left unclassified.

### Requirement 3 — Real LLM provider behind the existing abstraction

**User Story:** As the platform, I want a real language model reached only through
`AgentProvider`, so that intelligence improves without weakening any boundary.

#### Acceptance Criteria

1. WHEN a real provider is added THEN it SHALL implement the existing `AgentProvider`
   protocol and SHALL be reached only through the provider registry.
2. WHEN provider selection is configured THEN it SHALL be configuration, not code:
   switching providers SHALL require no change to FastAPI routes, `ChatService`, the
   worker, the capability provider, the backend, or the frontend.
3. WHEN Spec 002 completes THEN `RuleBasedProvider` SHALL remain available and
   selectable, so deterministic tests and offline development keep working.
4. WHEN credentials are required THEN they SHALL come from environment or configuration
   only, SHALL never be committed, logged, returned through the API, or placed in a
   `NEXT_PUBLIC_*` variable.
5. WHEN the default test suite runs THEN it SHALL NOT require network access, an API key,
   paid model calls, a running Blender, or a running MCP server.
6. IF live-provider tests exist THEN they SHALL be explicitly opt-in and SHALL skip
   cleanly when credentials are absent.
7. WHEN a provider returns metadata THEN it SHALL NOT include chain-of-thought, hidden
   reasoning, or raw prompt contents.
8. IF the configured provider is unavailable or unauthenticated THEN the platform SHALL
   return a structured `PROVIDER_UNAVAILABLE` outcome and SHALL NOT fabricate a plan.
9. WHEN Spec 002 is declared product-complete THEN the configured **real** provider SHALL
   have been demonstrated, on the development environment, to satisfy the live-provider
   acceptance gate. Passing only with `FakeLlmProvider` SHALL NOT be sufficient.
10. WHEN the live-provider acceptance gate runs THEN the real provider SHALL successfully
    produce, end to end: a grounded `Answer`; a schema-valid proposal for a move that
    becomes a validated plan and a real verified Blender mutation; ordered validated
    operations for a two-part instruction; and a `Clarification` with zero mutations for
    an ambiguous instruction. These tests SHALL assert structured outcomes and safety
    boundaries, never exact prose.

### Requirement 4 — Constrained structured model output

**User Story:** As the platform owner, I want the model's output to be data the platform
validates, so that language never becomes execution authority.

#### Acceptance Criteria

1. WHEN the provider calls the model THEN it SHALL request structured output constrained
   to a declared schema of proposable **platform capabilities**.
2. WHEN model output is received THEN the platform SHALL validate it against that
   canonical schema BEFORE any resolution or execution.
3. WHEN model output is invalid, malformed, truncated, or unparseable THEN the platform
   SHALL return a structured error and SHALL emit no Jobs.
4. WHEN model output names a capability the platform does not implement, or one the
   policy does not permit, THEN the platform SHALL reject it and SHALL emit no Jobs.
5. WHEN model output is processed THEN the platform SHALL NEVER evaluate, execute, or
   shell out to any text the model produced, and SHALL NEVER forward model-authored text
   into a guarded execution template, a code-execution tool, or any other execution path.
6. WHEN model output includes fields the platform did not declare THEN those fields SHALL
   be rejected rather than ignored.
7. WHEN a proposal is accepted THEN the platform SHALL convert it into an internal plan
   through its own construction code, so the model never authors a Job.
8. WHEN the agent is given a tool catalogue THEN it SHALL contain PLATFORM capability
   names only. Backend tool names SHALL NOT appear in prompts, proposals, plans, Jobs,
   API responses, or the browser.

### Requirement 5 — Read-only questions

**User Story:** As a user, I want to ask about my project, so that I can understand it
without changing anything.

#### Acceptance Criteria

1. WHEN the user asks a question answerable from the SceneSnapshot THEN the platform SHALL
   return an `Answer`.
2. WHEN an `Answer` is produced THEN the platform SHALL create zero mutation Jobs.
3. WHEN an `Answer` is produced THEN the platform SHALL NOT modify the project, SHALL NOT
   save, and SHALL NOT generate a new preview.
4. WHEN an answer states a measurement THEN it SHALL be consistent with the
   SceneSnapshot's canonical values.
5. IF a question cannot be answered from the available snapshot THEN the platform SHALL
   say so plainly rather than inventing a value.

### Requirement 6 — Clarification as a first-class outcome

**User Story:** As a user, I want to be asked when my instruction is ambiguous, so that
the assistant never changes the wrong thing.

#### Acceptance Criteria

1. WHEN an instruction matches more than one candidate object THEN the platform SHALL
   return a `Clarification` naming the candidates.
2. WHEN an instruction relies on an unresolvable referent ("it", "that one") THEN the
   platform SHALL return a `Clarification`.
3. WHEN a `Clarification` is returned THEN the platform SHALL create zero mutation Jobs
   and SHALL NOT modify the project.
4. WHEN a `Clarification` is returned THEN the response SHALL carry enough session and
   request context for the user's next message to continue the same intent.
5. WHEN the user answers a clarification THEN the platform SHALL resolve the original
   intent against the answer and proceed, or clarify again.
6. WHEN clarification state is retained THEN it SHALL be short-lived and session-scoped,
   and SHALL NOT constitute persistent project memory.
7. IF a clarification is never answered THEN it SHALL expire without side effects.
8. WHEN a `Clarification` is returned THEN it SHALL be presented as a question, not as an
   error.

### Requirement 7 — Object resolution against the authoritative scene

**User Story:** As the platform, I want objects resolved from real scene state, so that
no change is ever applied to an invented object.

#### Acceptance Criteria

1. WHEN the model refers to an object THEN the platform SHALL resolve that reference
   against the authoritative SceneSnapshot.
2. WHEN resolution succeeds THEN the resulting operation SHALL address the object by its
   stable `studio_object_id` where one exists.
3. WHEN the model emits an object identifier THEN the platform SHALL verify it exists in
   the SceneSnapshot and SHALL reject any identifier that does not.
4. WHEN exactly one object matches THEN resolution SHALL succeed.
5. WHEN several objects match THEN the platform SHALL return a `Clarification`.
6. WHEN no object matches THEN the platform SHALL return a structured error or a
   clarification, and SHALL NOT create the object, guess a substitute, or emit a Job.
7. WHEN a browser-selected object is supplied THEN the platform MAY use it to resolve an
   otherwise ambiguous referent, provided it exists in the SceneSnapshot.
8. WHEN an operation crosses into the backend THEN `OfficialBlenderLabBackend` SHALL
   translate the resolved stable id into whatever identifier the official MCP requires,
   and that translation SHALL be the ONLY place the two identity schemes meet.
9. WHEN an identifier is used as a template parameter THEN it SHALL be a validated
   identifier that exists in the authoritative snapshot, and never a free-form string
   taken from model output (Requirement 11).

### Requirement 8 — Canonical units, angles and colour

**User Story:** As the platform, I want one canonical representation per quantity, so
that conversion bugs cannot reach Blender.

#### Acceptance Criteria

1. WHEN distances or dimensions cross any boundary THEN they SHALL be in metres.
2. WHEN rotations cross any boundary THEN they SHALL be in **radians**.
3. WHEN a user expresses an angle in degrees THEN conversion to radians SHALL happen
   exactly once, in `packages/spatial`.
4. WHEN a user expresses a resize as a proportion THEN the canonical mutation SHALL carry
   absolute desired dimensions in metres, derived from the authoritative SceneSnapshot
   above the capability provider. A percentage SHALL NEVER cross into the backend.
5. WHEN an explicit transform-scale intent is expressed THEN it SHALL carry validated
   absolute unitless scale values.
6. WHEN a colour crosses any boundary THEN it SHALL be canonical linear sRGB RGBA in
   `[0, 1]`; a colour NAME SHALL NEVER cross into the backend.
7. WHEN unit, angle, percentage or colour conversion logic exists THEN it SHALL exist only
   in `packages/spatial`, enforced by the source guard.
8. WHEN world-space directions are interpreted THEN Spec 001's mapping SHALL hold: right
   +X, left −X, forward +Y, back −Y, up +Z, down −Z; camera-relative interpretation
   remains deferred.
9. IF the official MCP or a template expects a different unit or colour space THEN the
   backend SHALL convert at the boundary using `packages/spatial`, and the conversion
   SHALL be documented and tested.

### Requirement 9 — Blender capability provider backed by the official Blender MCP

**User Story:** As the product owner, I want Blender ability to come from the official
Blender MCP, so that we build product on the implementation Blender itself maintains
instead of maintaining a Blender integration.

#### Acceptance Criteria

1. WHEN Blender work is performed THEN it SHALL be performed through the official Blender
   MCP (Blender Lab: MCP server plus Blender MCP add-on), reached over the real Model
   Context Protocol.
2. WHEN the platform integrates the official MCP THEN it SHALL NOT fork it, copy its
   source or its tool-code into this repository, duplicate its add-on, or import its
   Python modules as if they were a local library.
3. WHEN the worker needs Blender ability THEN it SHALL talk ONLY to
   `BlenderCapabilityProvider`, so a different backend can be substituted without
   changing any component above it.
4. WHEN capabilities are named THEN they SHALL be PLATFORM capability names, mapped inside
   `OfficialBlenderLabBackend` to native official tools, ordered tool sequences, or
   guarded execution templates. `AgentProvider`, FastAPI routes, `JobFactory`, canonical
   Jobs and the frontend SHALL NOT reference official tool names.
5. WHEN a capability cannot be served by the configured backend THEN the platform SHALL
   report a structured, capability-named failure and SHALL NOT approximate it with an
   unguarded code-execution call.
6. WHEN the official MCP provides a native tool for an ability THEN the platform SHALL
   use it rather than re-implementing that ability. This explicitly covers scene and
   object inspection, blend-file summaries, API and manual documentation lookup,
   screenshots and rendering to a path.
7. WHEN platform-owned code exists around a capability THEN it SHALL be limited to OUR
   safety semantics — scene-version checks, expected-before checks, desired-after
   targets, idempotent-retry detection, result verification — plus the guarded execution
   templates of Requirement 11. It SHALL NOT constitute a second MCP server.
8. WHEN the official MCP is selected THEN both the MCP server build and the Blender
   add-on build SHALL be pinned to exact immutable identities (Requirement 16).
9. WHEN the official MCP is pinned THEN the repository SHALL record the source, the
   version or commit, the discovered tool inventory with schemas, the add-on protocol
   expectations, and the compatibility assumptions relied upon.
10. WHEN an upstream update changes the tool inventory or a tool schema THEN a
    compatibility test SHALL FAIL rather than the platform silently adapting.
11. WHEN the MCP server and the add-on must agree THEN both SHALL be pinned together and
    their agreement SHALL be verified at startup before any capability is served.
12. WHEN community Blender MCP implementations are considered THEN they SHALL be treated
    as capability references, possible optional future providers, or a possible temporary
    fallback ONLY after explicit review. They SHALL NOT be integrated during Task 3.

### Requirement 10 — Platform-owned capability policy over the official tool surface

**User Story:** As the platform owner, I want to decide which backend capabilities are
reachable, so that adopting the official MCP does not adopt its entire surface.

#### Acceptance Criteria

1. WHEN tools are discovered from the official MCP THEN discovery SHALL NOT imply
   permission. A platform-owned policy SHALL decide.
2. WHEN a discovered tool is not explicitly classified THEN it SHALL be DENIED. The policy
   SHALL fail closed.
3. WHEN the policy classifies a tool THEN it SHALL be exactly one of:
   - **Tier A — allowed:** safe read-only tools, such as scene and object summaries,
     blend-file path and data-block summaries, documentation and API search, and window
     or area screenshots.
   - **Tier B — guarded:** tools with side effects the platform must bound, such as
     rendering to a path, viewport navigation, and any tool taking a filesystem
     destination. These SHALL require explicit platform configuration, and the
     destination SHALL be platform-derived from `project_id` — never model-chosen.
   - **Tier C — never offered to a model:** generic code execution and any tool
     equivalent to arbitrary Python, shell command, subprocess launch, arbitrary file
     read or write, arbitrary network access, package installation, or persistent
     background code.
4. WHEN a Tier C tool exists in the official MCP THEN it SHALL NOT appear in the model's
   permitted capability catalogue, and no model-authored text SHALL ever reach it
   (Requirement 11).
5. WHEN the official MCP or its add-on offers a sandbox or safety mechanism THEN it SHALL
   be treated as defence-in-depth only. It SHALL NOT be treated as authorisation, and
   "the upstream sandbox is on, therefore execution is safe" SHALL NOT appear in any
   design decision.
6. WHEN a denied or unconfigured tool would be invoked THEN the platform SHALL refuse
   before any MCP call is made, and SHALL record the refusal.
7. WHEN the policy is evaluated THEN it SHALL be evaluated on the PLATFORM side, in the
   worker or control plane, and SHALL NOT depend on the MCP server or add-on enforcing
   it.
8. WHEN the permitted catalogue is assembled for a model THEN it SHALL be derived from the
   policy, so a model cannot name a capability it was never offered.

### Requirement 11 — No model-authored code; closed catalogue of guarded execution templates

**User Story:** As the platform owner, I want every character of Python that reaches
Blender to have been written and reviewed by us, so that adopting a code-oriented MCP
does not hand execution to a language model.

#### Acceptance Criteria

1. WHEN Spec 002 is implemented THEN **no model-authored code SHALL be permitted**, and
   **no user-authored arbitrary Python SHALL be permitted**. There is no exception, no
   developer flag in the product path, and no "advanced mode".
2. WHEN the model's capability catalogue is assembled THEN it SHALL NOT contain a generic
   `execute_python`, `execute_blender_code`, `run_script` or equivalent capability, under
   any name.
3. IF the official MCP has no native semantic tool for a required capability THEN the
   capability MAY be served by Python that satisfies ALL of the following:
   - generated entirely by platform-owned, reviewed templates;
   - parameterised only with validated canonical values;
   - never authored or modified by the LLM;
   - never accepted from the browser;
   - never accepted from an MCP response;
   - narrow to the intended operation.
4. WHEN a guarded execution template is written THEN its Python source text SHALL be a
   fixed literal in this repository, reviewed like production code, and SHALL NOT be
   assembled, concatenated or selected at runtime from any untrusted input.
5. WHEN a template is parameterised THEN parameters SHALL be passed through a structured,
   safely encoded mechanism. There SHALL BE **no string interpolation of object names, or
   of any other value, directly into Python source**: no f-string, no `%`, no `.format`,
   and no concatenation of a value into template text.
6. WHEN a template addresses an object THEN it SHALL address it by a validated stable
   identifier that exists in the authoritative SceneSnapshot.
7. WHEN a template is executed THEN it SHALL NOT `eval` or `exec` model text, SHALL NOT
   invoke a shell or subprocess, SHALL NOT perform an arbitrary import, SHALL NOT accept
   an arbitrary filesystem path, SHALL NOT accept an arbitrary URL, and SHALL NOT install
   a package.
8. WHEN the template catalogue is defined THEN it SHALL be **closed**: a fixed, enumerated
   set. WHEN a semantic capability has no entry in the catalogue and no native tool THEN
   the platform SHALL **DENY** it with a structured capability-named failure.
9. WHEN a template's parameter record is validated THEN every field SHALL be a canonical
   scalar, vector or validated identifier of a declared type and range; a field of
   free-form string type that is not a validated identifier SHALL NOT exist.
10. WHEN the template catalogue is tested THEN automated tests SHALL prove: the catalogue
    is closed and enumerated; each template's rendered source for a given parameter record
    is exactly the expected text; an attempt to smuggle Python through a parameter value
    (quotes, newlines, `__import__`, `;`) cannot alter the rendered program's structure;
    and no code path renders a template from model output.
11. WHEN a source guard runs THEN it SHALL statically prove that template modules contain
    no dynamic source construction, and that no module outside the template catalogue
    sends Python to the backend.
12. WHEN a native semantic tool becomes available for a capability THEN replacing that
    capability's template with the native tool SHALL be a change confined to
    `OfficialBlenderLabBackend` and its registry (Requirement 17.5).

### Requirement 12 — Blender and MCP exposure

**User Story:** As the platform owner, I want Blender and its MCP to be unreachable from
outside the workstation, so that adopting a local listener creates no entry point.

#### Acceptance Criteria

1. WHEN Spec 001's "the workstation never listens" invariant is restated for Spec 002 THEN
   it SHALL become the more precise invariant: **no Blender or MCP execution interface is
   externally reachable.**

   The official Blender MCP add-on requires a LOCAL listener, so an absolute "never binds
   a socket" rule is no longer accurate. The property that actually matters — nothing on
   the design machine is reachable from off the machine — is preserved and stated
   directly.
2. WHEN the official add-on binds a socket THEN it SHALL bind loopback only
   (`127.0.0.1` / `localhost`). It SHALL NOT bind `0.0.0.0`, a LAN address, or a public
   address.
3. WHEN the platform is configured THEN a non-loopback MCP or add-on host SHALL be
   rejected in the default production-safe configuration, and a configuration guard test
   SHALL enforce this.
4. WHEN the control plane is deployed THEN the MCP or add-on port SHALL NOT be exposed
   through it, tunnelled by it, proxied by it, or forwarded by a router.
5. WHEN the browser runs THEN it SHALL have no direct access to the MCP server, the add-on
   port, or any execution interface.
6. WHEN the worker connects to the control plane THEN that connection SHALL remain
   OUTBOUND and authenticated, exactly as in Spec 001.
7. WHEN the worker connects to the official MCP server THEN it SHALL do so as a local MCP
   client over a local transport (stdio-launched child process preferred), and the MCP
   server SHALL NOT be exposed to any network.
8. WHEN the add-on's local socket has no authentication THEN the platform SHALL treat
   loopback binding plus single-tenant workstation assumptions as the boundary, SHALL
   document that assumption, and SHALL NOT rely on the add-on for authorisation.
9. WHEN a developer-only diagnostic capability exists THEN it SHALL be isolated from the
   product path and disabled by default, and a test SHALL assert it is absent from the
   production-safe configuration.

### Requirement 13 — Durable mutation guard around non-idempotent backend calls

**User Story:** As a user, I want a crash never to double-apply a change, even though the
backend makes no retry-safety promise.

#### Acceptance Criteria

1. WHEN a mutation capability is invoked THEN the platform SHALL assume the backend call is
   NOT retry-idempotent and SHALL provide idempotency itself.
2. BEFORE a backend mutation is invoked THEN the platform SHALL durably persist: the
   intended operation, the required `scene_version`, the expected-before state, the
   desired-after state, and a recovery point where applicable.
3. AFTER a backend mutation is invoked THEN the platform SHALL inspect the actual resulting
   state through a read capability, verify it against the desired-after state, and durably
   record the result.
4. WHEN a mutation is retried after a crash THEN the platform SHALL FIRST inspect the
   current Blender state, and:
   - IF the desired-after state is already present THEN it SHALL mark the operation
     `already_applied` and SHALL NOT invoke the backend mutation again;
   - ELSE IF the current state matches expected-before THEN it SHALL invoke the backend
     mutation;
   - ELSE it SHALL return `PRECONDITION_MISMATCH` or `SCENE_VERSION_MISMATCH` as
     appropriate and SHALL mutate nothing.
5. WHEN verification fails THEN the platform SHALL NOT report success, and the recovery
   point SHALL be preserved.
6. WHEN mutation identity is derived THEN it SHALL remain
   `project_id + request_id + operation_index`.
7. WHEN the guard is described THEN it SHALL be understood as OUR safety semantics rather
   than a Blender implementation, and SHALL contain no Blender geometry logic and no
   template source.
8. WHEN a mutation is performed THEN the platform SHALL own the save: the project SHALL be
   saved deliberately by the platform's workflow, and success SHALL NOT be reported for a
   change that exists only in an unsaved session.
9. WHEN a project is operated on THEN the platform SHALL guarantee the backend is acting on
   THAT project, so a worker never modifies another project's Blender file.

### Requirement 14 — Multi-operation requests

**User Story:** As a user, I want to give one instruction containing several changes, so
that I do not have to type them separately.

#### Acceptance Criteria

1. WHEN one request implies several changes THEN the platform SHALL produce an ordered
   `AgentPlan` whose operations carry `operation_index` 0, 1, 2, … in execution order.
2. WHEN a plan contains several operations THEN all resulting Jobs SHALL share the
   originating `request_id`.
3. WHEN a multi-operation request is retried verbatim THEN no operation SHALL be applied a
   second time.
4. WHEN a multi-operation request is retried after partial completion THEN only the
   operations not yet applied SHALL execute.
5. WHEN a genuinely new `request_id` carries the same instruction THEN the changes SHALL be
   applied again intentionally.
6. WHEN operations execute THEN they SHALL execute in `operation_index` order.
7. IF an operation fails THEN the platform SHALL stop, SHALL NOT execute later operations,
   and SHALL report per-operation status distinguishing applied, failed and not-attempted.
8. WHEN a plan partially fails THEN the platform SHALL NOT claim overall success and SHALL
   NOT silently roll back applied operations; the documented semantics are sequential,
   non-atomic and resumable.
9. WHEN operation 0 executes THEN its scene-version precondition SHALL be the version the
   plan was reasoned against; a later operation's precondition SHALL be the version
   resulting from its predecessor, so an intentional change does not reject the next step
   while an EXTERNAL change still does.

### Requirement 15 — Safety and threat resistance

**User Story:** As the platform owner, I want untrusted language to be unable to escape the
capability boundary, so that adding an LLM and a code-oriented MCP adds no attack surface.

#### Acceptance Criteria

1. WHEN a user message contains an injection attempt THEN it SHALL NOT cause shell
   execution, Python evaluation, subprocess launch, filesystem access, package
   installation, or a code-execution call in Blender.
2. WHEN model output is processed THEN it SHALL NOT be able to introduce a new worker
   protocol message type.
3. WHEN model output is processed THEN it SHALL NOT be able to supply a filesystem path, a
   project-file location, an MCP host or port, an external URL, or template source text.
4. WHEN model output is processed THEN it SHALL NOT be able to override the trusted
   `project_id`, `user_id` or `session_id`.
5. WHEN model output is processed THEN it SHALL NOT be able to bypass object resolution
   against the SceneSnapshot, or to name a backend tool directly.
6. WHEN prompts are constructed THEN they SHALL contain no credentials, no filesystem
   paths, no worker token, no backend tool names, and no template source.
7. WHEN backend output is processed THEN it SHALL NOT be able to cause execution: a value
   returned by the MCP SHALL never become code, a template parameter that alters program
   structure, or a permission decision.
8. WHEN Spec 002 completes THEN these Spec 001 invariants SHALL still hold: no Blender or
   MCP execution interface is externally reachable (Requirement 12), the worker protocol
   vocabulary remains closed, no arbitrary-execution capability is offered to the model,
   and jobs and artifacts remain project-scoped with no unscoped routes.
9. WHEN the model requests an unavailable or denied capability THEN the platform SHALL
   refuse with a structured outcome rather than approximating it.

### Requirement 16 — Upstream version pinning and upgrade governance

**User Story:** As the platform owner, I want an upstream upgrade to be a verified
decision, so that a change in Blender or in the official MCP never silently breaks or
widens what our users' projects are exposed to.

#### Acceptance Criteria

1. WHEN the official MCP server and add-on are adopted THEN both SHALL be pinned to exact
   immutable identities, and `latest` SHALL NOT be used anywhere.
2. WHEN the pin is recorded THEN it SHALL identify the canonical upstream source, the
   commit or release identity, the add-on build, the required minimum Blender version, and
   the licence.
3. WHEN an update is considered THEN the platform SHALL **never auto-upgrade blindly**. The
   sequence SHALL be:
   1. discover the new version and record its identity;
   2. run the compatibility suite (tool inventory, tool schemas, add-on agreement);
   3. run the safety tests (policy fail-closed, Tier C never offered, template catalogue
      closed, no model-authored code path);
   4. run the Blender acceptance tests against the real backend;
   5. update the pin ONLY when all of the above are green.
4. IF any step fails THEN the pin SHALL remain unchanged and the failure SHALL be reported.
5. WHEN the compatibility suite runs THEN a changed tool inventory or schema SHALL FAIL
   loudly rather than being absorbed, and any newly appeared tool SHALL be denied until
   classified.
6. WHEN a version is verified THEN **the verified Blender MCP version SHALL be recorded in
   `verification.md`**, together with the verified Blender version and the date.
7. WHEN the upstream collects telemetry or transmits data off the machine THEN it SHALL be
   disabled by default and the disabling SHALL be verified in configuration tests.
8. WHEN a tool parameter exists to capture the user's verbatim words for third-party
   purposes THEN the platform SHALL NOT populate it with user content.
9. WHEN the upstream licence is recorded THEN its compatibility with this project SHALL be
   confirmed, and the integration SHALL remain a separate-process protocol integration
   rather than a source or link-level one.

### Requirement 17 — Long-term architectural design capability is not foreclosed

**User Story:** As the product owner, I want Spec 002's architecture to support the real
product — users uploading plans and photographs and asking for buildings and interiors —
so that we are not locked into a handful of hand-written operations.

#### Acceptance Criteria

1. WHEN the long-term goal is stated THEN it SHALL remain: users upload floor plans,
   architectural drawings, photographs, elevations, site and topography references, and
   furniture or material references; a multimodal agent builds interiors, houses,
   buildings, furniture layouts and architectural models.
2. WHEN high-level architectural capability is designed THEN it SHALL be built **above**
   `BlenderCapabilityProvider` — as composition of semantic capabilities, planning and
   constraint reasoning — and NOT as another MCP implementation.
3. WHEN the capability registry is designed THEN it SHALL be extensible to at least:
   `inspect_scene`, `inspect_object`, `move_object`, `rotate_object`,
   `set_object_dimensions`, `set_material_color`, `create_object`, `delete_object`,
   `duplicate_object`, `render`, and later structural and reference capabilities.
4. WHEN a new capability is added THEN it SHALL require a registry entry, a policy
   classification, and either a native tool mapping or a reviewed template — and SHALL NOT
   require a new MCP server.
5. WHEN the official MCP later exposes native semantic tools (for example `object.move`,
   `material.set_color`, `scene.inspect`) THEN the upgrade SHALL be the replacement of
   `platform capability → guarded execution template` with
   `platform capability → official semantic MCP tool`, and **nothing above
   `BlenderCapabilityProvider` SHALL change**. A test SHALL demonstrate this by swapping a
   capability's implementation with no change above the provider.
6. WHEN file and reference ingestion is scoped THEN it SHALL be either delivered in Spec 002
   or explicitly deferred to Spec 003 with the boundary recorded.

### Requirement 18 — Browser experience

**User Story:** As a user, I want one coherent reply per thing I say, so that the
conversation stays readable even when the system does several things internally.

#### Acceptance Criteria

1. WHEN the user sends one message THEN the transcript SHALL show exactly one user entry and
   exactly one Studio reply slot for it.
2. WHEN one request produces several internal Jobs THEN the browser SHALL still show one
   Studio reply, not one per operation.
3. WHEN the outcome is an `Answer` THEN the browser SHALL render it as an assistant reply
   with no mutation progress and no preview change.
4. WHEN the outcome is a `Clarification` THEN the browser SHALL render it as a question,
   answerable in the normal input.
5. WHEN a mutation is in progress THEN the browser SHALL show progress and SHALL show final
   success or a readable failure.
6. WHEN a multi-operation plan partially fails THEN the browser SHALL communicate what was
   applied and what was not, without claiming overall success.
7. WHEN a request is refused because the scene changed underneath it THEN the browser SHALL
   explain that the project changed and that nothing was modified.
8. WHEN a preview is produced THEN the browser SHALL display it, and a colour change SHALL
   be visible in the displayed preview.
9. WHEN anything is rendered THEN it SHALL contain no filesystem path, credential, job
   identifier, provider prompt, backend tool name, template source, MCP transport detail,
   or internal worker detail.

### Requirement 19 — Regression compatibility with Spec 001

**User Story:** As a developer, I want Spec 001's proven behaviour to keep working, so that
new intelligence and a new backend do not cost existing correctness.

#### Acceptance Criteria

1. WHEN Spec 002 completes THEN the Spec 001 mandatory E2E scenario SHALL still pass in
   intent: "Move Cube 50 cm to the right." moves the cube to X = 0.50 m, saves, previews,
   and reports success.
2. WHEN Spec 002 completes THEN all Spec 001 failure-case E2E tests SHALL still pass:
   worker/Blender unavailable, invalid object, and lock conflict, each with a structured
   error and no project corruption.
3. WHEN Spec 002 completes THEN Spec 001's idempotency behaviour SHALL still hold.
4. WHEN the Blender backend changes THEN the migration SHALL be INCREMENTAL: the Spec 001
   implementation MAY remain as a fallback and as a test oracle until parity with the
   official-MCP-backed path is proven, and a destructive rewrite SHALL NOT be performed.
5. WHEN parity is proven for a capability THEN retiring the duplicated platform-owned
   implementation SHALL be a deliberate, separately reviewed step.
6. WHEN canonical contracts are extended THEN the language-neutral JSON Schemas SHALL
   remain the source of truth, with Python and TypeScript representations in parity and
   conformance tests passing.
7. WHEN Spec 002 completes THEN a fresh environment SHALL still install and import every
   package with no `PYTHONPATH`, and the frontend SHALL still type-check and build.
8. WHEN the preview configuration changes THEN preview determinism SHALL be preserved, and
   any recorded preview constant SHALL be re-baselined explicitly rather than an assertion
   being loosened.

---

## Out of scope (explicitly deferred)

These are deliberately **not** part of Spec 002:

- authentication, real users, production authorization
- billing, quotas, cost controls
- multi-user collaboration
- production deployment, TLS termination, cloud infrastructure
- multiple worker scheduling
- Three.js interactive scene, object selection by clicking
- live viewport streaming, WebRTC
- persistent long-term project memory, design-decision history
- version history or undo UI
- full material / node-graph authoring
- photorealistic final render workflow
- camera-relative direction interpretation
- shared-read / exclusive-write locking
- forking, vendoring or maintaining a Blender MCP implementation
- integrating a community Blender MCP (reference only, review-gated — Requirement 9.12)
- high-level architectural composition (walls, rooms, floor plans) — enabled by
  Requirement 17, delivered later
- uploaded floor plans and reference photos as model context — scoped by Requirement
  17.6, and deferred to Spec 003 unless Task 12 concludes otherwise

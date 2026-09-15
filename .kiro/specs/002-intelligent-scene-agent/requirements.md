# Requirements — 002 Intelligent Scene Agent

## Introduction

Spec 001 proved the vertical slice: one hard-coded sentence shape, interpreted by a
deterministic `RuleBasedProvider`, moving one cube. Every boundary works and is
regression-tested (see `001-core-vertical-slice/verification.md`).

Spec 002 makes the assistant **intelligent** (a real LLM), **scene-aware** (an
authoritative snapshot), and **capable** (real Blender operations) — and it does the
third of those by **reusing an existing Blender MCP implementation** rather than
building a second one.

### The reuse decision (refactor of this spec)

> AI 3D Design Studio does not reinvent Blender MCP.

An existing Blender MCP server is adopted as the Blender **capability engine**, behind
a platform-owned adapter. The initial target is
[`ahujasid/blender-mcp`](https://github.com/ahujasid/blender-mcp), pinned to an exact
version, reached over the real Model Context Protocol — never by importing its
internals and never by copying its source or its Blender add-on into this repository.

What the platform owns, and will keep owning:

product UX · scene grounding · identity, project and session context · LLM provider
abstraction · clarification · proposal validation · security policy · scene-version
preconditions · job durability · retry and idempotency · project locks · recovery
points · project isolation · previews and results · cloud-facing networking.

What the external MCP owns:

the Blender implementation — object manipulation, materials, rendering, asset
integrations, export.

### The governing principles

1. **The model gains language ability, not authority.** It proposes; the platform
   validates, resolves and executes.
2. **A discovered tool is not a permitted tool.** Tool discovery informs a
   platform-owned policy; the policy decides. Unknown tools are denied.
3. **The MCP is an integration boundary, not a library.** We speak protocol to it.

## Glossary

- **SceneSnapshot** — an authoritative, read-only, safe description of a project's
  current scene. Produced by NORMALISING external MCP read output; never
  model-authored, and never handed to a provider in raw MCP form.
- **Scene version** — a content digest of an explicitly enumerated, versioned
  projection of semantic scene state (`studio-scene-v1`, design §2.3), used for
  caching and as an **enforced execution precondition**.
- **Capability** — a platform-named ability (`inspect_scene`, `transform_object`,
  `render`, `import_asset`, …). Capabilities are what the agent reasons about;
  third-party tool names never reach it.
- **BlenderMcpGateway** — the platform boundary that speaks MCP to an external
  Blender MCP server and exposes capabilities.
- **McpToolPolicy** — the platform-owned classification of every discovered tool into
  allowed / guarded / denied, defaulting to denied.
- **Proposal** — the model's *untrusted* structured output.
- **Plan** — a *validated, resolved* AgentPlan the platform will execute.
- **Clarification** — a first-class agent outcome that asks a question and emits no
  Jobs.
- **Canonical angle** — radians (Requirement 7). **Canonical colour** — linear sRGB
  RGBA (Requirement 7).

---

## Requirements

### Requirement 1 — Authoritative scene grounding

**User Story:** As a user, I want the assistant to know what is actually in my
project, so that its answers and changes reflect reality rather than a guess.

#### Acceptance Criteria

1. WHEN the agent needs scene knowledge THEN the platform SHALL obtain a
   `SceneSnapshot` derived from the current Blender project state, not from model
   memory or a database summary.
2. WHEN a SceneSnapshot is produced THEN it SHALL be read-only with respect to the
   project: producing it SHALL NOT mutate the scene, save the project, create a
   recovery point, write a mutation journal record, or create a preview.
3. WHEN a SceneSnapshot is produced THEN it SHALL be built by NORMALISING the output
   of read-only external MCP capabilities. The platform SHALL NOT implement its own
   Blender scene-inspection engine when the external MCP already exposes scene and
   object inspection.
4. WHEN external MCP output is received THEN it SHALL be treated as UNTRUSTED
   integration data and validated before a `SceneSnapshot` is constructed from it.
5. WHEN one MCP read is insufficient THEN the adapter MAY compose several read-only
   MCP capabilities into one snapshot.
6. WHEN a provider receives scene context THEN it SHALL receive the canonical
   `SceneSnapshot` only. Raw MCP output SHALL NOT be exposed to `AgentProvider`.
7. WHEN a SceneSnapshot is produced THEN it SHALL carry `project_id`, the scene's unit
   configuration, a `scene_version` identity, and a list of objects.
8. WHEN an object appears in a SceneSnapshot THEN it SHALL include its stable
   `studio_object_id` where one exists, its name, object type, world position in
   metres, dimensions in metres, rotation, scale, a basic material summary, and safe
   visibility state.
9. WHEN a SceneSnapshot is serialized THEN it SHALL NOT contain a filesystem path, a
   project-file location, worker credentials, hostnames, MCP transport detail, or
   arbitrary Blender internals.
10. IF the scene cannot be read THEN the platform SHALL return a structured error and
    SHALL NOT fall back to an assumed or stale scene for a mutation decision.
11. WHEN a mutation succeeds THEN any cached SceneSnapshot for that project SHALL be
    invalidated.
12. WHEN the authoritative scene is read THEN the read SHALL hold the project's
    EXISTING execution lock for the duration of the inspection, so the snapshot
    describes ONE consistent project state.

    Rationale, recorded because it looks like a contradiction: a lock-free read could
    run while another execution is mutating and saving the same project and observe a
    torn scene. The snapshot is what the agent reasons against and what a plan's
    `scene_version` precondition is compared to, so a torn snapshot would poison both.
    A shared-read / exclusive-write lock is explicitly deferred, and a second locking
    mechanism SHALL NOT be introduced.
13. IF the project lock cannot be acquired for a read THEN the platform SHALL return
    the existing structured `LOCK_CONFLICT` outcome, SHALL NOT introduce a second lock
    error model, and SHALL NOT bypass or downgrade the lock.
14. WHEN a read completes, successfully or not, THEN the lock SHALL be released, and an
    unrelated project SHALL remain independently lockable throughout.
15. WHEN the same unchanged project is inspected twice THEN the two snapshots SHALL
    carry an identical `scene_version` and MAY carry different `captured_at` values.
16. WHEN a read job is retried THEN inspection MAY execute again, because a read has no
    mutation side effect and the caller wants CURRENT state. Mutation-style idempotency
    machinery SHALL NOT be used to return a cached older snapshot, and read retry
    behaviour SHALL NOT weaken mutation idempotency.

### Requirement 2 — Scene version identity and enforcement

**User Story:** As the platform, I want to know exactly which scene a plan was
reasoned against, so that a change is never applied to a scene that has moved on.

#### Acceptance Criteria

1. WHEN a mutation plan is produced from a SceneSnapshot THEN the plan SHALL record the
   authoritative `scene_version` it was reasoned against.
2. WHEN the first mutation of a plan is about to execute THEN the platform SHALL verify
   that the authoritative scene state still matches the plan's recorded
   `scene_version`, while the project lock is held, before any recovery point or
   mutation.
3. IF the authoritative scene version no longer matches THEN the platform SHALL NOT
   mutate Blender, SHALL return the canonical error `SCENE_VERSION_MISMATCH`, SHALL
   refresh the SceneSnapshot, and SHALL require re-resolution and re-planning.
4. WHEN a plan reasons about a relationship between objects (for example "move the
   chair closer to the table") THEN per-object `expected_before` verification SHALL NOT
   be treated as sufficient: the scene-version precondition SHALL also hold, because an
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
9. WHEN a new field is added to `SceneSnapshot` THEN it SHALL be classified explicitly
   as informational or planning-relevant, and an automated check SHALL fail if a field
   is left unclassified.

### Requirement 3 — Real LLM provider behind the existing abstraction

**User Story:** As the platform, I want a real language model reached only through
`AgentProvider`, so that intelligence improves without weakening any boundary.

#### Acceptance Criteria

1. WHEN a real provider is added THEN it SHALL implement the existing `AgentProvider`
   protocol and SHALL be reached only through the provider registry.
2. WHEN provider selection is configured THEN it SHALL be configuration, not code:
   switching providers SHALL require no change to FastAPI routes, `ChatService`, the
   worker, the MCP gateway, or the frontend.
3. WHEN Spec 002 completes THEN `RuleBasedProvider` SHALL remain available and
   selectable, so deterministic tests and offline development keep working.
4. WHEN credentials are required THEN they SHALL come from environment or configuration
   only, SHALL never be committed, logged, returned through the API, or placed in a
   `NEXT_PUBLIC_*` variable.
5. WHEN the default test suite runs THEN it SHALL NOT require network access, an API
   key, paid model calls, a running Blender, or a running external MCP server.
6. IF live-provider tests exist THEN they SHALL be explicitly opt-in and SHALL skip
   cleanly when credentials are absent.
7. WHEN a provider returns metadata THEN it SHALL NOT include chain-of-thought, hidden
   reasoning, or raw prompt contents.
8. IF the configured provider is unavailable or unauthenticated THEN the platform SHALL
   return a structured `PROVIDER_UNAVAILABLE` outcome and SHALL NOT fabricate a plan.
9. WHEN Spec 002 is declared product-complete THEN the configured **real** provider
   SHALL have been demonstrated, on the development environment, to satisfy the
   live-provider acceptance gate. Passing only with `FakeLlmProvider` SHALL NOT be
   sufficient.
10. WHEN the live-provider acceptance gate runs THEN the real provider SHALL
    successfully produce, end to end: a grounded `Answer`; a schema-valid proposal for a
    move that becomes a validated plan and a real verified Blender mutation; ordered
    validated operations for a two-part instruction; and a `Clarification` with zero
    mutations for an ambiguous instruction. These tests SHALL assert structured
    outcomes and safety boundaries, never exact prose.

### Requirement 4 — Constrained structured model output

**User Story:** As the platform owner, I want the model's output to be data the
platform validates, so that language never becomes execution authority.

#### Acceptance Criteria

1. WHEN the provider calls the model THEN it SHALL request structured output
   constrained to a declared schema of proposable **capabilities**.
2. WHEN model output is received THEN the platform SHALL validate it against that
   canonical schema BEFORE any resolution or execution.
3. WHEN model output is invalid, malformed, truncated, or unparseable THEN the platform
   SHALL return a structured error and SHALL emit no Jobs.
4. WHEN model output names a capability the platform does not implement, or one the
   policy does not permit, THEN the platform SHALL reject it and SHALL emit no Jobs.
5. WHEN model output is processed THEN the platform SHALL NEVER evaluate, execute, or
   shell out to any text the model produced, and SHALL NEVER forward model-authored
   text to an MCP tool that executes code.
6. WHEN model output includes fields the platform did not declare THEN those fields
   SHALL be rejected rather than ignored.
7. WHEN a proposal is accepted THEN the platform SHALL convert it into an internal plan
   through its own construction code, so the model never authors a Job.
8. WHEN the agent is given a tool catalogue THEN it SHALL contain PLATFORM capability
   names only. Third-party MCP tool names SHALL NOT appear in prompts, proposals,
   plans, Jobs, API responses, or the browser.

### Requirement 5 — Read-only questions

**User Story:** As a user, I want to ask about my project, so that I can understand it
without changing anything.

#### Acceptance Criteria

1. WHEN the user asks a question answerable from the SceneSnapshot THEN the platform
   SHALL return an `Answer`.
2. WHEN an `Answer` is produced THEN the platform SHALL create zero mutation Jobs.
3. WHEN an `Answer` is produced THEN the platform SHALL NOT modify the project, SHALL
   NOT save, and SHALL NOT generate a new preview.
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
6. WHEN clarification state is retained THEN it SHALL be short-lived and
   session-scoped, and SHALL NOT constitute persistent project memory.
7. IF a clarification is never answered THEN it SHALL expire without side effects.
8. WHEN a `Clarification` is returned THEN it SHALL be presented as a question, not as
   an error.

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
8. WHEN an operation crosses the MCP boundary THEN the adapter SHALL translate the
   resolved stable id into whatever identifier the external MCP requires, and that
   translation SHALL be the ONLY place the two identity schemes meet.

### Requirement 8 — Canonical units, angles and colour

**User Story:** As the platform, I want one canonical representation per quantity, so
that conversion bugs cannot reach Blender or an external MCP.

#### Acceptance Criteria

1. WHEN distances or dimensions cross any boundary THEN they SHALL be in metres.
2. WHEN rotations cross any boundary THEN they SHALL be in **radians**.
3. WHEN a user expresses an angle in degrees THEN conversion to radians SHALL happen
   exactly once, in `packages/spatial`.
4. WHEN a user expresses a resize as a proportion THEN the canonical mutation SHALL
   carry absolute desired dimensions in metres, derived from the authoritative
   SceneSnapshot above the adapter. A percentage SHALL NEVER cross the MCP boundary.
5. WHEN an explicit transform-scale intent is expressed THEN it SHALL carry validated
   absolute unitless scale values.
6. WHEN a colour crosses any boundary THEN it SHALL be canonical linear sRGB RGBA in
   `[0, 1]`; a colour NAME SHALL NEVER cross the MCP boundary.
7. WHEN unit, angle, percentage or colour conversion logic exists THEN it SHALL exist
   only in `packages/spatial`, enforced by the source guard.
8. WHEN world-space directions are interpreted THEN Spec 001's mapping SHALL hold:
   right +X, left −X, forward +Y, back −Y, up +Z, down −Z; camera-relative
   interpretation remains deferred.
9. IF an external MCP capability expects a different unit or colour space THEN the
   adapter SHALL convert at the boundary using `packages/spatial`, and the conversion
   SHALL be documented and tested.

### Requirement 9 — Blender capabilities are provided by an external MCP

**User Story:** As the product owner, I want Blender ability to come from an existing
maintained MCP implementation, so that we build product instead of re-implementing
Blender.

#### Acceptance Criteria

1. WHEN Blender work is performed THEN it SHALL be performed by an external Blender MCP
   implementation reached through the real Model Context Protocol.
2. WHEN the platform integrates that implementation THEN it SHALL NOT fork it, copy its
   source into this repository, duplicate its Blender add-on, or import its private
   Python modules as if they were a local library.
3. WHEN the worker needs Blender ability THEN it SHALL talk ONLY to the platform-owned
   `BlenderMcpGateway` abstraction, so a different compatible MCP implementation can be
   substituted later.
4. WHEN capabilities are named THEN they SHALL be PLATFORM capability names mapped to
   external tool names (or ordered tool sequences) inside the adapter. `AgentProvider`,
   FastAPI routes, `JobFactory`, canonical Jobs and the frontend SHALL NOT reference
   implementation-specific tool names.
5. WHEN a capability is unavailable in the configured implementation THEN the platform
   SHALL report a structured, capability-named failure and SHALL NOT approximate it with
   a code-execution tool.
6. WHEN the platform needs a Blender ability the external MCP already provides THEN the
   platform SHALL NOT re-implement it. This explicitly covers object creation and
   manipulation, transforms, materials, rendering, screenshots, import, export and asset
   integration.
7. WHEN platform-owned operation code exists around a capability THEN it SHALL be
   limited to OUR safety semantics: scene-version checks, expected-before checks,
   desired-after targets, idempotent-retry detection, and result verification. It SHALL
   NOT constitute a second Blender implementation.
8. WHEN the external implementation is selected THEN it SHALL be pinned to an exact
   package version or immutable commit; `latest` SHALL NOT be used.
9. WHEN the external implementation is pinned THEN the repository SHALL record the
   implementation, the version or commit, the discovered tool inventory with schemas,
   and the compatibility assumptions relied upon.
10. WHEN an upstream update changes the tool inventory or a tool schema THEN a
    compatibility test SHALL FAIL rather than the platform silently adapting.
11. WHEN the external MCP add-on and server must agree on a protocol version THEN both
    SHALL be pinned together and their handshake SHALL be verified at startup.

### Requirement 10 — Platform-owned MCP tool policy

**User Story:** As the platform owner, I want to decide which third-party capabilities
the model may reach, so that adopting an MCP does not adopt its entire attack surface.

#### Acceptance Criteria

1. WHEN tools are discovered from an external MCP THEN discovery SHALL NOT imply
   permission. A platform-owned `McpToolPolicy` SHALL decide.
2. WHEN a discovered tool is not explicitly classified THEN it SHALL be DENIED. The
   policy SHALL fail closed.
3. WHEN the policy classifies a tool THEN it SHALL be exactly one of:
   - **Tier A — allowed by default:** safe semantic and modelling capabilities such as
     scene inspection, object information, ordinary object creation, transforms,
     material and colour operations, render and screenshot, and safe scene
     manipulation.
   - **Tier B — guarded:** capabilities reaching external resources or with broader
     effect, such as asset downloads, remote asset services, AI model generation,
     import, export and external URLs. These SHALL require explicit platform
     configuration to enable, SHALL be bounded to configured providers or domains where
     practical, and SHALL NOT accept a model-chosen filesystem destination.
   - **Tier C — disabled by default:** capabilities equivalent to arbitrary Python or
     Blender code execution, shell commands, subprocess launch, arbitrary file read or
     write, arbitrary network access, package installation, or persistent background
     code.
4. WHEN a Tier C tool exists in the external MCP THEN it SHALL NOT appear in the
   model's permitted capability catalogue by default, regardless of upstream safe-mode
   settings.
5. WHEN the external MCP offers a safe mode THEN it SHALL be enabled as
   defence-in-depth. Upstream safe mode SHALL NOT be treated as authorisation: the
   platform policy remains authoritative, and "safe mode is on, therefore arbitrary
   execution is safe" SHALL NOT appear in any design decision.
6. WHEN a denied or unconfigured tool is invoked THEN the platform SHALL refuse before
   any MCP call is made, and SHALL record the refusal.
7. WHEN the policy is evaluated THEN it SHALL be evaluated on the PLATFORM side, in the
   worker or control plane, and SHALL NOT depend on the external server enforcing it.
8. WHEN the permitted catalogue is assembled for a model THEN it SHALL be derived from
   the policy, so a model cannot name a tool it was never offered.

### Requirement 11 — Local MCP networking and exposure

**User Story:** As the platform owner, I want Blender and its MCP to remain
unreachable from outside the workstation, so that adopting a local listener does not
create an entry point.

#### Acceptance Criteria

1. WHEN Spec 001's "the workstation never listens" invariant is restated for Spec 002
   THEN it SHALL become the more precise invariant: **no Blender or MCP service on the
   workstation is externally reachable**.

   The external Blender MCP add-on requires a LOCAL listener, so an absolute
   "never binds a socket" rule is no longer accurate. The property that actually
   matters — nothing on the design machine is reachable from off the machine — is
   preserved and stated directly.
2. WHEN the external MCP or its add-on binds a socket THEN it SHALL bind loopback only
   (`127.0.0.1` / `localhost`). It SHALL NOT bind `0.0.0.0`, a LAN address, or a public
   address by default.
3. WHEN the platform is configured THEN a non-loopback Blender MCP host SHALL be
   rejected in the default production-safe configuration, and a configuration guard test
   SHALL enforce this.
4. WHEN the control plane is deployed THEN the Blender MCP or add-on port SHALL NOT be
   exposed through it, tunnelled by it, or forwarded by a router.
5. WHEN the browser runs THEN it SHALL have no direct access to the Blender MCP or its
   add-on port.
6. WHEN the worker connects to the control plane THEN that connection SHALL remain
   OUTBOUND and authenticated, exactly as in Spec 001.
7. WHEN the worker connects to the external MCP server THEN it SHALL do so as a local
   MCP client over a local transport (stdio-launched child process preferred), and the
   MCP server SHALL NOT be exposed to any network.
8. WHEN the add-on's local socket has no authentication THEN the platform SHALL treat
   loopback binding plus single-tenant workstation assumptions as the boundary, SHALL
   document that assumption, and SHALL NOT rely on the add-on for authorisation.

### Requirement 12 — Durable mutation guard around non-idempotent MCP tools

**User Story:** As a user, I want a crash never to double-apply a change, even though
the external MCP does not promise retry safety.

#### Acceptance Criteria

1. WHEN a mutation capability is invoked THEN the platform SHALL assume the external
   tool is NOT retry-idempotent and SHALL provide idempotency itself.
2. BEFORE an MCP mutation is invoked THEN the platform SHALL durably persist: the
   intended operation, the required `scene_version`, the expected-before state, the
   desired-after state, and a recovery point where applicable.
3. AFTER an MCP mutation is invoked THEN the platform SHALL inspect the actual
   resulting state through a read capability, verify it against the desired-after
   state, and durably record the result.
4. WHEN a mutation is retried after a crash THEN the platform SHALL FIRST inspect the
   current Blender state, and:
   - IF the desired-after state is already present THEN it SHALL mark the operation
     `already_applied` and SHALL NOT invoke the MCP mutation again;
   - ELSE IF the current state matches expected-before THEN it SHALL invoke the MCP
     mutation;
   - ELSE it SHALL return `PRECONDITION_MISMATCH` or `SCENE_VERSION_MISMATCH` as
     appropriate and SHALL mutate nothing.
5. WHEN verification fails THEN the platform SHALL NOT report success, and the recovery
   point SHALL be preserved.
6. WHEN mutation identity is derived THEN it SHALL remain
   `project_id + request_id + operation_index`.
7. WHEN the platform's guard wrapper is described THEN it SHALL be understood as OUR
   safety semantics rather than a re-implementation of the MCP, and SHALL contain no
   Blender geometry logic.

### Requirement 13 — Multi-operation requests

**User Story:** As a user, I want to give one instruction containing several changes,
so that I do not have to type them separately.

#### Acceptance Criteria

1. WHEN one request implies several changes THEN the platform SHALL produce an ordered
   `AgentPlan` whose operations carry `operation_index` 0, 1, 2, … in execution order.
2. WHEN a plan contains several operations THEN all resulting Jobs SHALL share the
   originating `request_id`.
3. WHEN a multi-operation request is retried verbatim THEN no operation SHALL be applied
   a second time.
4. WHEN a multi-operation request is retried after partial completion THEN only the
   operations not yet applied SHALL execute.
5. WHEN a genuinely new `request_id` carries the same instruction THEN the changes SHALL
   be applied again intentionally.
6. WHEN operations execute THEN they SHALL execute in `operation_index` order.
7. IF an operation fails THEN the platform SHALL stop, SHALL NOT execute later
   operations, and SHALL report per-operation status distinguishing applied, failed and
   not-attempted.
8. WHEN a plan partially fails THEN the platform SHALL NOT claim overall success and
   SHALL NOT silently roll back applied operations; the documented semantics are
   sequential, non-atomic and resumable.
9. WHEN operation 0 executes THEN its scene-version precondition SHALL be the version
   the plan was reasoned against; a later operation's precondition SHALL be the version
   resulting from its predecessor, so an intentional change does not reject the next
   step while an EXTERNAL change still does.

### Requirement 14 — Safety and threat resistance

**User Story:** As the platform owner, I want untrusted language to be unable to escape
the capability boundary, so that adding an LLM and a third-party MCP does not add an
attack surface.

#### Acceptance Criteria

1. WHEN a user message contains an injection attempt THEN it SHALL NOT cause shell
   execution, Python evaluation, subprocess launch, filesystem access, or an MCP
   code-execution call.
2. WHEN model output is processed THEN it SHALL NOT be able to introduce a new worker
   protocol message type.
3. WHEN model output is processed THEN it SHALL NOT be able to supply a filesystem path,
   a project-file location, an MCP host or port, or an external URL.
4. WHEN model output is processed THEN it SHALL NOT be able to override the trusted
   `project_id`, `user_id` or `session_id`.
5. WHEN model output is processed THEN it SHALL NOT be able to bypass object resolution
   against the SceneSnapshot, or to name an MCP tool directly.
6. WHEN prompts are constructed THEN they SHALL contain no credentials, no filesystem
   paths, no worker token, and no third-party tool names.
7. WHEN Spec 002 completes THEN these Spec 001 invariants SHALL still hold: the
   workstation is not externally reachable (Requirement 11), the worker protocol
   vocabulary remains closed, no arbitrary-execution capability is offered to the model,
   and jobs and artifacts remain project-scoped with no unscoped routes.
8. WHEN the model requests an unavailable or denied capability THEN the platform SHALL
   refuse with a structured outcome rather than approximating it.

### Requirement 15 — Third-party dependency governance

**User Story:** As the platform owner, I want the external MCP's own behaviour — data
collection, licences, network calls — to be a reviewed decision rather than a surprise.

#### Acceptance Criteria

1. WHEN an external MCP is adopted THEN its licence SHALL be recorded and its
   compatibility with this project SHALL be confirmed.
2. WHEN the external MCP collects telemetry THEN the platform SHALL disable it by
   default and SHALL verify the disabling in configuration tests.

   This is a hard requirement, not a preference: the target implementation's telemetry
   is ON by default and collects prompts, generated code, screenshots and scene data,
   which for this product would mean users' private design work leaving the machine.
3. WHEN a tool parameter exists solely to capture the user's verbatim words for
   third-party analytics THEN the platform SHALL NOT populate it with user content.
4. WHEN the external MCP can reach external services THEN those services SHALL be
   disabled unless explicitly configured (Requirement 10, Tier B).
5. WHEN the external MCP requires its own credentials for a Tier B service THEN those
   credentials SHALL be supplied by platform configuration, never by a model, and never
   committed.
6. WHEN the external MCP version is upgraded THEN the compatibility test, the tool
   inventory record and the policy classification SHALL be re-reviewed before the
   upgrade is accepted.

### Requirement 16 — Long-term design capability is not foreclosed

**User Story:** As the product owner, I want Spec 002's architecture to leave room for
real design work, so that we are not locked forever into a handful of hand-written
operations.

#### Acceptance Criteria

1. WHEN the capability registry is designed THEN it SHALL be extensible to at least:
   scene inspection, object inspection, object creation, transforms, material editing,
   render, screenshot, asset import and export.
2. WHEN a new capability is added THEN it SHALL require a registry entry, a policy
   classification and an adapter mapping — and SHALL NOT require a new bespoke Blender
   implementation where the external MCP already provides one.
3. WHEN the product roadmap is considered THEN the design SHALL NOT structurally
   prevent: uploaded floor plans, reference photos, elevations and furniture photos;
   modelling interiors and buildings; placing furniture; applying materials; importing
   assets; and rendering.
4. WHEN file and reference ingestion is scoped THEN it SHALL be either delivered in
   Spec 002 or explicitly deferred to Spec 003 with the boundary recorded.

### Requirement 17 — Browser experience

**User Story:** As a user, I want one coherent reply per thing I say, so that the
conversation stays readable even when the system does several things internally.

#### Acceptance Criteria

1. WHEN the user sends one message THEN the transcript SHALL show exactly one user entry
   and exactly one Studio reply slot for it.
2. WHEN one request produces several internal Jobs THEN the browser SHALL still show one
   Studio reply, not one per operation.
3. WHEN the outcome is an `Answer` THEN the browser SHALL render it as an assistant
   reply with no mutation progress and no preview change.
4. WHEN the outcome is a `Clarification` THEN the browser SHALL render it as a question,
   answerable in the normal input.
5. WHEN a mutation is in progress THEN the browser SHALL show progress and SHALL show
   final success or a readable failure.
6. WHEN a multi-operation plan partially fails THEN the browser SHALL communicate what
   was applied and what was not, without claiming overall success.
7. WHEN a request is refused because the scene changed underneath it THEN the browser
   SHALL explain that the project changed and that nothing was modified.
8. WHEN a preview is produced THEN the browser SHALL display it, and a colour change
   SHALL be visible in the displayed preview.
9. WHEN anything is rendered THEN it SHALL contain no filesystem path, credential, job
   identifier, provider prompt, third-party tool name, MCP transport detail, or internal
   worker detail.

### Requirement 18 — Regression compatibility with Spec 001

**User Story:** As a developer, I want Spec 001's proven behaviour to keep working, so
that new intelligence and a new capability engine do not cost existing correctness.

#### Acceptance Criteria

1. WHEN Spec 002 completes THEN the Spec 001 mandatory E2E scenario SHALL still pass in
   intent: "Move Cube 50 cm to the right." moves the cube to X = 0.50 m, saves,
   previews, and reports success.
2. WHEN Spec 002 completes THEN all Spec 001 failure-case E2E tests SHALL still pass:
   worker/Blender unavailable, invalid object, and lock conflict, each with a structured
   error and no project corruption.
3. WHEN Spec 002 completes THEN Spec 001's idempotency behaviour SHALL still hold.
4. WHEN the Blender capability engine changes THEN the migration SHALL be INCREMENTAL:
   the existing implementation MAY remain as a fallback and as a test oracle until
   parity with the MCP-backed path is proven, and a destructive rewrite SHALL NOT be
   performed.
5. WHEN parity is proven for a capability THEN retiring the duplicated platform-owned
   implementation SHALL be a deliberate, separately reviewed step.
6. WHEN canonical contracts are extended THEN the language-neutral JSON Schemas SHALL
   remain the source of truth, with Python and TypeScript representations in parity and
   conformance tests passing.
7. WHEN Spec 002 completes THEN a fresh environment SHALL still install and import every
   package with no `PYTHONPATH`, and the frontend SHALL still type-check and build.
8. WHEN the preview configuration changes THEN preview determinism SHALL be preserved,
   and any recorded preview constant SHALL be re-baselined explicitly rather than an
   assertion being loosened.

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
- full material/node graph authoring by the platform (the external MCP's own material
  capabilities are used instead)
- photorealistic final render workflow
- camera-relative direction interpretation
- shared-read / exclusive-write locking
- forking or maintaining a Blender MCP implementation
- uploaded floor plans and reference photos as model context — scoped by Requirement
  16.4, and deferred to Spec 003 unless Task 12 concludes otherwise

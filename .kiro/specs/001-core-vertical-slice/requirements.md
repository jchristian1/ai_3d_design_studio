# Requirements — 001 Core Vertical Slice

## Introduction

This spec defines the first mandatory vertical slice of the AI 3D Design Studio.
It proves the end-to-end flow through every logical boundary of the system with a
single, concrete user action: moving an object in a Blender scene by a natural-language
instruction.

The target scenario (from `architecture.md` and `testing.md`):

> User enters: "Move Cube 50 cm to the right."

Flow:

```
Browser → API → AgentProvider → MCP → Blender Worker → Blender
       → save → preview → API → Browser
```

The slice must exercise real boundaries even if several components initially run in one
process (per `structure.md`). The goal is a thin but complete path, not feature breadth.

## Glossary

- **Slice**: The minimal end-to-end path proving all boundaries connect.
- **Canonical unit**: Meters. `50 cm = 0.50 m` (per `blender.md`).
- **Recovery point**: A pre-operation snapshot enabling rollback.
- **Preview**: A viewport screenshot or GLB representing current scene state.

## Requirements

### Requirement 1 — Natural-language move request from the browser

**User Story:** As a non-technical user, I want to type "Move Cube 50 cm to the right"
in the browser chat, so that the object moves without me touching Blender, Python, or a terminal.

#### Acceptance Criteria

1. WHEN the user submits a chat message THEN the web app SHALL send the request to the API with `project_id` and `session_id`.
2. WHEN the request is in flight THEN the web app SHALL display a pending/working state.
3. WHEN the operation completes successfully THEN the web app SHALL display a success message and the updated preview.
4. IF the operation fails THEN the web app SHALL display a human-readable failure message and SHALL NOT display a stale success state.

### Requirement 2 — API control-plane request handling

**User Story:** As the platform, I want the API to authenticate, scope, and route the request, so that only authorized project work is performed.

#### Acceptance Criteria

1. WHEN the API receives a chat request THEN it SHALL validate the request against a shared contract (`packages/contracts`).
2. WHEN the request is valid THEN the API SHALL create a job explicitly bound to the `project_id` (per `security.md` project isolation).
3. WHEN the request is missing `project_id` THEN the API SHALL reject it with a validation error.
4. WHEN the job is created THEN the API SHALL forward it to the AgentProvider abstraction, not to a specific model directly.

### Requirement 3 — Agent reasoning through AgentProvider

**User Story:** As the platform, I want the agent to interpret the instruction and select the correct MCP tool, so that intent maps to a safe semantic operation.

#### Acceptance Criteria

1. WHEN the agent receives "Move Cube 50 cm to the right" THEN it SHALL resolve this to the MCP tool `move_object` with `delta_x = +0.50` meters.
2. WHEN interpreting distances THEN the agent SHALL convert to canonical meters (`50 cm → 0.50 m`).
3. WHEN "to the right" is interpreted THEN it SHALL map to positive X (documented convention for this slice).
4. WHEN the agent is invoked THEN it SHALL be reached only through the `AgentProvider` interface, allowing provider substitution.
5. IF the target object cannot be resolved THEN the agent SHALL return a structured error rather than guessing.

### Requirement 4 — MCP semantic move operation

**User Story:** As the platform, I want a focused `move_object` MCP tool, so that Blender is modified through a safe semantic interface rather than arbitrary Python.

#### Acceptance Criteria

1. WHEN `move_object` is called with a target and axis deltas THEN it SHALL apply the delta in meters to the resolved object.
2. WHEN the move is applied THEN the tool SHALL NOT rely on arbitrary `execute_python` as its interface (per `security.md`/`tech.md`).
3. WHEN invalid units or a non-existent object are supplied THEN the tool SHALL return a structured validation error.
4. WHEN the operation completes THEN the tool SHALL return the resulting object position for verification.

### Requirement 5 — Blender Worker executes safely

**User Story:** As the platform, I want the worker to run the operation with locking, recovery, save, and verification, so that project state stays correct and recoverable.

#### Acceptance Criteria

1. WHEN the worker receives a job THEN it SHALL acquire a project lock before modifying the `.blend` file.
2. WHEN starting an important change THEN the worker SHALL create a recovery point.
3. WHEN the move executes THEN the worker SHALL move `Cube` by +0.50 m on X.
4. WHEN the move completes THEN the worker SHALL verify the resulting scene state (not assume success from lack of exception, per `testing.md`).
5. WHEN verification passes THEN the worker SHALL save the project.
6. WHEN the operation finishes THEN the worker SHALL release the project lock, including on failure paths.
7. IF another job holds the lock THEN the worker SHALL report a lock conflict rather than proceed.

### Requirement 6 — Preview generation and return

**User Story:** As the user, I want to see the updated scene, so that I can confirm the change visually.

#### Acceptance Criteria

1. WHEN the save succeeds THEN the system SHALL generate an updated preview (viewport screenshot is acceptable for this slice, per `blender.md` progression).
2. WHEN the preview is generated THEN it SHALL be made available to the API and returned to the browser.
3. WHEN the browser receives the preview THEN it SHALL display the updated result.

### Requirement 7 — Worker/control-plane security boundary

**User Story:** As the platform owner, I want Blender to never be publicly exposed, so that the system stays secure.

#### Acceptance Criteria

1. WHEN the worker connects THEN it SHALL use an outbound authenticated connection to the control plane (no inbound public Blender ports).
2. WHEN operations are requested THEN Blender ports, Python console, and MCP endpoints SHALL NOT be publicly exposed.
3. WHEN secrets are needed THEN they SHALL be read from environment variables and never committed.

### Requirement 8 — End-to-end verification

**User Story:** As a developer, I want an automated E2E test of this slice, so that the boundary integration is protected against regressions.

#### Acceptance Criteria

1. WHEN the E2E test runs THEN it SHALL open a test project with a `Cube` at X = 0.
2. WHEN it sends "Move Cube 50 cm to the right" THEN the agent SHALL select the correct operation.
3. WHEN the worker executes THEN Blender SHALL move the cube and save.
4. WHEN complete THEN the test SHALL assert `Cube` X = 0.50 (within tolerance) AND that a preview updated AND that the browser-facing response reports success.
5. WHEN a failure case is simulated (Blender unavailable, lock conflict, invalid object) THEN the system SHALL report a structured failure without corrupting project state.

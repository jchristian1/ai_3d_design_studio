# Agent Orchestration (Spec 001, Task 7)

The replaceable AI-provider boundary, and the point where probabilistic language
becomes deterministic execution.

```
ChatRequest
    v
AgentProvider          <-- the ONLY thing the platform imports
    v
AgentPlan              <-- provider-neutral canonical semantics
    v
JobFactory
    v
canonical Job          <-- deterministic from here on
    v
Blender Worker
    v
Blender
```

Everything left of `AgentPlan` may be probabilistic and provider-specific.
Everything right of it is deterministic, contract-validated, and testable without
a model. That is the whole point of the boundary.

## Providers

```
AgentProvider (Protocol, provider.py)
├── RuleBasedProvider   deterministic, Spec 001 only, TEMPORARY
├── AstraProvider       boundary defined, NOT implemented
└── CodexProvider       boundary defined, NOT implemented
```

Obtain one through `studio_agent.providers.registry.get_provider(name)`. Nothing
outside `studio_agent.providers` should import a concrete class, so swapping the
language engine is configuration rather than a refactor.

### RuleBasedProvider is temporary

It exists for two reasons: to prove the vertical slice end to end, and to keep CI
and E2E runs byte-for-byte reproducible without calling a model API. It is **not**
the intended long-term language engine. It understands one sentence shape and
refuses everything else.

### Grammar

```
move <target> <number> <unit> [to the|to|toward the] <direction>
```

| Token | Accepted |
|---|---|
| target | one bare word, used verbatim as an object name (`Cube`) |
| number | non-negative integer or decimal (`50`, `0.5`) |
| unit | `cm`, `centimeter(s)`, `centimetre(s)`, `m`, `meter(s)`, `metre(s)` |
| direction | `right`, `left`, `up`, `down`, `forward(s)`, `back(ward(s))` |
| filler | optional leading `please`, optional trailing `.` |

Matched case-insensitively. Anything else returns `UNSUPPORTED_INSTRUCTION`.
There is no pronoun resolution (`move it`), no vague magnitude (`a little`), no
landmark reasoning (`toward the window`), and no camera-relative direction. A
wrong guess would silently mutate a user's design, so refusing is correct.

The provider maps **words to canonical tokens** and performs no arithmetic:
`packages/spatial` resolves `right` to world +X and `50 cm` to `0.50 m`.
Duplicating that math here would create a second source of truth for the most
safety-critical conversion in the system, and tests assert it is absent.

### How Astra / Codex plug in later

Each adapter implements the same `interpret(request, context) -> AgentResult` and
becomes selectable by name. Its responsibilities will be: build context from
`AgentContext`, call the model API with credentials from environment variables
only, translate the model's tool calls into canonical `PlannedOperation`s, and
return a structured error for anything it cannot resolve.

Today both return `PROVIDER_UNAVAILABLE`. They deliberately do not fabricate a
plan — a provider that silently returned an invented or empty plan would be far
worse than one that fails loudly. Tests assert `configured is False`, that no plan
is produced, and that neither module imports an HTTP client.

## AgentContext

Trusted identity, supplied by the control plane — never parsed from the message
and never chosen by the provider:

```python
AgentContext(user_id, project_id, session_id, selected_object_id=None)
```

Frozen, so an interpretation cannot mutate the identity it was handed. The richer
context from `agent-context.md` (project brief, constraints, decisions, scene
snapshot, session summary) is future work and is **not** stubbed here: persistent
project memory and context retrieval are out of scope for Task 7.

## AgentPlan

```python
AgentPlan(operations=(PlannedOperation(operation_type, payload), ...))
```

Contains resolved canonical semantics only. It does **not** contain unresolved
language (`"right"`, `"50 cm"`), provider-specific envelopes, chain-of-thought, or
identity. A plan cannot name a project — asserted structurally by a test over its
fields.

`operations` is ordered, and an operation's index **is** its `operation_index` for
job idempotency. Spec 001 produces exactly one operation, but nothing assumes that
permanently. Dependency graphs and conditional planning are out of scope.

`ProviderMetadata` exposes only `provider_name`, `provider_version` and
`operation_count`. No model reasoning is captured or persisted.

### Why no JSON Schema

`AgentPlan` is an internal Python boundary: provider and job factory are both
Python and currently in-process, and it never reaches the browser (the API returns
`ChatResponse`). A canonical schema plus a TypeScript mirror would add contract
machinery with no second consumer. If a provider is ever hosted out-of-process this
becomes a genuine wire contract and should get the full canonical treatment then.

## JobFactory

Maps each planned operation to a canonical Job via the **Task 3 builder**. No
idempotency hashing is reimplemented — a test asserts the resulting key equals
`derive_idempotency_key(project_id, request_id, operation_index)` and that this
module mentions no hashing at all.

Identity discipline:

- every identity comes from `AgentContext` and the trusted `ChatRequest`
- a request whose `project_id` disagrees with the context is refused
- a blank `user_id`, `project_id`, `session_id` or `request_id` prevents job
  creation
- `job_id` is derived deterministically as `job_<request_id>_<operation_index>` so
  a retry reuses the same job record rather than creating a second one

Retry semantics therefore fall out of Task 3 unchanged: same `request_id` → same
mutation identity; new `request_id` with an identical message → different identity,
so a user can legitimately repeat a command.

## What the agent may not do

The provider interprets language. It must not mutate Blender, import `bpy`, call
the worker, read project files, manage locks, create recovery copies, or execute
MCP tools. This is enforced by AST tests over every module in the package, which
reject imports of `bpy`, `blender_worker`, `blender_mcp`, `subprocess`, `shutil`,
`fcntl`, `os` and `pathlib`.

## Tests

```bash
python3 -m pytest services/agent      # fast, no Blender, no network
```

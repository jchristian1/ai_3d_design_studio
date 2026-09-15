# Blender Worker (Spec 001, Task 6)

Executes canonical Jobs against a real Blender project, safely and repeatably.

```
Job (Task 3)  ->  WorkerExecutor  ->  MoveObjectPlan (Task 4)  ->  Blender project (Task 5)
```

## Lifecycle

For a `move_object` job:

1. Validate the job against the canonical contract.
2. Reject unsupported `job_type`.
3. Resolve `project_id` to a trusted `.blend` via the project registry.
4. Acquire the project-scoped lock.
5. Load any existing execution record. A **corrupt** record aborts the run.
6. Duplicate checks: a completed record for this `job_id`, or the mutation
   identity already bound to a different `job_id`.
7. **First execution only:** read the target's current position, derive
   `expected_before` / `desired_after`, and persist the plan **before** mutating.
   **Retry:** reuse the persisted plan verbatim.
8. Create a recovery snapshot of the `.blend`.
9. Execute the Task 4 `move_object` operation (mutates and saves).
10. Confirm durability by re-reading the **saved** file in a fresh Blender process.
11. Record completion; release the lock.

### Internal phases vs public status

Internal phases are separate from the canonical Job lifecycle, so worker
internals never leak into a shared contract:

```
received -> plan_persisted -> recovery_created -> executing
         -> mutation_verified -> project_saved -> completed | failed
```

The record also carries `job_status`, which is the public value (`succeeded` /
`failed` / `running`). A test asserts the phase vocabulary and the Job status
enum do not overlap.

## Execution journal

`WorkerExecutionStore` is the abstraction; `FileSystemExecutionStore` is the local
implementation. No Redis, no PostgreSQL.

```
runtime/worker/
├── executions/<project_id>/<job_id>.json    execution records
├── idempotency/<project_id>/<key>.json      mutation-identity bindings
├── locks/<project_id>.lock                  project locks
└── recovery/<project_id>/<job_id>/...       pre-mutation snapshots
```

`runtime/` is git-ignored and deliberately **not** inside `tests/fixtures/`, which
holds committed fixture sources.

Writes are atomic: temp file in the same directory → `fsync` the file →
`os.replace` → `fsync` the directory. A crash leaves either the old record or the
new one, never a partial one.

A record that cannot be parsed, has an unknown `record_version`, is missing
required fields, or carries an unknown phase raises `JournalCorruptError` and the
worker **refuses to execute**. Silently treating corruption as "no record" would
be the worst possible behaviour: the worker would replan from an already-mutated
scene and double-move the object.

## Why a retry cannot recalculate `expected_before`

```
Unsafe                              Safe
------                              ----
attempt 1: read 0.0                 attempt 1: read 0.0, PERSIST plan
           move 0.0 -> 0.5                     move 0.0 -> 0.5, save
           save                     crash before recording completion
crash                               retry: LOAD the same plan {0.0 -> 0.5}
retry: read 0.5                            Task 4 sees current == desired_after
       plan 0.5 -> 1.0                     -> already_applied, no mutation
       Cube ends at 1.00  WRONG             Cube stays 0.50  CORRECT
```

This is why the plan is written to the journal before the mutation, and why
`WorkerExecutor` never calls `read_object_position` for planning once a plan
exists. A test asserts no scene read occurs before the mutation on a retry path.

### Planning scope limitation

`expected_before` is captured at first worker execution, under the project lock.
That is sufficient for a single-worker local slice. It is **not** collaborative
scene-version planning: a future architecture should build the plan against an
explicit scene version in the control plane so a stale plan can be rejected before
it reaches a worker. This implementation does not provide that.

## Crash windows

| Window | Situation | Behaviour |
|---|---|---|
| A | Plan persisted, crash before mutation | Retry loads the same plan, executes once |
| B | Mutation + save succeeded, completion unrecorded | Retry loads the same plan; Task 4 returns `already_applied`; no second mutation |
| C | Plan persisted, scene changed externally | Task 4 returns `PRECONDITION_MISMATCH`; no mutation, no guessing |
| D | Recovery created, mutation/verification failed | Structured failure recorded; **recovery evidence preserved** |

## Locking

`ProjectLockProvider` is the abstraction; `FileLockProvider` uses `fcntl.flock`
with one lock file per `project_id`. All Linux-specific code is confined to
`locks.py` — `executor.py` never imports `fcntl`, asserted by a test.

Locks are per project, so unrelated projects proceed independently. The lock is
released on every path, including failures, and the kernel releases it if the
process dies.

Limitation: an flock is single-machine. It is correct for one local worker and
will **not** coordinate multiple worker hosts. A distributed lock can be
substituted by implementing the same Protocol.

## Project path security

A Job never carries a filesystem path, and cannot:

- The canonical Job and `MoveObjectPayload` schemas declare
  `additionalProperties: false` and have no path field — a path is
  unrepresentable, not merely ignored.
- `project_id` must be a safe single path segment; `../../etc/passwd`, `..`,
  `a/b` and absolute paths are rejected before touching the filesystem.
- The registry only resolves ids it registered itself, and re-checks after
  symlink resolution that the result is inside its allowed root.
- An unknown `project_id` is refused; the worker never searches for a plausible
  project.

## Recovery

Before mutating, the worker copies the `.blend` to
`runtime/worker/recovery/<project_id>/<job_id>/attemptNNN-<timestamp>-<name>.blend`
and records the path plus the source SHA-256 in the journal. The attempt number
and timestamp mean an earlier attempt's evidence is never overwritten.

On failure the snapshot is **kept**. Nothing deletes recovery evidence
automatically.

## Blender boundary

`BlenderOperationExecutor` has two implementations:

- `FakeBlenderOperationExecutor` — in-memory, used by the fast tests. It runs the
  **real** Task 4 service against a fake scene adapter, so the retry semantics
  under test are the production semantics with only bpy replaced.
- `SubprocessBlenderOperationExecutor` — spawns headless Blender per operation via
  the centralized `blender_mcp.blender_runtime` resolver. No Blender path appears
  in the worker.

Limitation: one subprocess per operation costs roughly a second per call and
cannot keep a scene in memory across steps. A persistent Blender process can
replace it behind the same Protocol without touching `WorkerExecutor`.

## Tests

```bash
python3 -m pytest services/blender-worker     # fast, fakes only
python3 -m pytest -m blender                  # real Blender, opt-in
```

"""Internal worker execution phases.

Spec 001, Task 6.

These are deliberately SEPARATE from the canonical Job lifecycle. The public Job
status (queued / claimed / running / succeeded / failed) is a contract the browser
and control plane depend on; overloading it with worker internals would leak
implementation detail into a shared contract and force a schema change every time
the worker grows a step.

The phase is what makes crash recovery possible: it records exactly how far the
previous attempt got, so a retry knows whether a plan exists, whether a recovery
point exists, and whether the mutation was already verified.
"""

from __future__ import annotations

from typing import Final

#: A record exists; nothing has been decided yet.
RECEIVED: Final = "received"
#: The MoveObjectPlan is durably on disk. Mutation may now be attempted.
PLAN_PERSISTED: Final = "plan_persisted"
#: A recovery copy of the .blend exists for this execution.
RECOVERY_CREATED: Final = "recovery_created"
#: The mutation has been handed to Blender. Outcome unknown — this is the phase a
#: crash is most likely to leave behind.
EXECUTING: Final = "executing"
#: Blender reported the mutation applied (or already applied) and verified.
MUTATION_VERIFIED: Final = "mutation_verified"
#: The new position was confirmed present in the saved .blend by a fresh read.
PROJECT_SAVED: Final = "project_saved"
#: Terminal success.
COMPLETED: Final = "completed"
#: Terminal failure.
FAILED: Final = "failed"

EXECUTION_PHASES: Final[tuple[str, ...]] = (
    RECEIVED,
    PLAN_PERSISTED,
    RECOVERY_CREATED,
    EXECUTING,
    MUTATION_VERIFIED,
    PROJECT_SAVED,
    COMPLETED,
    FAILED,
)

TERMINAL_PHASES: Final[tuple[str, ...]] = (COMPLETED, FAILED)

#: Phases after which the plan is guaranteed to exist and MUST be reused rather
#: than recomputed. Recomputing expected_before on a retry is how double
#: movement happens.
PHASES_WITH_PLAN: Final[tuple[str, ...]] = (
    PLAN_PERSISTED,
    RECOVERY_CREATED,
    EXECUTING,
    MUTATION_VERIFIED,
    PROJECT_SAVED,
    COMPLETED,
)


def is_terminal(phase: str) -> bool:
    return phase in TERMINAL_PHASES

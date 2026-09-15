"""The move_object semantic operation — domain logic.

Spec 001, Task 4.

This module contains no bpy, no MCP transport, and no I/O. It depends only on the
SceneAdapter Protocol, so its behaviour is fully testable with an in-memory fake.

Retry safety
------------
The canonical Job stores a relative ``delta_meters``, which is the right semantic
representation. But a relative Blender mutation is unsafe to replay: if the move
is applied and the .blend saved, and the worker then dies before recording
completion, replaying ``location += delta`` would move the object twice.

So execution is driven by a plan carrying absolute endpoints
(``expected_before_meters``, ``desired_after_meters``) and follows exactly three
paths:

    current ~= desired_after   -> ALREADY APPLIED. Report success, mutate nothing.
    current ~= expected_before -> APPLY. Write the absolute destination, re-read,
                                  verify, and only then report success.
    otherwise                  -> CONFLICT. Report PRECONDITION_MISMATCH and
                                  mutate nothing. Never re-derive the delta from
                                  an unexpected position.

"~=" is the single documented tolerance in ``tolerance.py``, used identically for
all three comparisons.

Resolution boundary
-------------------
This layer receives explicit canonical meters only. It never sees "right",
"50 cm", or any natural-language phrase; direction and unit resolution happened
upstream in packages/spatial and the agent.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from studio_contracts import ChatError
from studio_types import MoveObjectPlan, MoveObjectResult, ObjectRef, Vec3

from ..adapters.scene import SceneAdapter, SceneAdapterError, SceneObjectState
from ..tolerance import is_finite_position, positions_equal

MOVE_MESSAGES: dict[str, str] = {
    "MALFORMED_TARGET": (
        "target must provide a non-blank object_id or name"
    ),
    "OBJECT_NOT_FOUND": "no object in the scene matches the requested target",
    "NON_FINITE_PLAN": (
        "plan coordinates must be finite numbers of meters"
    ),
    "NON_FINITE_SCENE": (
        "the object's current position is not a finite number of meters"
    ),
    "INCONSISTENT_PLAN": (
        "desired_after_meters must equal expected_before_meters + delta_meters"
    ),
    "PRECONDITION_MISMATCH": (
        "the object is at neither the expected position nor the desired "
        "position; the scene changed since the plan was made, so no movement "
        "was applied"
    ),
    "MUTATION_FAILED": "the scene rejected the position write",
    "VERIFY_FAILED": (
        "the position read back after the write does not match the desired "
        "position"
    ),
    "UNREADABLE_AFTER_WRITE": (
        "the object's position could not be read back after the write"
    ),
}


def _vec_sum(a: Vec3, b: Vec3) -> Vec3:
    return Vec3(a.x + b.x, a.y + b.y, a.z + b.z)


def _is_valid_ref(ref: Any) -> bool:
    object_id = getattr(ref, "object_id", None)
    name = getattr(ref, "name", None)
    for value in (object_id, name):
        if isinstance(value, str) and value.strip():
            return True
    return False


def _failure(
    plan: MoveObjectPlan,
    code: str,
    message: str,
    previous: Optional[Vec3] = None,
    resolved: Optional[ObjectRef] = None,
) -> MoveObjectResult:
    """Build a failure result.

    applied and verified are always False here — the canonical result schema
    forbids a failure that claims otherwise, so this is the only way to fail.
    """
    return MoveObjectResult(
        job_id=plan.job_id,
        target=plan.target,
        requested_delta_meters=plan.delta_meters,
        applied=False,
        already_applied=False,
        verified=False,
        resolved_object=resolved,
        previous_position_meters=previous,
        error=ChatError(code=code, message=message),
    )


def move_object(plan: MoveObjectPlan, scene: SceneAdapter) -> MoveObjectResult:
    """Execute one retry-safe absolute move.

    Never raises for expected failure modes: every outcome is a structured
    MoveObjectResult so the caller decides from flags and error codes rather than
    from an exception or a human-readable string.
    """
    # ---- validate the plan itself ---------------------------------------
    if not _is_valid_ref(plan.target):
        return _failure(
            plan, "VALIDATION_ERROR", MOVE_MESSAGES["MALFORMED_TARGET"]
        )

    for position in (
        plan.expected_before_meters,
        plan.delta_meters,
        plan.desired_after_meters,
    ):
        if not is_finite_position(position):
            return _failure(
                plan, "INVALID_UNITS", MOVE_MESSAGES["NON_FINITE_PLAN"]
            )

    # The plan's three fields must agree. A plan whose desired_after does not
    # equal expected_before + delta is internally inconsistent, and trusting
    # either half would move the object to the wrong place.
    if not positions_equal(
        _vec_sum(plan.expected_before_meters, plan.delta_meters),
        plan.desired_after_meters,
    ):
        return _failure(
            plan, "VALIDATION_ERROR", MOVE_MESSAGES["INCONSISTENT_PLAN"]
        )

    # ---- resolve the target ---------------------------------------------
    try:
        state: Optional[SceneObjectState] = scene.find_object(plan.target)
    except SceneAdapterError as exc:
        return _failure(plan, "BLENDER_UNAVAILABLE", str(exc))

    if state is None:
        return _failure(
            plan, "OBJECT_NOT_FOUND", MOVE_MESSAGES["OBJECT_NOT_FOUND"]
        )

    resolved = state.as_object_ref()
    current = state.position_meters

    if not is_finite_position(current):
        return _failure(
            plan,
            "INVALID_UNITS",
            MOVE_MESSAGES["NON_FINITE_SCENE"],
            resolved=resolved,
        )

    # ---- PATH 1: already applied ----------------------------------------
    # Checked before the expected-before comparison so a replay after a crash is
    # recognised as complete. Note that a zero-delta plan has
    # expected_before == desired_after and therefore lands here: the scene is
    # already where it should be, so nothing is written.
    if positions_equal(current, plan.desired_after_meters):
        return MoveObjectResult(
            job_id=plan.job_id,
            target=plan.target,
            requested_delta_meters=plan.delta_meters,
            applied=False,
            already_applied=True,
            verified=True,
            resolved_object=resolved,
            previous_position_meters=current,
            final_position_meters=current,
        )

    # ---- PATH 3 (guard): the scene moved under us ------------------------
    if not positions_equal(current, plan.expected_before_meters):
        return _failure(
            plan,
            "PRECONDITION_MISMATCH",
            MOVE_MESSAGES["PRECONDITION_MISMATCH"],
            previous=current,
            resolved=resolved,
        )

    # ---- PATH 2: apply ---------------------------------------------------
    if not state.movable:
        reason = state.immovable_reason or "object cannot be moved"
        return _failure(
            plan,
            "OBJECT_NOT_MOVABLE",
            reason,
            previous=current,
            resolved=resolved,
        )

    try:
        # Absolute write, never an increment. This is what makes the operation
        # idempotent with respect to the plan.
        scene.set_world_position(state.handle, plan.desired_after_meters)
    except SceneAdapterError as exc:
        return _failure(
            plan,
            "MUTATION_FAILED",
            f"{MOVE_MESSAGES['MUTATION_FAILED']}: {exc}",
            previous=current,
            resolved=resolved,
        )

    # ---- inspect and verify ---------------------------------------------
    # Absence of an exception is not evidence of success
    # (see .kiro/steering/testing.md): read the scene back and check it.
    try:
        final = scene.read_world_position(state.handle)
    except SceneAdapterError as exc:
        return _failure(
            plan,
            "VERIFY_FAILED",
            f"{MOVE_MESSAGES['UNREADABLE_AFTER_WRITE']}: {exc}",
            previous=current,
            resolved=resolved,
        )

    if final is None:
        return _failure(
            plan,
            "VERIFY_FAILED",
            MOVE_MESSAGES["UNREADABLE_AFTER_WRITE"],
            previous=current,
            resolved=resolved,
        )

    if not is_finite_position(final) or not positions_equal(
        final, plan.desired_after_meters
    ):
        return _failure(
            plan,
            "VERIFY_FAILED",
            MOVE_MESSAGES["VERIFY_FAILED"],
            previous=current,
            resolved=resolved,
        )

    return MoveObjectResult(
        job_id=plan.job_id,
        target=plan.target,
        requested_delta_meters=plan.delta_meters,
        applied=True,
        already_applied=False,
        verified=True,
        resolved_object=resolved,
        previous_position_meters=current,
        final_position_meters=final,
    )


def plan_from_delta(
    job_id: str,
    target: ObjectRef,
    expected_before_meters: Vec3,
    delta_meters: Vec3,
) -> MoveObjectPlan:
    """Build a plan from the Job's relative delta and an observed position.

    The Job keeps storing ``delta_meters`` (unchanged from Task 3); this derives
    the absolute endpoints the retry-safe execution needs. Deciding *when* to
    capture ``expected_before_meters``, and persisting the resulting plan, belongs
    to the worker task — this helper only performs the arithmetic.
    """
    return MoveObjectPlan(
        job_id=job_id,
        target=target,
        expected_before_meters=expected_before_meters,
        delta_meters=delta_meters,
        desired_after_meters=_vec_sum(expected_before_meters, delta_meters),
    )

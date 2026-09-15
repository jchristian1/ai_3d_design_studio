"""move_object domain tests — no Blender required (Spec 001, Task 4).

Every case runs against the in-memory FakeSceneAdapter, so the full decision
logic is exercised deterministically and fast. Real-Blender coverage lives in
tests/blender/ behind the `blender` marker.

Covers the eight required scenarios:
  1. fresh apply        5. verification failure
  2. retry / already    6. non-finite input
  3. scene conflict     7. zero movement
  4. object missing     8. Y and Z movement
"""

from __future__ import annotations

import pytest
from blender_mcp.adapters.fake_scene import FakeSceneAdapter
from blender_mcp.tolerance import (
    MAX_HIDDEN_DISPLACEMENT_M_AT_1KM,
    POSITION_ABSOLUTE_TOLERANCE_M,
    POSITION_RELATIVE_TOLERANCE,
    coordinates_equal,
    is_finite_position,
    positions_equal,
)
from blender_mcp.tools.move_object import move_object, plan_from_delta
from studio_types import MoveObjectPlan, ObjectRef, Vec3

ORIGIN = Vec3(0.0, 0.0, 0.0)
HALF_X = Vec3(0.5, 0.0, 0.0)


def cube_plan(
    expected_before: Vec3 = ORIGIN,
    delta: Vec3 = HALF_X,
    job_id: str = "job_1",
    target: ObjectRef | None = None,
) -> MoveObjectPlan:
    """The Spec 001 plan: move Cube from 0 by +0.50 m on X."""
    return plan_from_delta(
        job_id=job_id,
        target=target if target is not None else ObjectRef(name="Cube"),
        expected_before_meters=expected_before,
        delta_meters=delta,
    )


# ---------------------------------------------------------------------------
# 1. Fresh apply
# ---------------------------------------------------------------------------


def test_1_applies_once_and_verifies():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(), scene)

    assert result.applied is True
    assert result.already_applied is False
    assert result.verified is True
    assert result.error is None
    assert result.final_position_meters == Vec3(0.5, 0.0, 0.0)
    assert result.final_position_meters.x == 0.5
    assert result.previous_position_meters == ORIGIN
    assert result.requested_delta_meters == HALF_X
    assert result.job_id == "job_1"
    assert len(scene.writes) == 1, "exactly one write"
    # The write is absolute, not an increment.
    assert scene.writes[0] == ("Cube", Vec3(0.5, 0.0, 0.0))


def test_1_resolved_object_is_reported():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN, object_id="obj_8d83f")
    result = move_object(cube_plan(), scene)
    assert result.resolved_object == ObjectRef(object_id="obj_8d83f", name="Cube")


def test_1_target_can_be_resolved_by_stable_object_id():
    """Compatible with future stable IDs, not only names."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN, object_id="obj_8d83f")
    plan = cube_plan(target=ObjectRef(object_id="obj_8d83f"))
    result = move_object(plan, scene)
    assert result.applied is True
    assert result.final_position_meters.x == 0.5


# ---------------------------------------------------------------------------
# 2. Retry after successful application
# ---------------------------------------------------------------------------


def test_2_retry_after_success_does_not_move_again():
    """The crash/retry window: position already reached, completion not recorded."""
    scene = FakeSceneAdapter.with_object("Cube", Vec3(0.5, 0.0, 0.0))
    result = move_object(cube_plan(), scene)  # same plan as the first attempt

    assert result.already_applied is True
    assert result.applied is False
    assert result.verified is True
    assert result.error is None
    assert result.final_position_meters.x == 0.5, "still 0.50, not 1.00"
    assert scene.writes == [], "no mutation on the already-applied path"


def test_2_repeated_retries_remain_stable():
    """Executing the same plan many times must never accumulate movement."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    plan = cube_plan()

    first = move_object(plan, scene)
    assert first.applied is True

    for _ in range(10):
        again = move_object(plan, scene)
        assert again.already_applied is True
        assert again.applied is False
        assert again.final_position_meters.x == 0.5

    assert len(scene.writes) == 1, "only the first attempt wrote"
    assert scene.objects["Cube"].position_meters.x == 0.5


def test_2_already_applied_within_tolerance_of_float32_rounding():
    """A float32 round-trip must still count as already applied."""
    stored = Vec3(0.5000000001, 0.0, 0.0)
    scene = FakeSceneAdapter.with_object("Cube", stored)
    result = move_object(cube_plan(), scene)
    assert result.already_applied is True
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 3. Scene conflict
# ---------------------------------------------------------------------------


def test_3_scene_conflict_is_reported_and_nothing_moves():
    scene = FakeSceneAdapter.with_object("Cube", Vec3(0.25, 0.0, 0.0))
    result = move_object(cube_plan(), scene)

    assert result.error is not None
    assert result.error.code == "PRECONDITION_MISMATCH"
    assert result.applied is False
    assert result.already_applied is False
    assert result.verified is False
    assert scene.writes == [], "a conflict must not mutate the scene"
    assert scene.objects["Cube"].position_meters == Vec3(0.25, 0.0, 0.0)
    assert result.previous_position_meters == Vec3(0.25, 0.0, 0.0)


def test_3_conflict_never_applies_delta_from_unexpected_position():
    """Guessing from an unexpected position would produce 0.75, not 0.50."""
    scene = FakeSceneAdapter.with_object("Cube", Vec3(0.25, 0.0, 0.0))
    move_object(cube_plan(), scene)
    assert scene.objects["Cube"].position_meters.x == 0.25


@pytest.mark.parametrize(
    "unexpected",
    [
        Vec3(0.25, 0.0, 0.0),
        Vec3(-0.5, 0.0, 0.0),
        Vec3(1.0, 0.0, 0.0),
        Vec3(0.0, 0.5, 0.0),
        Vec3(0.5, 0.5, 0.0),
        Vec3(0.001, 0.0, 0.0),
    ],
)
def test_3_any_unexpected_position_conflicts(unexpected):
    scene = FakeSceneAdapter.with_object("Cube", unexpected)
    result = move_object(cube_plan(), scene)
    assert result.error is not None
    assert result.error.code == "PRECONDITION_MISMATCH"
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 4. Object missing / malformed target
# ---------------------------------------------------------------------------


def test_4_missing_object_reports_object_not_found():
    scene = FakeSceneAdapter()  # empty scene
    result = move_object(cube_plan(), scene)

    assert result.error is not None
    assert result.error.code == "OBJECT_NOT_FOUND"
    assert result.applied is False
    assert result.verified is False
    assert result.previous_position_meters is None


def test_4_wrong_name_does_not_fall_back_to_another_object():
    """No broad scene search: a near-miss name must not resolve."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(target=ObjectRef(name="Cube.001")), scene)
    assert result.error.code == "OBJECT_NOT_FOUND"
    assert scene.writes == []


def test_4_unknown_object_id_does_not_fall_back_to_name():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN, object_id="obj_8d83f")
    result = move_object(cube_plan(target=ObjectRef(object_id="obj_other")), scene)
    assert result.error.code == "OBJECT_NOT_FOUND"


@pytest.mark.parametrize(
    "target", [ObjectRef(), ObjectRef(name="   "), ObjectRef(object_id="")]
)
def test_4_malformed_object_ref_is_rejected(target):
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(target=target), scene)
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 5. Verification failure
# ---------------------------------------------------------------------------


def test_5_verification_failure_is_reported_not_silently_successful():
    """The scene accepts the write but reports a different position."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    scene.report_position_after_write = Vec3(0.4, 0.0, 0.0)

    result = move_object(cube_plan(), scene)

    assert result.error is not None
    assert result.error.code == "VERIFY_FAILED"
    assert result.applied is False, "must not claim success"
    assert result.verified is False
    assert len(scene.writes) == 1, "the write was attempted"


def test_5_unreadable_after_write_fails_verification():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)

    def vanish(handle):
        return None

    scene.read_world_position = vanish  # type: ignore[assignment]
    result = move_object(cube_plan(), scene)
    assert result.error.code == "VERIFY_FAILED"
    assert result.applied is False


def test_5_non_finite_position_after_write_fails_verification():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    scene.report_position_after_write = Vec3(float("nan"), 0.0, 0.0)
    result = move_object(cube_plan(), scene)
    assert result.error.code == "VERIFY_FAILED"
    assert result.applied is False


def test_5_mutation_failure_is_reported_as_mutation_failed():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    scene.fail_write_with = "scene is read-only"
    result = move_object(cube_plan(), scene)
    assert result.error.code == "MUTATION_FAILED"
    assert result.applied is False
    assert result.verified is False


def test_5_immovable_object_is_reported_before_any_write():
    scene = FakeSceneAdapter.with_object(
        "Cube", ORIGIN, movable=False, immovable_reason="location is locked on x"
    )
    result = move_object(cube_plan(), scene)
    assert result.error.code == "OBJECT_NOT_MOVABLE"
    assert "locked" in result.error.message
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 6. Non-finite input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
@pytest.mark.parametrize("field", ["expected_before", "delta"])
def test_6_non_finite_plan_input_is_rejected(bad, field):
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    kwargs = {"expected_before": ORIGIN, "delta": HALF_X}
    kwargs[field] = Vec3(bad, 0.0, 0.0)
    result = move_object(cube_plan(**kwargs), scene)

    assert result.error is not None
    assert result.error.code == "INVALID_UNITS"
    assert scene.writes == []


def test_6_non_finite_desired_after_is_rejected():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    plan = MoveObjectPlan(
        job_id="job_1",
        target=ObjectRef(name="Cube"),
        expected_before_meters=ORIGIN,
        delta_meters=HALF_X,
        desired_after_meters=Vec3(float("inf"), 0.0, 0.0),
    )
    result = move_object(plan, scene)
    assert result.error.code == "INVALID_UNITS"
    assert scene.writes == []


def test_6_non_finite_scene_position_is_rejected():
    scene = FakeSceneAdapter.with_object("Cube", Vec3(float("nan"), 0.0, 0.0))
    result = move_object(cube_plan(), scene)
    assert result.error.code == "INVALID_UNITS"
    assert scene.writes == []


def test_6_inconsistent_plan_is_rejected():
    """desired_after must equal expected_before + delta."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    plan = MoveObjectPlan(
        job_id="job_1",
        target=ObjectRef(name="Cube"),
        expected_before_meters=ORIGIN,
        delta_meters=HALF_X,
        desired_after_meters=Vec3(0.9, 0.0, 0.0),  # not 0 + 0.5
    )
    result = move_object(plan, scene)
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 7. Zero movement
# ---------------------------------------------------------------------------


def test_7_zero_movement_does_not_mutate():
    """expected_before == desired_after, so the scene is already correct."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(delta=Vec3(0.0, 0.0, 0.0)), scene)

    assert result.error is None
    assert result.already_applied is True
    assert result.applied is False
    assert result.verified is True
    assert result.final_position_meters == ORIGIN
    assert scene.writes == [], "zero movement must not write"


def test_7_zero_movement_from_nonzero_position():
    scene = FakeSceneAdapter.with_object("Cube", Vec3(0.5, 0.0, 0.0))
    result = move_object(
        cube_plan(expected_before=Vec3(0.5, 0.0, 0.0), delta=Vec3(0.0, 0.0, 0.0)),
        scene,
    )
    assert result.already_applied is True
    assert scene.writes == []


def test_7_zero_movement_from_wrong_position_still_conflicts():
    """A no-op plan must not paper over a scene that moved."""
    scene = FakeSceneAdapter.with_object("Cube", Vec3(0.25, 0.0, 0.0))
    result = move_object(cube_plan(delta=Vec3(0.0, 0.0, 0.0)), scene)
    assert result.error.code == "PRECONDITION_MISMATCH"
    assert scene.writes == []


def test_7_negative_zero_delta_behaves_as_zero():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(delta=Vec3(-0.0, -0.0, -0.0)), scene)
    assert result.already_applied is True
    assert scene.writes == []


# ---------------------------------------------------------------------------
# 8. Y and Z movement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected",
    [
        (Vec3(0.5, 0.0, 0.0), Vec3(0.5, 0.0, 0.0)),
        (Vec3(-0.5, 0.0, 0.0), Vec3(-0.5, 0.0, 0.0)),
        (Vec3(0.0, 1.0, 0.0), Vec3(0.0, 1.0, 0.0)),
        (Vec3(0.0, -1.0, 0.0), Vec3(0.0, -1.0, 0.0)),
        (Vec3(0.0, 0.0, 2.4), Vec3(0.0, 0.0, 2.4)),
        (Vec3(0.0, 0.0, -2.4), Vec3(0.0, 0.0, -2.4)),
        (Vec3(0.5, 1.0, 2.4), Vec3(0.5, 1.0, 2.4)),
    ],
    ids=["+X", "-X", "+Y", "-Y", "+Z", "-Z", "XYZ"],
)
def test_8_world_space_movement_on_every_axis(delta, expected):
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(delta=delta), scene)

    assert result.applied is True
    assert result.verified is True
    assert result.final_position_meters == expected
    assert scene.objects["Cube"].position_meters == expected


def test_8_movement_from_a_nonzero_start_is_absolute():
    start = Vec3(1.0, 2.0, 3.0)
    scene = FakeSceneAdapter.with_object("Cube", start)
    result = move_object(
        cube_plan(expected_before=start, delta=Vec3(0.5, 0.0, 0.0)), scene
    )
    assert result.applied is True
    assert result.final_position_meters == Vec3(1.5, 2.0, 3.0)
    assert scene.writes[0][1] == Vec3(1.5, 2.0, 3.0)


def test_8_retry_on_a_y_axis_move_is_also_safe():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    plan = cube_plan(delta=Vec3(0.0, 1.0, 0.0))
    assert move_object(plan, scene).applied is True
    retry = move_object(plan, scene)
    assert retry.already_applied is True
    assert scene.objects["Cube"].position_meters.y == 1.0


# ---------------------------------------------------------------------------
# The single tolerance rule
# ---------------------------------------------------------------------------


def test_tolerance_is_magnitude_aware_for_float32_storage():
    """Blender stores float32; round-trip error grows with magnitude."""
    # Measured Blender round-trip error at 1000.4 m is 2.44e-05.
    assert coordinates_equal(1000.4, 1000.4000244140625)
    # Near the origin the absolute floor applies.
    assert coordinates_equal(0.5, 0.5 + 5e-7)
    assert not coordinates_equal(0.5, 0.5 + 5e-5)


def test_tolerance_never_hides_a_meaningful_displacement():
    """1 mm must always be detected, even 1 km from the origin."""
    assert MAX_HIDDEN_DISPLACEMENT_M_AT_1KM < 0.001
    assert not coordinates_equal(1000.0, 1000.001)
    assert not coordinates_equal(0.0, 0.001)


def test_tolerance_constants_are_documented_values():
    assert POSITION_ABSOLUTE_TOLERANCE_M == 1e-6
    assert POSITION_RELATIVE_TOLERANCE == 4e-7


def test_non_finite_coordinates_are_never_equal():
    nan = float("nan")
    assert not coordinates_equal(nan, nan)
    assert not coordinates_equal(float("inf"), float("inf"))
    assert not positions_equal(Vec3(nan, 0, 0), Vec3(nan, 0, 0))


def test_is_finite_position_rejects_non_numbers():
    assert is_finite_position(Vec3(0.0, 0.0, 0.0)) is True
    assert is_finite_position(Vec3(float("nan"), 0.0, 0.0)) is False
    assert is_finite_position(Vec3("0.5", 0.0, 0.0)) is False
    assert is_finite_position(Vec3(True, 0.0, 0.0)) is False


def test_one_millimetre_move_is_applied_not_treated_as_noise():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = move_object(cube_plan(delta=Vec3(0.001, 0.0, 0.0)), scene)
    assert result.applied is True
    assert result.final_position_meters.x == 0.001


# ---------------------------------------------------------------------------
# plan_from_delta keeps the approved Job contract intact
# ---------------------------------------------------------------------------


def test_plan_from_delta_derives_absolute_endpoints():
    plan = plan_from_delta(
        job_id="job_1",
        target=ObjectRef(name="Cube"),
        expected_before_meters=Vec3(1.0, 2.0, 3.0),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert plan.expected_before_meters == Vec3(1.0, 2.0, 3.0)
    assert plan.delta_meters == Vec3(0.5, 0.0, 0.0)
    assert plan.desired_after_meters == Vec3(1.5, 2.0, 3.0)


def test_the_domain_layer_never_sees_units_or_directions():
    """The plan type has no field that could carry unresolved language."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(MoveObjectPlan)}
    for forbidden in ("unit", "units", "direction", "distance", "phrase", "message"):
        assert forbidden not in fields

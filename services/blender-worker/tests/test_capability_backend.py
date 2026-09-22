"""The capability boundary: normalisation, the fake backend, and the approval rule."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from blender_worker.backends.fake import FakeBlenderCapabilityProvider, FakeObject
from blender_worker.backends.official.backend import approval_token_for
from blender_worker.capability import names, normalize
from blender_worker.capability.provider import (
    APPROVAL_REQUIRED,
    BlenderCapabilityProvider,
    CapabilityRequest,
)

PROJECT = "proj_test"
BLEND = Path("/tmp/proj_test.blend")


def request(capability: str, **arguments: object) -> CapabilityRequest:
    token = arguments.pop("approval_token", None)
    return CapabilityRequest(
        capability=capability,
        project_id=PROJECT,
        project_path=BLEND,
        arguments=arguments,
        approval_token=token,  # type: ignore[arg-type]
    )


@pytest.fixture()
def backend() -> FakeBlenderCapabilityProvider:
    provider = FakeBlenderCapabilityProvider()
    provider.seed_cube(object_id="obj_cube")
    return provider


# --- the protocol ---------------------------------------------------------


def test_the_fake_backend_satisfies_the_provider_protocol(backend) -> None:
    assert isinstance(backend, BlenderCapabilityProvider)


def test_an_unknown_capability_is_reported_as_unavailable(backend) -> None:
    result = backend.invoke(request("teleport_object"))
    assert not result.ok
    assert result.error_code == "CAPABILITY_UNAVAILABLE"


def test_health_reports_availability(backend) -> None:
    assert backend.health().available
    offline = FakeBlenderCapabilityProvider(available=False)
    assert not offline.health().available
    result = offline.invoke(request(names.INSPECT_SCENE))
    assert result.error_code == "BLENDER_UNAVAILABLE"


# --- normalisation --------------------------------------------------------


def test_a_scene_read_normalises_into_a_snapshot_with_a_digest(backend) -> None:
    result = backend.invoke(request(names.INSPECT_SCENE))
    assert result.ok
    readout = normalize.scene_snapshot_from_backend(PROJECT, result.data)
    snapshot = readout.snapshot
    assert snapshot.project_id == PROJECT
    assert snapshot.scene_version.startswith("sha256:")
    assert [obj.name for obj in snapshot.objects] == ["Cube"]
    assert snapshot.objects[0].studio_object_id == "obj_cube"
    assert snapshot.units.length_unit == "m"


def test_the_same_unchanged_scene_yields_the_same_scene_version(backend) -> None:
    first = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    ).snapshot
    second = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    ).snapshot
    assert first.scene_version == second.scene_version
    assert first.captured_at is not None


def test_a_mutation_changes_the_scene_version(backend) -> None:
    before = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    ).snapshot
    backend.invoke(
        request(
            names.MOVE_OBJECT,
            object_id="obj_cube",
            desired_position_meters={"x": 0.5, "y": 0.0, "z": 0.0},
        )
    )
    after = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    ).snapshot
    assert before.scene_version != after.scene_version


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"objects": []}, "units"),
        ({"units": {"unit_system": "METRIC", "length_unit": "m", "scale_length": 1.0}}, "objects"),
        (
            {
                "units": {"unit_system": "METRIC", "length_unit": "cm", "scale_length": 1.0},
                "objects": [],
            },
            "metres",
        ),
        (
            {
                "units": {"unit_system": "WEIRD", "length_unit": "m", "scale_length": 1.0},
                "objects": [],
            },
            "unit_system",
        ),
    ],
)
def test_untrustworthy_backend_output_is_refused(payload, fragment) -> None:
    with pytest.raises(normalize.NormalisationError) as error:
        normalize.scene_snapshot_from_backend(PROJECT, payload)
    assert fragment in str(error.value)


def test_a_non_finite_coordinate_is_refused() -> None:
    payload = {
        "units": {"unit_system": "METRIC", "length_unit": "m", "scale_length": 1.0},
        "objects": [
            {
                "name": "Cube",
                "object_type": "MESH",
                "visible": True,
                "world_position_meters": {"x": float("inf"), "y": 0.0, "z": 0.0},
                "dimensions_meters": {"x": 1.0, "y": 1.0, "z": 1.0},
                "rotation_euler_radians": {"x": 0.0, "y": 0.0, "z": 0.0},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
            }
        ],
    }
    with pytest.raises(normalize.NormalisationError):
        normalize.scene_snapshot_from_backend(PROJECT, payload)


def test_absent_material_stays_absent_rather_than_becoming_black(backend) -> None:
    readout = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    )
    assert readout.snapshot.objects[0].material is None

    backend.invoke(
        request(
            names.SET_MATERIAL_COLOR,
            object_id="obj_cube",
            color_linear_srgb={"r": 0.0, "g": 0.0, "b": 0.0, "a": 1.0},
        )
    )
    painted = normalize.scene_snapshot_from_backend(
        PROJECT, backend.invoke(request(names.INSPECT_SCENE)).data
    )
    material = painted.snapshot.objects[0].material
    assert material is not None
    assert material.base_color is not None
    assert material.base_color.r == 0.0


# --- geometry -------------------------------------------------------------


def test_a_wall_is_placed_and_oriented_from_its_endpoints(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_WALL,
            display_name="Wall_North",
            object_id="obj_wall_1",
            start_meters={"x": 0.0, "y": 0.0},
            end_meters={"x": 4.0, "y": 0.0},
            height_meters=2.4,
            thickness_meters=0.12,
            base_elevation_meters=0.0,
        )
    )
    assert result.ok
    wall = result.data["object"]
    assert result.data["length_meters"] == pytest.approx(4.0)
    assert wall["dimensions_meters"] == {"x": 4.0, "y": 0.12, "z": 2.4}
    # Centred along the run, and lifted to half its height.
    assert wall["world_position_meters"]["x"] == pytest.approx(2.0)
    assert wall["world_position_meters"]["z"] == pytest.approx(1.2)
    assert wall["rotation_euler_radians"]["z"] == pytest.approx(0.0)


def test_a_diagonal_wall_is_rotated_to_match_its_run(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_WALL,
            display_name="Wall_Diagonal",
            start_meters={"x": 0.0, "y": 0.0},
            end_meters={"x": 3.0, "y": 3.0},
            height_meters=2.4,
            thickness_meters=0.12,
        )
    )
    wall = result.data["object"]
    assert result.data["length_meters"] == pytest.approx(math.hypot(3.0, 3.0))
    assert wall["rotation_euler_radians"]["z"] == pytest.approx(math.pi / 4)


def test_a_degenerate_wall_is_refused(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_WALL,
            start_meters={"x": 1.0, "y": 1.0},
            end_meters={"x": 1.0, "y": 1.0},
            height_meters=2.4,
            thickness_meters=0.12,
        )
    )
    assert not result.ok
    assert result.error_code == "MUTATION_FAILED"


def test_a_floor_spans_its_footprint(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_FLOOR,
            display_name="Floor",
            footprint_meters=[
                {"x": 0.0, "y": 0.0},
                {"x": 4.0, "y": 0.0},
                {"x": 4.0, "y": 3.0},
                {"x": 0.0, "y": 3.0},
            ],
            thickness_meters=0.2,
            elevation_meters=0.0,
        )
    )
    assert result.ok
    floor = result.data["object"]
    assert floor["dimensions_meters"]["x"] == pytest.approx(4.0)
    assert floor["dimensions_meters"]["y"] == pytest.approx(3.0)


def test_a_floor_needs_at_least_three_points(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_FLOOR,
            footprint_meters=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}],
            thickness_meters=0.2,
        )
    )
    assert not result.ok
    assert result.error_code == "VALIDATION_ERROR"


def test_an_opening_is_recorded_against_its_wall(backend) -> None:
    backend.invoke(
        request(
            names.CREATE_WALL,
            display_name="Wall_North",
            object_id="obj_wall_1",
            start_meters={"x": 0.0, "y": 0.0},
            end_meters={"x": 4.0, "y": 0.0},
            height_meters=2.4,
            thickness_meters=0.12,
        )
    )
    result = backend.invoke(
        request(
            names.CREATE_OPENING,
            wall_object_id="obj_wall_1",
            centre_meters={"x": 1.0, "y": 0.0, "z": 1.05},
            width_meters=0.9,
            height_meters=2.1,
        )
    )
    assert result.ok
    assert backend.find(object_id="obj_wall_1").openings == 1


def test_an_opening_in_a_missing_wall_is_refused(backend) -> None:
    result = backend.invoke(
        request(
            names.CREATE_OPENING,
            wall_object_id="obj_nope",
            centre_meters={"x": 0.0, "y": 0.0, "z": 1.0},
            width_meters=0.9,
            height_meters=2.1,
        )
    )
    assert result.error_code == "OBJECT_NOT_FOUND"


# --- object resolution ----------------------------------------------------


def test_a_stable_id_resolves_even_when_the_display_name_changed(backend) -> None:
    backend.objects["Renamed"] = backend.objects.pop("Cube")
    backend.objects["Renamed"].name = "Renamed"
    result = backend.invoke(
        request(
            names.MOVE_OBJECT,
            object_id="obj_cube",
            desired_position_meters={"x": 1.0, "y": 0.0, "z": 0.0},
        )
    )
    assert result.ok
    assert result.data["object"]["name"] == "Renamed"


def test_an_invented_object_is_refused(backend) -> None:
    result = backend.invoke(
        request(
            names.MOVE_OBJECT,
            object_id="obj_does_not_exist",
            desired_position_meters={"x": 1.0, "y": 0.0, "z": 0.0},
        )
    )
    assert result.error_code == "OBJECT_NOT_FOUND"


# --- model-authored code and the approval rule ---------------------------


def test_scene_only_model_code_runs_without_approval(backend) -> None:
    code = "import bpy\nbpy.ops.mesh.primitive_cube_add(size=1.0)\n"
    result = backend.invoke(request(names.EXECUTE_BLENDER_PYTHON, code=code))
    assert result.ok
    assert backend.executed_code == [code]


def test_risky_model_code_is_held_for_approval_and_does_not_run(backend) -> None:
    code = "import shutil\nshutil.rmtree('/home/christian')\n"
    result = backend.invoke(request(names.EXECUTE_BLENDER_PYTHON, code=code))
    assert not result.ok
    assert result.error_code == APPROVAL_REQUIRED
    assert backend.executed_code == []
    assert result.assessment is not None
    assert result.assessment.requires_approval
    assert any("shutil" in reason for reason in result.assessment.reasons())


def test_an_approved_token_lets_the_same_code_run(backend) -> None:
    code = "import os\nimport bpy\nprint(os.getcwd())\n"
    held = backend.invoke(request(names.EXECUTE_BLENDER_PYTHON, code=code))
    assert held.error_code == APPROVAL_REQUIRED

    approved = backend.invoke(
        request(
            names.EXECUTE_BLENDER_PYTHON,
            code=code,
            approval_token=approval_token_for(code),
        )
    )
    assert approved.ok
    assert backend.executed_code == [code]


def test_an_approval_token_is_bound_to_the_exact_code_it_approved(backend) -> None:
    approved_code = "import os\nprint(os.getcwd())\n"
    swapped_code = "import shutil\nshutil.rmtree('/home/christian')\n"

    result = backend.invoke(
        request(
            names.EXECUTE_BLENDER_PYTHON,
            code=swapped_code,
            approval_token=approval_token_for(approved_code),
        )
    )
    assert not result.ok
    assert result.error_code == APPROVAL_REQUIRED
    assert backend.executed_code == []


def test_unparseable_model_code_is_refused_outright(backend) -> None:
    result = backend.invoke(
        request(names.EXECUTE_BLENDER_PYTHON, code="import bpy\nobj = bpy.data.objects[\n")
    )
    assert not result.ok
    assert result.error_code == "VALIDATION_ERROR"
    assert backend.executed_code == []


def test_empty_model_code_is_refused(backend) -> None:
    result = backend.invoke(request(names.EXECUTE_BLENDER_PYTHON, code="   "))
    assert result.error_code == "VALIDATION_ERROR"


# --- artifacts ------------------------------------------------------------


def test_a_glb_export_writes_to_the_platform_chosen_path(backend, tmp_path: Path) -> None:
    destination = tmp_path / "scene.glb"
    result = backend.invoke(request(names.EXPORT_GLB, output_path=str(destination)))
    assert result.ok
    assert destination.exists()
    assert destination.read_bytes().startswith(b"glTF")


def test_a_glb_export_without_a_destination_is_refused(backend) -> None:
    result = backend.invoke(request(names.EXPORT_GLB))
    assert result.error_code == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
#
# From a real turn: "create this whole scene as similar as possible" produced one
# enormous authored program, Blender was killed at the upstream limit, and the user was
# shown `Error executing tool execute_blender_code_for_cli: Blender CLI timed out after
# 120s` — reported as BLENDER_UNAVAILABLE, which sent everyone looking for a broken
# Blender. Blender was fine. The step was too big.
#
# The limit is a hard-coded constant in the pinned upstream MCP and this project does not
# fork it, so the ceiling is designed around rather than raised. What matters is that the
# failure says something true and actionable, because the model reads it too and its next
# attempt depends on it.


def test_a_run_that_exceeded_the_limit_is_not_reported_as_a_broken_blender() -> None:
    from blender_worker.backends.official.backend import (
        STEP_TOO_LARGE_MESSAGE,
        _looks_like_cli_timeout,
    )

    assert _looks_like_cli_timeout(
        "Error executing tool execute_blender_code_for_cli: "
        "Blender CLI timed out after 120s"
    )
    # The advice has to be in the message, because the message is the only channel
    # reaching the next attempt.
    assert "smaller steps" in STEP_TOO_LARGE_MESSAGE
    assert "nothing from it was saved" in STEP_TOO_LARGE_MESSAGE


def test_a_genuinely_unavailable_blender_is_still_reported_as_such() -> None:
    """Narrowing the timeout case must not swallow the real availability failure."""
    from blender_worker.backends.official.backend import _looks_like_cli_timeout

    assert not _looks_like_cli_timeout("Blender executable not found")
    assert not _looks_like_cli_timeout("connection refused")
    assert not _looks_like_cli_timeout("the MCP session is not initialised")

"""Task 3: the official Blender Lab MCP verified against the real Blender on this machine.

Opt-in: ``pytest -m mcp``. Requires
``python scripts/setup_official_blender_mcp.py`` to have run, and a real Blender.

This suite is the executable form of the integration spike. It answers one question
with evidence:

    Can our safe, durable architecture use the official Blender MCP as its Blender
    backend without giving the model unrestricted execution?

It deliberately operates on a throwaway fixture, never a user project.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blender_worker.backends.official.backend import (
    CLI_EXECUTE_TOOL,
    REQUIRED_TOOLS,
    OfficialBlenderLabBackend,
    approval_token_for,
)
from blender_worker.backends.official.session import McpInstall, McpSessionError, OfficialMcpSession
from blender_worker.capability import names, normalize
from blender_worker.capability.provider import CapabilityRequest

pytestmark = pytest.mark.mcp

REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_FILE = REPO_ROOT / "external" / "official-blender-mcp.pin.json"

#: The inventory recorded when the pin was verified. A change must FAIL loudly
#: rather than be silently absorbed.
EXPECTED_TOOLS = (
    "execute_blender_code",
    "execute_blender_code_for_cli",
    "get_blendfile_summary_datablocks",
    "get_blendfile_summary_datablocks_for_cli",
    "get_blendfile_summary_missing_files",
    "get_blendfile_summary_missing_files_for_cli",
    "get_blendfile_summary_of_linked_libraries",
    "get_blendfile_summary_of_linked_libraries_for_cli",
    "get_blendfile_summary_path_info",
    "get_blendfile_summary_path_info_for_cli",
    "get_blendfile_summary_usage_guess",
    "get_blendfile_summary_usage_guess_for_cli",
    "get_object_detail_summary",
    "get_objects_summary",
    "get_python_api_docs",
    "get_screenshot_of_area_as_image",
    "get_screenshot_of_window_as_image",
    "get_screenshot_of_window_as_json",
    "jump_to_tab_by_name",
    "jump_to_tab_by_space_type",
    "jump_to_view3d_object_by_name",
    "jump_to_view3d_object_data_by_name",
    "render_thumbnail_to_path",
    "render_viewport_to_path",
    "search_api_docs",
    "search_manual_docs",
)

FIXTURE_SCRIPT = """
import bpy
import sys

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 0.0))
bpy.context.active_object.name = "Cube"
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""


def _blender() -> str:
    from blender_mcp.blender_runtime import find_blender_executable

    executable = find_blender_executable()
    if not executable:
        pytest.skip("no Blender executable found")
    return executable


@pytest.fixture(scope="module")
def install() -> McpInstall:
    try:
        return McpInstall.load()
    except McpSessionError as error:
        pytest.skip(str(error))


@pytest.fixture(scope="module")
def session(install: McpInstall):
    session = OfficialMcpSession(install)
    try:
        session.start()
    except McpSessionError as error:
        pytest.skip(f"official MCP server would not start: {error}")
    yield session
    session.close()


@pytest.fixture(scope="module")
def backend(session: OfficialMcpSession) -> OfficialBlenderLabBackend:
    return OfficialBlenderLabBackend(session)


@pytest.fixture()
def fixture_project(tmp_path: Path) -> Path:
    """A throwaway project. Step 11: never a user project."""
    blend = tmp_path / "spike_fixture.blend"
    script = tmp_path / "make_fixture.py"
    script.write_text(FIXTURE_SCRIPT, encoding="utf-8")
    subprocess.run(
        [_blender(), "--background", "--factory-startup", "--python", str(script), "--", str(blend)],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert blend.exists()
    return blend


def _request(capability: str, project: Path, **arguments: object) -> CapabilityRequest:
    token = arguments.pop("approval_token", None)
    return CapabilityRequest(
        capability=capability,
        project_id="proj_spike",
        project_path=project,
        arguments=arguments,
        approval_token=token,  # type: ignore[arg-type]
    )


# --- steps 1 and 2: identity, and the real Blender on this machine -------


def test_step_1_the_pinned_identity_is_recorded_and_installed(install: McpInstall) -> None:
    pin = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    assert install.pinned_commit == pin["pinned_commit"]
    assert install.server_version == pin["mcp_server"]["declared_version"]
    assert "GPL-3.0-or-later" in install.licence
    assert pin["canonical_repository"].startswith("https://projects.blender.org/")
    # The pin must never be a moving target: it is an immutable commit, and no
    # version-bearing field may say "latest".
    assert len(pin["pinned_commit"]) == 40
    version_fields = (
        pin["pinned_commit"],
        pin["mcp_server"]["declared_version"],
        pin["blender_addon"]["declared_version"],
    )
    assert not any("latest" in field.lower() for field in version_fields)


def test_step_2_the_backend_is_healthy_against_the_installed_blender(
    backend: OfficialBlenderLabBackend,
) -> None:
    health = backend.health()
    assert health.available, health.detail
    assert health.backend == "official_blender_lab_mcp"
    assert health.blender_version, "Blender version should be discoverable"


# --- steps 3 and 4: connect programmatically, enumerate the real interface


def test_step_3_and_4_the_tool_inventory_matches_the_pinned_record(
    session: OfficialMcpSession,
) -> None:
    tools = session.list_tools()
    assert tools == EXPECTED_TOOLS, (
        "the official MCP tool inventory changed; review and re-pin before adapting"
    )
    for required in REQUIRED_TOOLS:
        assert required in tools


def test_step_4_the_official_mcp_still_has_no_semantic_mutation_tool(
    session: OfficialMcpSession,
) -> None:
    """The finding that justifies platform-owned scripts existing at all."""
    tools = session.list_tools()
    semantic_mutations = [
        tool
        for tool in tools
        if any(
            verb in tool
            for verb in ("move", "rotate", "scale", "translate", "set_material", "create_object")
        )
    ]
    assert semantic_mutations == [], (
        f"the official MCP now exposes semantic mutation tools {semantic_mutations}; "
        "the registry should start mapping to them"
    )
    assert session.server_info["name"]


# --- steps 5 and 6: read a real scene, derive a snapshot -----------------


def test_step_5_and_6_a_real_scene_normalises_into_a_scene_snapshot(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    result = backend.invoke(_request(names.INSPECT_SCENE, fixture_project))
    assert result.ok, result.error_message

    readout = normalize.scene_snapshot_from_backend("proj_spike", result.data)
    snapshot = readout.snapshot
    assert [obj.name for obj in snapshot.objects] == ["Cube"]
    assert snapshot.scene_version.startswith("sha256:")
    assert snapshot.units.length_unit == "m"
    assert snapshot.units.unit_system == "METRIC"

    cube = snapshot.objects[0]
    assert cube.world_position_meters.x == pytest.approx(0.0, abs=1e-6)
    assert cube.dimensions_meters.x == pytest.approx(2.0, abs=1e-6)
    # Recorded coverage: the official reads supply everything SceneObject needs.
    assert cube.object_type == "MESH"
    assert cube.visible is True


def test_step_6_a_read_does_not_modify_the_project(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    import hashlib

    before = hashlib.sha256(fixture_project.read_bytes()).hexdigest()
    backend.invoke(_request(names.INSPECT_SCENE, fixture_project))
    after = hashlib.sha256(fixture_project.read_bytes()).hexdigest()
    assert before == after, "inspecting a scene must not write to the project"


# --- steps 7, 8, 9, 10: one bounded mutation, no model, verified ---------


def test_step_7_to_10_one_bounded_mutation_is_applied_and_verified(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    """Move Cube +0.50 m on X, through a platform script, and verify by reading back."""
    result = backend.invoke(
        _request(
            names.MOVE_OBJECT,
            fixture_project,
            name="Cube",
            desired_position_meters={"x": 0.5, "y": 0.0, "z": 0.0},
        )
    )
    assert result.ok, result.error_message

    # Step 10: a returned status is not evidence. Read the file back.
    verified = backend.invoke(_request(names.INSPECT_SCENE, fixture_project))
    snapshot = normalize.scene_snapshot_from_backend("proj_spike", verified.data).snapshot
    cube = normalize.find_object(snapshot, name="Cube")
    assert cube is not None
    assert cube.world_position_meters.x == pytest.approx(0.5, abs=1e-6)
    assert cube.world_position_meters.y == pytest.approx(0.0, abs=1e-6)


def test_step_11_no_sibling_project_file_is_left_behind(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    """The upstream CLI helper can create a numbered copy; prove it does not here."""
    backend.invoke(
        _request(
            names.MOVE_OBJECT,
            fixture_project,
            name="Cube",
            desired_position_meters={"x": 0.25, "y": 0.0, "z": 0.0},
        )
    )
    siblings = sorted(p.name for p in fixture_project.parent.iterdir())
    assert not any("_mcp_" in name for name in siblings), siblings
    # Blender's own .blend1 backup is expected and is already gitignored.
    assert fixture_project.name in siblings


def test_step_12_repeating_the_same_absolute_move_cannot_apply_it_twice(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    """Absolute desired-after targets are what make a retry safe."""
    target = {"x": 0.5, "y": 0.0, "z": 0.0}
    for _ in range(2):
        result = backend.invoke(
            _request(names.MOVE_OBJECT, fixture_project, name="Cube", desired_position_meters=target)
        )
        assert result.ok

    snapshot = normalize.scene_snapshot_from_backend(
        "proj_spike", backend.invoke(_request(names.INSPECT_SCENE, fixture_project)).data
    ).snapshot
    cube = normalize.find_object(snapshot, name="Cube")
    assert cube is not None
    # 0.5, not 1.0: the operation carries where the object should END UP.
    assert cube.world_position_meters.x == pytest.approx(0.5, abs=1e-6)


# --- step 13: model-authored code cannot slip through -------------------


def test_step_13_risky_model_code_is_held_for_approval_and_never_executes(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    marker = fixture_project.parent / "should_never_exist.txt"
    code = (
        "import bpy\n"
        f"open({str(marker)!r}, 'w').write('escaped')\n"
    )
    result = backend.invoke(
        _request(names.EXECUTE_BLENDER_PYTHON, fixture_project, code=code)
    )
    assert not result.ok
    assert result.error_code == "APPROVAL_REQUIRED"
    assert not marker.exists(), "held code must not have run"


def test_step_13_scene_only_model_code_runs_against_real_blender(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    """Model-authored Python is a first-class path when it only touches the scene."""
    code = (
        "import bpy\n"
        "bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3.0, 0.0, 0.0))\n"
        "bpy.context.active_object.name = 'ModelMadeThis'\n"
        "bpy.ops.wm.save_mainfile()\n"
        "result = {'created': 'ModelMadeThis'}\n"
    )
    result = backend.invoke(
        _request(names.EXECUTE_BLENDER_PYTHON, fixture_project, code=code)
    )
    assert result.ok, result.error_message

    snapshot = normalize.scene_snapshot_from_backend(
        "proj_spike", backend.invoke(_request(names.INSPECT_SCENE, fixture_project)).data
    ).snapshot
    assert normalize.find_object(snapshot, name="ModelMadeThis") is not None


def test_step_13_approved_risky_code_runs_only_with_a_matching_token(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    marker = fixture_project.parent / "approved_write.txt"
    code = "import bpy\n" f"open({str(marker)!r}, 'w').write('approved')\n"

    wrong = backend.invoke(
        _request(
            names.EXECUTE_BLENDER_PYTHON,
            fixture_project,
            code=code,
            approval_token=approval_token_for("something else entirely"),
        )
    )
    assert wrong.error_code == "APPROVAL_REQUIRED"
    assert not marker.exists()

    approved = backend.invoke(
        _request(
            names.EXECUTE_BLENDER_PYTHON,
            fixture_project,
            code=code,
            approval_token=approval_token_for(code),
        )
    )
    assert approved.ok, approved.error_message
    assert marker.exists(), "approved code should have run"


# --- architecture: the primitives the MVP needs -------------------------


def test_architectural_primitives_build_a_recognisable_room(
    backend: OfficialBlenderLabBackend, fixture_project: Path
) -> None:
    backend.invoke(
        _request(
            names.CREATE_FLOOR,
            fixture_project,
            display_name="Floor",
            object_id="obj_floor",
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
    wall = backend.invoke(
        _request(
            names.CREATE_WALL,
            fixture_project,
            display_name="Wall_South",
            object_id="obj_wall_south",
            start_meters={"x": 0.0, "y": 0.0},
            end_meters={"x": 4.0, "y": 0.0},
            height_meters=2.4,
            thickness_meters=0.12,
            base_elevation_meters=0.0,
        )
    )
    assert wall.ok, wall.error_message
    assert wall.data["length_meters"] == pytest.approx(4.0, abs=1e-6)

    opening = backend.invoke(
        _request(
            names.CREATE_OPENING,
            fixture_project,
            wall_object_id="obj_wall_south",
            centre_meters={"x": 1.0, "y": 0.0, "z": 1.05},
            width_meters=0.9,
            height_meters=2.1,
            wall_thickness_meters=0.12,
        )
    )
    assert opening.ok, opening.error_message

    door = backend.invoke(
        _request(
            names.CREATE_DOOR_PLACEHOLDER,
            fixture_project,
            display_name="Door_1",
            object_id="obj_door_1",
            centre_meters={"x": 1.0, "y": 0.0, "z": 1.05},
            width_meters=0.9,
            height_meters=2.1,
            depth_meters=0.05,
        )
    )
    assert door.ok, door.error_message

    snapshot = normalize.scene_snapshot_from_backend(
        "proj_spike", backend.invoke(_request(names.INSPECT_SCENE, fixture_project)).data
    ).snapshot
    by_name = {obj.name: obj for obj in snapshot.objects}
    assert {"Floor", "Wall_South", "Door_1"} <= set(by_name)

    wall_object = by_name["Wall_South"]
    assert wall_object.studio_object_id == "obj_wall_south"
    assert wall_object.dimensions_meters.z == pytest.approx(2.4, abs=1e-3)


def test_a_glb_export_produces_a_real_binary_gltf(
    backend: OfficialBlenderLabBackend, fixture_project: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "export" / "scene.glb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = backend.invoke(
        _request(names.EXPORT_GLB, fixture_project, output_path=str(destination))
    )
    assert result.ok, result.error_message
    assert destination.exists()
    assert destination.read_bytes()[:4] == b"glTF"
    assert destination.stat().st_size > 500


# --- step 14: reconnect, and a dead server surfaces cleanly -------------


def test_step_14_connect_disconnect_reconnect(install: McpInstall) -> None:
    first = OfficialMcpSession(install)
    first.start()
    assert first.started
    tools = first.list_tools()
    first.close()
    assert not first.started

    second = OfficialMcpSession(install)
    second.start()
    assert second.list_tools() == tools
    second.close()


def test_step_14_a_missing_server_reports_unavailable_rather_than_hanging(
    tmp_path: Path,
) -> None:
    broken = McpInstall(
        server_command=(str(tmp_path / "does-not-exist"),),
        pinned_commit="x",
        server_version="x",
        addon_version="x",
        licence="x",
    )
    backend = OfficialBlenderLabBackend(OfficialMcpSession(broken))
    health = backend.health()
    assert not health.available

    result = backend.invoke(
        _request(names.INSPECT_SCENE, tmp_path / "nothing.blend")
    )
    assert not result.ok
    assert result.error_code == "BLENDER_UNAVAILABLE"

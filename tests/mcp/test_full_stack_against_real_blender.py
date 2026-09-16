"""The whole stack against REAL Blender through the OFFICIAL Blender MCP.

Opt-in: ``pytest -m mcp``. Requires ``python scripts/setup_official_blender_mcp.py``
and a real Blender.

This is the same offline harness the acceptance suites use — real uvicorn, real HTTP,
real WebSocket, real routes, real SQLite, real job contracts, real project lock and
journal — with the Blender end swapped for the real thing:

    httpx -> FastAPI -> DesignChatService -> job -> WebSocket -> worker
          -> CapabilityPlanExecutor -> OfficialBlenderLabBackend
          -> official Blender MCP (blender_mcp @ pinned commit)
          -> Blender 5.x, headless -> saved .blend -> GLB

Only the model is scripted, and only because reaching Astra needs an interactive
ChatGPT sign-in that cannot run in a suite. Everything the model would decide is
supplied as the exact validated proposal it would have to produce.

Every geometric assertion is read from the SAVED ``.blend`` by a FRESH Blender process,
so what is proven is durability rather than worker memory (.kiro/steering/testing.md).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from blender_worker.backends.official.backend import OfficialBlenderLabBackend  # noqa: E402
from blender_worker.backends.official.session import (  # noqa: E402
    McpInstall,
    McpSessionError,
    OfficialMcpSession,
)
from studio_agent.providers.fake_llm import operation  # noqa: E402
from studio_fixtures.design_stack import DesignStack, build_design_stack  # noqa: E402

pytestmark = pytest.mark.mcp

PROJECT_ID = "proj_real"
CEILING = 2.70
THICKNESS = 0.12
ROOM_WIDTH = 4.0
ROOM_DEPTH = 3.0

#: Blender's own transforms are float32, so compare in millimetres, not in ulps.
TOLERANCE = 1e-4

FIXTURE_SCRIPT = """
import bpy
import sys

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.unit_settings.system = "METRIC"
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""

READBACK_SCRIPT = """
import bpy
import json
import sys

out = []
for obj in bpy.data.objects:
    t = obj.matrix_world.translation
    d = obj.dimensions
    out.append({
        "name": obj.name,
        "studio_object_id": obj.get("studio_object_id"),
        "position": [t[0], t[1], t[2]],
        "dimensions": [d[0], d[1], d[2]],
        "rotation_z": obj.rotation_euler[2],
    })
with open(sys.argv[-1], "w") as handle:
    json.dump(out, handle)
"""


def _blender() -> str:
    from blender_mcp.blender_runtime import find_blender_executable

    executable = find_blender_executable()
    if not executable:
        pytest.skip("no Blender executable found")
    return executable


def _run_blender(script: str, tmp_path: Path, argument: Path) -> None:
    path = tmp_path / "script.py"
    path.write_text(script, encoding="utf-8")
    subprocess.run(
        [
            _blender(),
            "--background",
            "--factory-startup",
            "--python",
            str(path),
            "--",
            str(argument),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )


def _saved_objects(project: Path, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Read the SAVED project with a fresh Blender process."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "readback.json"
    script = tmp_path / "readback.py"
    script.write_text(READBACK_SCRIPT, encoding="utf-8")
    subprocess.run(
        [
            _blender(),
            "--background",
            "--factory-startup",
            str(project),
            "--python",
            str(script),
            "--",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    return {entry["name"]: entry for entry in json.loads(out.read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def session() -> Iterator[OfficialMcpSession]:
    try:
        install = McpInstall.load()
    except McpSessionError as error:
        pytest.skip(str(error))
    live = OfficialMcpSession(install)
    try:
        live.start()
    except McpSessionError as error:
        pytest.skip(f"official MCP server would not start: {error}")
    yield live
    live.close()


@pytest.fixture
def stack(tmp_path: Path, session: OfficialMcpSession) -> Iterator[DesignStack]:
    """The full stack, with the OFFICIAL MCP backend and a throwaway project."""
    source = tmp_path / "empty.blend"
    _run_blender(FIXTURE_SCRIPT, tmp_path, source)
    assert source.exists(), "the fixture project was not created"

    with build_design_stack(
        tmp_path / "stack",
        project_id=PROJECT_ID,
        backend=OfficialBlenderLabBackend(session),
        project_source=source,
    ) as running:
        yield running


def _wall(name: str, start: tuple[float, float], end: tuple[float, float]) -> dict:
    return operation(
        "create_wall",
        {
            "display_name": name,
            "object_id": f"obj_{name.lower()}",
            "start_meters": {"x": start[0], "y": start[1]},
            "end_meters": {"x": end[0], "y": end[1]},
            "height_meters": CEILING,
            "thickness_meters": THICKNESS,
        },
        label=f"build {name}",
    )


def test_a_room_is_built_in_real_blender_through_the_whole_stack(
    stack: DesignStack, tmp_path: Path
):
    """MANDATORY (real tier): a conversation turn becomes durable Blender geometry.

    Asserts, in order:

      1. the request is accepted and dispatched as ONE job
      2. the job succeeds
      3. the SAVED .blend, reopened by a fresh Blender, holds the wall
      4. its geometry is correct in metres
      5. the stable studio id was written into the file, so later turns can find it
      6. the control plane cached the resulting scene for the next turn
      7. a real GLB was exported and is downloadable
    """
    half_w, half_d = ROOM_WIDTH / 2, ROOM_DEPTH / 2

    # ---- 1 & 2 -----------------------------------------------------------
    stack.llm.queue_plan(
        [
            _wall("Wall_North", (-half_w, half_d), (half_w, half_d)),
            operation(
                "create_floor",
                {
                    "display_name": "Floor",
                    "object_id": "obj_floor",
                    "footprint_meters": [
                        {"x": -half_w, "y": -half_d},
                        {"x": half_w, "y": -half_d},
                        {"x": half_w, "y": half_d},
                        {"x": -half_w, "y": half_d},
                    ],
                    "thickness_meters": 0.20,
                },
                label="lay the floor",
            ),
        ],
        message="Building the north wall and the floor.",
    )
    final = stack.run_turn(
        "req_real_001", "Build a 4 by 3 metre room, 2.7 m ceilings.", expected_status="succeeded"
    )
    assert final["result"]["applied"] == 2, final["result"]

    # ---- 3, 4 & 5: read the SAVED file with a FRESH Blender ---------------
    saved = _saved_objects(stack.project_path, tmp_path / "readback")
    assert "Wall_North" in saved, f"the saved project holds {sorted(saved)}"

    wall = saved["Wall_North"]
    assert wall["dimensions"][0] == pytest.approx(ROOM_WIDTH, abs=TOLERANCE)
    assert wall["dimensions"][1] == pytest.approx(THICKNESS, abs=TOLERANCE)
    assert wall["dimensions"][2] == pytest.approx(CEILING, abs=TOLERANCE)
    # A wall stands ON the floor.
    assert wall["position"][2] == pytest.approx(CEILING / 2, abs=TOLERANCE)
    assert wall["studio_object_id"] == "obj_wall_north", (
        "the stable id was not written into the .blend, so later turns cannot resolve it"
    )

    floor = saved["Floor"]
    assert floor["dimensions"][0] == pytest.approx(ROOM_WIDTH, abs=TOLERANCE)
    assert floor["dimensions"][1] == pytest.approx(ROOM_DEPTH, abs=TOLERANCE)

    # ---- 6: the browser and the next agent turn can see it ---------------
    cached = stack.cached_object("obj_wall_north")
    assert cached is not None, "the control plane never learned the resulting scene"
    assert cached["dimensions_meters"]["z"] == pytest.approx(CEILING, abs=TOLERANCE)

    # ---- 7: a real GLB ---------------------------------------------------
    model = final["result"].get("model")
    assert model is not None, final["result"].get("model_error")
    fetched = stack.http.get(
        f"/api/projects/{stack.project_id}/artifacts/{model['artifact_id']}"
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.content.startswith(b"glTF"), "that is not a glTF binary"
    assert len(fetched.content) > 1000, "a GLB of a room should not be a stub"


def test_a_second_turn_edits_the_object_from_the_first_one(
    stack: DesignStack, tmp_path: Path
):
    """The iterative loop, against real Blender: build, then change what was built."""
    stack.llm.queue_plan([_wall("Wall_North", (-2.0, 1.5), (2.0, 1.5))])
    stack.run_turn("req_real_010", "Build the north wall.")

    # The user clicks the wall in the browser and asks for it to be taller.
    stack.llm.queue_plan(
        [
            operation(
                "set_object_dimensions",
                {
                    "object_id": "obj_wall_north",
                    "desired_dimensions_meters": {
                        "x": ROOM_WIDTH,
                        "y": THICKNESS,
                        "z": 3.00,
                    },
                },
                label="raise the wall",
            )
        ]
    )
    final = stack.run_turn(
        "req_real_011", "Make this 30 cm taller.", selected_object_id="obj_wall_north"
    )
    assert final["result"]["applied"] == 1, final["result"]

    saved = _saved_objects(stack.project_path, tmp_path / "readback2")
    assert saved["Wall_North"]["dimensions"][2] == pytest.approx(3.00, abs=TOLERANCE)


def test_repeating_the_same_instruction_changes_nothing_in_real_blender(
    stack: DesignStack, tmp_path: Path
):
    """"Already satisfied" is decided by reading Blender, not by trusting a journal."""
    stack.llm.queue_plan([_wall("Wall_North", (-2.0, 1.5), (2.0, 1.5))])
    stack.run_turn("req_real_020", "Build the north wall.")
    first = _saved_objects(stack.project_path, tmp_path / "readback_a")

    stack.llm.queue_plan([_wall("Wall_North", (-2.0, 1.5), (2.0, 1.5))])
    final = stack.run_turn("req_real_021", "Build the north wall.")

    assert final["result"]["already_applied"] == 1, final["result"]
    assert final["result"]["applied"] == 0, final["result"]

    second = _saved_objects(stack.project_path, tmp_path / "readback_b")
    assert sorted(second) == sorted(first), "a duplicate request created a second wall"
    assert second["Wall_North"]["dimensions"] == pytest.approx(
        first["Wall_North"]["dimensions"], abs=TOLERANCE
    )

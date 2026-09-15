"""The Blender execution boundary.

Spec 001, Task 6.

    WorkerExecutor
        v
    BlenderOperationExecutor   (this module)
        v
    Task 4 MCP move_object handler
        v
    bpy

The worker orchestrates; it contains no bpy. Swapping the local
subprocess-per-execution model for a persistent Blender process later means
writing another implementation of this Protocol, not rewriting WorkerExecutor.

Current limitation, stated plainly: ``SubprocessBlenderOperationExecutor`` starts
a fresh headless Blender for every operation. That is simple and crash-isolated,
but it costs roughly a second per call and cannot hold a scene in memory between
steps. Acceptable for the local vertical slice; a persistent worker process is
future work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Protocol

from blender_mcp.blender_runtime import find_blender_executable, run_blender_script
from studio_types import MoveObjectPlan, ObjectRef, Vec3

WORKER_SCRIPTS = Path(__file__).resolve().parent / "blender_scripts"
READ_SCRIPT = WORKER_SCRIPTS / "read_position.py"
MOVE_SCRIPT = WORKER_SCRIPTS / "execute_move.py"

RESULT_PREFIX = "RESULT_JSON:"

#: Import roots Blender's bundled interpreter needs.
REPO_ROOT = Path(__file__).resolve().parents[3]
STUDIO_PATHS = (
    REPO_ROOT / "packages" / "types" / "python",
    REPO_ROOT / "packages" / "contracts" / "python",
    REPO_ROOT / "packages" / "validation" / "python",
    REPO_ROOT / "packages" / "spatial" / "python",
    REPO_ROOT / "services" / "blender-mcp",
)


class BlenderExecutionError(RuntimeError):
    """Blender could not be run, or did not report a usable outcome."""


class BlenderOperationExecutor(Protocol):
    """Semantic Blender operations the worker needs.

    Narrow on purpose: read a position, apply one move plan, nothing else.
    """

    def read_object_position(
        self, project_path: Path, target: ObjectRef
    ) -> Optional[Vec3]:
        """Read a target's world position from the SAVED project.

        Used both to capture ``expected_before`` before planning and to confirm
        durability after saving, so implementations must read persisted state
        rather than any in-memory scene.
        """
        ...

    def execute_move(
        self, project_path: Path, plan: MoveObjectPlan
    ) -> dict[str, Any]:
        """Apply one MoveObjectPlan and save the project.

        Returns the canonical MoveObjectResult wire document from Task 4.
        """
        ...


# ---------------------------------------------------------------------------
# Fake implementation for fast worker tests
# ---------------------------------------------------------------------------


class FakeBlenderOperationExecutor:
    """In-memory stand-in so worker orchestration is testable without Blender.

    Holds positions per (project_path, object name) and runs the REAL Task 4
    domain service against a fake scene adapter, so retry semantics under test
    are the production semantics — only bpy is replaced.
    """

    def __init__(self, positions: Optional[dict[str, Vec3]] = None) -> None:
        # project path -> object name -> position
        self.scenes: dict[str, dict[str, Vec3]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_move_with: Optional[str] = None
        self.crash_after_move: bool = False
        self._initial = positions or {}

    def seed_project(
        self, project_path: Path, objects: dict[str, Vec3], object_ids: Optional[dict[str, str]] = None
    ) -> None:
        self.scenes[str(project_path)] = dict(objects)
        self._object_ids = dict(object_ids or {})

    def _scene(self, project_path: Path) -> dict[str, Vec3]:
        return self.scenes.setdefault(str(project_path), dict(self._initial))

    def position_of(self, project_path: Path, name: str) -> Optional[Vec3]:
        return self._scene(project_path).get(name)

    def read_object_position(
        self, project_path: Path, target: ObjectRef
    ) -> Optional[Vec3]:
        self.calls.append(("read", str(project_path)))
        scene = self._scene(project_path)
        name = getattr(target, "name", None)
        if name and name in scene:
            return scene[name]
        object_id = getattr(target, "object_id", None)
        if object_id:
            for candidate, mapped in getattr(self, "_object_ids", {}).items():
                if mapped == object_id and candidate in scene:
                    return scene[candidate]
        return None

    def execute_move(
        self, project_path: Path, plan: MoveObjectPlan
    ) -> dict[str, Any]:
        self.calls.append(("move", str(project_path)))
        if self.fail_move_with is not None:
            raise BlenderExecutionError(self.fail_move_with)

        # Run the real Task 4 service against a fake scene, so the three
        # execution paths behave exactly as they do in production.
        from blender_mcp.adapters.fake_scene import FakeSceneAdapter
        from blender_mcp.mcp_server import handle_move_object
        from studio_contracts import to_wire

        scene_map = self._scene(project_path)
        adapter = FakeSceneAdapter()
        for name, position in scene_map.items():
            adapter.add_object(
                name,
                position,
                object_id=getattr(self, "_object_ids", {}).get(name),
            )

        result = handle_move_object(to_wire(plan), adapter)

        # Persist the fake scene, mimicking a Blender save.
        for name, state in adapter.objects.items():
            scene_map[name] = state.position_meters

        if self.crash_after_move:
            raise BlenderExecutionError(
                "simulated worker crash after mutation and save"
            )
        return result


# ---------------------------------------------------------------------------
# Real implementation: headless Blender subprocess per operation
# ---------------------------------------------------------------------------


class SubprocessBlenderOperationExecutor:
    """Runs each operation in a fresh headless Blender process.

    Blender is located through ``blender_mcp.blender_runtime`` (Task 4); this
    module contains no executable path.
    """

    def __init__(self, timeout: int = 600) -> None:
        self.timeout = timeout

    def _env(self, extra: dict[str, str]) -> dict[str, str]:
        return {
            "STUDIO_PYTHONPATH": os.pathsep.join(
                str(path) for path in STUDIO_PATHS
            ),
            **extra,
        }

    def _run(self, script: Path, env: dict[str, str]) -> dict[str, dict]:
        if find_blender_executable() is None:
            raise BlenderExecutionError("Blender executable not found")
        proc = run_blender_script(
            str(script), env=self._env(env), timeout=self.timeout
        )
        phases: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            if line.startswith(RESULT_PREFIX):
                payload = json.loads(line[len(RESULT_PREFIX) :])
                phases[payload.pop("phase")] = payload
        if "done" not in phases:
            raise BlenderExecutionError(
                f"{script.name} did not complete (exit {proc.returncode}).\n"
                f"stdout tail:\n{proc.stdout[-3000:]}\n"
                f"stderr tail:\n{proc.stderr[-3000:]}"
            )
        return phases

    def read_object_position(
        self, project_path: Path, target: ObjectRef
    ) -> Optional[Vec3]:
        phases = self._run(
            READ_SCRIPT,
            {
                "WORKER_BLEND": str(project_path),
                "WORKER_TARGET": json.dumps(
                    {
                        "object_id": getattr(target, "object_id", None),
                        "name": getattr(target, "name", None),
                    }
                ),
            },
        )
        found = phases.get("position", {})
        if not found.get("found"):
            return None
        coords = found["position"]
        return Vec3(float(coords[0]), float(coords[1]), float(coords[2]))

    def execute_move(
        self, project_path: Path, plan: MoveObjectPlan
    ) -> dict[str, Any]:
        from studio_contracts import to_wire

        phases = self._run(
            MOVE_SCRIPT,
            {
                "WORKER_BLEND": str(project_path),
                "WORKER_PLAN": json.dumps(to_wire(plan)),
            },
        )
        if "result" not in phases:
            raise BlenderExecutionError("Blender did not report a move result")
        return phases["result"]["result"]

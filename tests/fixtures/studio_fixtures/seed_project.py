"""Host-side access to the Spec 001 seed Blender fixture.

Spec 001, Task 5.

Responsibilities:
  - locate the spec and the generated .blend
  - generate the fixture from scratch (never hand-authored)
  - hand out ISOLATED working copies so tests never mutate the canonical file
  - inspect a .blend and compare its scene digest against the spec

Blender is always invoked through ``blender_mcp.blender_runtime`` so executable
detection lives in exactly one place (Task 4). No Blender path appears here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from blender_mcp.blender_runtime import find_blender_executable, run_blender_script
from studio_contracts.scene import compute_scene_version

REPO_ROOT = Path(__file__).resolve().parents[3]

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "blender"
SPEC_PATH = FIXTURE_DIR / "seed_project.spec.json"

GENERATOR_SCRIPT = FIXTURE_DIR / "generate_seed_project.py"
INSPECT_SCRIPT = FIXTURE_DIR / "inspect_blend.py"
MOVE_SCRIPT = FIXTURE_DIR / "move_on_blend.py"

#: Generated artefacts live under build/ and are NOT committed to Git. The
#: generator script and the spec are the committed source of truth; a binary
#: .blend would be an unreviewable duplicate that could silently drift.
BUILD_DIR = FIXTURE_DIR / "build"

RESULT_PREFIX = "RESULT_JSON:"

#: Import roots the Blender-bundled interpreter needs, since it does not read
#: this repository's pytest configuration.
STUDIO_PATHS = (
    REPO_ROOT / "packages" / "types" / "python",
    REPO_ROOT / "packages" / "contracts" / "python",
    REPO_ROOT / "packages" / "validation" / "python",
    REPO_ROOT / "packages" / "spatial" / "python",
    REPO_ROOT / "services" / "blender-mcp",
)


class FixtureError(RuntimeError):
    """Raised when the fixture cannot be generated or does not match its spec."""


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def load_spec() -> dict[str, Any]:
    """The machine-readable fixture specification."""
    with SPEC_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def seed_blend_path() -> Path:
    """Canonical location of the generated seed .blend."""
    return BUILD_DIR / load_spec()["blend_filename"]


def expected_object(name: str = "Cube") -> dict[str, Any]:
    """The spec entry for one seed object."""
    for entry in load_spec()["objects"]:
        if entry["name"] == name:
            return entry
    raise FixtureError(f"no object named {name!r} in the fixture spec")


# ---------------------------------------------------------------------------
# Blender invocation plumbing
# ---------------------------------------------------------------------------


def studio_pythonpath() -> str:
    return os.pathsep.join(str(path) for path in STUDIO_PATHS)


def _parse_phases(stdout: str) -> dict[str, dict]:
    phases: dict[str, dict] = {}
    for line in stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            phases[payload.pop("phase")] = payload
    return phases


def _run(script: Path, env: dict[str, str], timeout: int = 600) -> dict[str, dict]:
    if find_blender_executable() is None:
        raise FixtureError("Blender executable not found")

    merged = {"STUDIO_PYTHONPATH": studio_pythonpath(), **env}
    proc = run_blender_script(str(script), env=merged, timeout=timeout)
    phases = _parse_phases(proc.stdout)

    if "done" not in phases:
        raise FixtureError(
            f"{script.name} did not complete (exit {proc.returncode}).\n"
            f"stdout tail:\n{proc.stdout[-4000:]}\n"
            f"stderr tail:\n{proc.stderr[-4000:]}"
        )
    return phases


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_seed_project(
    output: Optional[Path] = None, force: bool = True
) -> Path:
    """Generate the seed .blend from scratch and return its path.

    Deterministic with respect to the scene state Spec 001 relies on: the
    generator starts from factory settings with an empty scene and builds
    everything from the spec.
    """
    destination = Path(output) if output is not None else seed_blend_path()
    if destination.exists() and not force:
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        GENERATOR_SCRIPT,
        {"SEED_SPEC": str(SPEC_PATH), "SEED_OUTPUT": str(destination)},
    )
    if not destination.exists():
        raise FixtureError(f"generator did not produce {destination}")
    return destination


def ensure_seed_project() -> Path:
    """The canonical seed .blend, generating it only if absent."""
    return generate_seed_project(force=False)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@contextmanager
def working_copy(prefix: str = "studio-seed-") -> Iterator[Path]:
    """Yield an isolated, writable copy of the seed project.

    Mutations belong in the copy. The canonical fixture is opened only to be
    copied, never written, so repeated E2E runs always begin from Cube X = 0 and
    no state leaks between runs. The temporary directory is removed afterwards
    even if the test fails.
    """
    canonical = ensure_seed_project()
    temp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        destination = temp_dir / canonical.name
        shutil.copy2(canonical, destination)
        yield destination
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def file_digest(path: Path) -> str:
    """SHA-256 of a file, used to prove the canonical fixture is untouched."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Inspection and verification
# ---------------------------------------------------------------------------


def inspect_blend(blend_path: Path) -> dict[str, Any]:
    """Open a .blend headlessly and return its scene digest plus resolution info."""
    phases = _run(INSPECT_SCRIPT, {"INSPECT_BLEND": str(blend_path)})
    if "digest" not in phases:
        raise FixtureError("inspection produced no digest")
    return {
        "digest": phases["digest"]["digest"],
        "resolution": phases.get("resolution", {}),
    }


def move_on_blend(
    blend_path: Path, plan: dict[str, Any], save: bool = True
) -> dict[str, dict]:
    """Run one move_object plan against a .blend, optionally saving it."""
    return _run(
        MOVE_SCRIPT,
        {
            "MOVE_BLEND": str(blend_path),
            "MOVE_PLAN": json.dumps(plan),
            "MOVE_SAVE": "1" if save else "0",
        },
    )


def scene_state_digest(inspection: dict[str, Any]) -> str:
    """A stable hash of only the spec-relevant scene state.

    Used for the determinism check: two independent generations must produce the
    same value even though their .blend binaries differ.

    EVOLVED IN SPEC 002, TASK 1. This used to hash the inspection dictionary
    directly. It now projects that dictionary into the canonical scene shape and
    delegates to ``studio_contracts.scene.compute_scene_version``, so the
    repository has exactly ONE definition of "scene state digest". Two subtly
    different definitions would mean two answers to "did the scene change?", which
    is the question the Spec 002 execution precondition depends on.

    What the delegation changes, stated plainly:
      - the value is now prefixed (``sha256:...``) and quantised to the documented
        1e-6 digest quantum instead of the previous 9-decimal rounding;
      - ``scene.name`` no longer participates, because it is not part of the
        canonical projection. Nothing is lost: the Blender fixture tests assert the
        scene name directly and compare the whole inspection dictionaries, which is
        a stronger and clearer check than folding it into a hash.

    The properties Spec 001 relied on are unchanged: identical scene state hashes
    identically, key order is irrelevant, and any change to a position, rotation,
    scale, dimension, unit configuration, object identity or object membership
    changes the value.
    """
    return compute_scene_version(scene_body_from_inspection(inspection))


def scene_body_from_inspection(inspection: dict[str, Any]) -> dict[str, Any]:
    """Project a Task 5 inspection dictionary into the canonical scene shape.

    An ADAPTER, deliberately living in test tooling rather than in the contract
    layer: ``inspect_blend`` predates the SceneSnapshot contract and reports a
    fixture-shaped dictionary. Spec 002 Task 3 replaces it with a real
    ``inspect_scene`` operation that produces a canonical SceneSnapshot directly,
    at which point this adapter disappears.

    Two fields the old inspection does not report are supplied here, and the
    substitution is deliberate rather than incidental:
      - ``visible``: assumed True. The fixture scene has no hidden objects, and the
        old digest did not cover visibility either, so no coverage is lost.
      - ``material``: omitted. Same reasoning.
    """
    scene = inspection["digest"]["scene"]
    return {
        "units": {
            "unit_system": scene["unit_system"],
            "length_unit": scene["length_unit"],
            "scale_length": scene["scale_length"],
        },
        "objects": [
            {
                "studio_object_id": entry.get("object_id"),
                "name": entry["name"],
                "object_type": entry["type"],
                "world_position_meters": entry["world_position_meters"],
                "dimensions_meters": entry["dimensions_meters"],
                "rotation_euler_radians": entry["rotation_euler_radians"],
                "scale": entry["scale"],
                "visible": True,
            }
            for entry in inspection["digest"]["objects"]
        ],
    }


def spec_mismatches(digest: dict[str, Any]) -> list[str]:
    """Differences between an observed scene digest and the fixture spec.

    Returns human-readable descriptions; an empty list means the fixture matches
    its documented contract.
    """
    spec = load_spec()
    problems: list[str] = []

    scene_spec = spec["scene"]
    scene = digest["scene"]
    if scene["unit_system"] != scene_spec["unit_system"]:
        problems.append(
            f"unit_system {scene['unit_system']!r} != {scene_spec['unit_system']!r}"
        )
    if scene["length_unit"] != scene_spec["length_unit"]:
        problems.append(
            f"length_unit {scene['length_unit']!r} != {scene_spec['length_unit']!r}"
        )
    if abs(scene["scale_length"] - scene_spec["scale_length"]) > 1e-9:
        problems.append(
            f"scale_length {scene['scale_length']} != {scene_spec['scale_length']}"
        )

    actual_names = sorted(entry["name"] for entry in digest["objects"])
    expected_names = sorted(scene_spec["expected_object_names"])
    if actual_names != expected_names:
        problems.append(f"objects {actual_names} != {expected_names}")

    by_name = {entry["name"]: entry for entry in digest["objects"]}
    for object_spec in spec["objects"]:
        found = by_name.get(object_spec["name"])
        if found is None:
            problems.append(f"missing object {object_spec['name']!r}")
            continue
        if found["object_id"] != object_spec["object_id"]:
            problems.append(
                f"{object_spec['name']} object_id {found['object_id']!r} != "
                f"{object_spec['object_id']!r}"
            )
        if found["type"] != object_spec["type"]:
            problems.append(
                f"{object_spec['name']} type {found['type']!r} != "
                f"{object_spec['type']!r}"
            )
        for axis in ("x", "y", "z"):
            expected = float(object_spec["location_meters"][axis])
            actual = float(found["world_position_meters"][axis])
            if abs(actual - expected) > 1e-6:
                problems.append(
                    f"{object_spec['name']} position {axis} {actual} != {expected}"
                )
            expected_dim = float(object_spec["expected_dimensions_meters"][axis])
            actual_dim = float(found["dimensions_meters"][axis])
            if abs(actual_dim - expected_dim) > 1e-6:
                problems.append(
                    f"{object_spec['name']} dimension {axis} {actual_dim} != "
                    f"{expected_dim}"
                )
    return problems

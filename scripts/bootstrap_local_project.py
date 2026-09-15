"""Put a working project where the local worker can find it.

Spec 001, Task 11. DEVELOPMENT TOOLING — not part of any shipped service.

    python scripts/bootstrap_local_project.py

Generates the Task 5 seed fixture (if needed) and copies it to
``runtime/projects/proj_seed.blend``, which is where ``python -m blender_worker``
looks by convention.

Why a copy, not the fixture itself
----------------------------------
The canonical fixture under ``tests/fixtures/blender/build/`` is a TEST input and
must stay pristine: `working_copy()` exists precisely so tests never mutate it. The
local worker, by contrast, is meant to mutate its project — that is the whole point
of the demo. So it gets its own copy under the git-ignored ``runtime/`` tree.

Re-running resets the project back to Cube X = 0, which is what you want between
demo runs. Pass ``--keep`` to leave an existing project alone.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# studio_fixtures is test tooling and deliberately not installed as a package.
sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))

DEFAULT_PROJECTS_ROOT = REPO_ROOT / "runtime" / "projects"
DEFAULT_PROJECT_ID = "proj_seed"


def bootstrap(
    project_id: str = DEFAULT_PROJECT_ID,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    keep_existing: bool = False,
) -> Path:
    from studio_fixtures.seed_project import ensure_seed_project

    destination = projects_root / f"{project_id}.blend"
    if destination.exists() and keep_existing:
        print(f"keeping existing project: {destination}")
        return destination

    print("generating the seed fixture (this runs Blender once)...")
    canonical = ensure_seed_project()

    projects_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(canonical, destination)
    print(f"project ready: {destination}")
    print(f"  project_id: {project_id}")
    print("  Cube X:     0.00 m")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a local working project for the Blender worker."
    )
    parser.add_argument(
        "--project-id",
        default=DEFAULT_PROJECT_ID,
        help=f"logical project id (default: {DEFAULT_PROJECT_ID})",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help="directory the worker reads projects from",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave an existing project in place instead of resetting it",
    )
    args = parser.parse_args()

    try:
        bootstrap(args.project_id, args.projects_root, args.keep)
    except Exception as exc:
        print(f"could not prepare the project: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

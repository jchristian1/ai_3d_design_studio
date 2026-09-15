"""Regenerate, verify, or inspect the Spec 001 seed Blender fixture.

Usage (from the repository root):

    python3 -m studio_fixtures.regenerate            # regenerate + verify
    python3 -m studio_fixtures.regenerate --verify   # verify only
    python3 -m studio_fixtures.regenerate --inspect  # print the scene digest

Requires PYTHONPATH to include the studio package roots; the simplest way is to
run it via the repo's pytest configuration paths:

    PYTHONPATH=packages/types/python:packages/contracts/python:\
packages/validation/python:packages/spatial/python:services/blender-mcp:tests/fixtures \
      python3 -m studio_fixtures.regenerate
"""

from __future__ import annotations

import argparse
import json
import sys

from blender_mcp.blender_runtime import blender_version, find_blender_executable

from .seed_project import (
    FixtureError,
    generate_seed_project,
    inspect_blend,
    load_spec,
    scene_state_digest,
    seed_blend_path,
    spec_mismatches,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true", help="verify the existing fixture only"
    )
    parser.add_argument(
        "--inspect", action="store_true", help="print the scene digest as JSON"
    )
    args = parser.parse_args(argv)

    if find_blender_executable() is None:
        print("Blender executable not found; cannot work with the fixture.")
        return 2
    print(f"Blender: {blender_version()}")

    spec = load_spec()
    print(f"Fixture spec version: {spec['fixture_version']}")

    target = seed_blend_path()

    try:
        if not args.verify and not args.inspect:
            print(f"Generating {target} ...")
            generate_seed_project()
            print("Generated.")

        if not target.exists():
            print(f"Fixture missing: {target}")
            print("Run without --verify/--inspect to generate it.")
            return 1

        inspection = inspect_blend(target)

        if args.inspect:
            print(json.dumps(inspection, indent=2, sort_keys=True))

        problems = spec_mismatches(inspection["digest"])
        if problems:
            print("Fixture does NOT match its spec:")
            for problem in problems:
                print(f"  - {problem}")
            return 1

        print(f"Fixture matches spec. Scene digest: {scene_state_digest(inspection)}")
        return 0
    except FixtureError as exc:
        print(f"Fixture error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

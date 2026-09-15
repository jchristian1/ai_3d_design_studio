# Seed Blender Project (Spec 001)

The deterministic starting state for every integration and E2E run: a scene
containing exactly one Cube at the world origin, so a run always begins from
**Cube X = 0**.

## What it contains

| Property | Value |
|---|---|
| Object display name | `Cube` |
| Stable object id | `obj_cube001` (custom property `studio_object_id`) |
| World position | `(0.0, 0.0, 0.0)` meters |
| Rotation / scale | identity |
| Dimensions | 2 x 2 x 2 m |
| Unit system | `METRIC`, length unit `METERS`, `scale_length` 1.0 |
| Other objects | none — the scene starts empty, so no default camera or light |

`seed_project.spec.json` is the machine-readable source of truth for all of the
above. The generator reads it, and tests verify a generated file against it, so
the fixture and its documented contract cannot drift apart.

## Layout

```
tests/fixtures/blender/
├── seed_project.spec.json        machine-readable spec (committed)
├── generate_seed_project.py      runs INSIDE Blender, builds the scene
├── inspect_blend.py              runs INSIDE Blender, emits a scene digest
├── move_on_blend.py              runs INSIDE Blender, applies a move_object plan
├── README.md                     this file
└── build/
    └── seed_project.blend        GENERATED, git-ignored

tests/fixtures/studio_fixtures/
├── seed_project.py               host-side helper (generate, working_copy, verify)
└── regenerate.py                 CLI entry point
```

## Generating it

```bash
PYTHONPATH=packages/types/python:packages/contracts/python:packages/validation/python:packages/spatial/python:services/blender-mcp:tests/fixtures \
  python3 -m studio_fixtures.regenerate
```

The command generates `build/seed_project.blend`, then re-opens it and verifies
it against the spec. Other modes:

```bash
python3 -m studio_fixtures.regenerate --verify    # verify the existing file only
python3 -m studio_fixtures.regenerate --inspect   # print the scene digest as JSON
```

Blender is located by `blender_mcp.blender_runtime.find_blender_executable()`
(`BLENDER_EXECUTABLE`, then `PATH`, then known locations including
`/snap/bin/blender`) and invoked with `--background --factory-startup`. No script
here contains a Blender path.

From Python:

```python
from studio_fixtures.seed_project import generate_seed_project, working_copy
generate_seed_project()          # force-regenerate the canonical fixture
```

## Why the .blend is not committed

The generator script and the spec are committed; the binary is not.

- It is ~0.5 MB of unreviewable binary that would grow the repository on every
  regeneration.
- Blender embeds its version and absolute file paths in the container, so the
  file is **not** byte-for-byte reproducible. A committed copy would produce
  meaningless diffs and could silently drift from the generator.
- The generator is fast and fully scripted, so the file is cheap to recreate.

No attempt is made to force binary determinism. What *is* guaranteed reproducible
is the scene state Spec 001 depends on — object list, names, stable ids, world
transforms, dimensions, and unit configuration. `scene_state_digest()` hashes
exactly those fields, and a test asserts two independent generations produce the
same digest.

## How tests get an isolated copy

```python
from studio_fixtures.seed_project import working_copy

with working_copy() as blend_path:
    ...  # mutate blend_path freely
# temp directory removed here; the canonical fixture was never written to
```

`working_copy()` copies the canonical file into a fresh temporary directory,
yields the copy, and deletes the directory afterwards even if the test fails. The
canonical file is only ever read. This is what lets repeated E2E runs each start
from Cube X = 0 with no state leaking between them.

Two tests enforce this rather than trusting it: one asserts a fresh copy is back
at X = 0 after a previous copy was moved to 0.5, and another compares the
canonical file's SHA-256 before and after mutating copies.

## Inspecting it manually

Fastest, no UI:

```bash
PYTHONPATH=... python3 -m studio_fixtures.regenerate --inspect
```

In the Blender UI, if you want to look around:

```bash
/snap/bin/blender tests/fixtures/blender/build/seed_project.blend
```

The stable id lives on the object as a custom property; in the UI it is under
Object Properties → Custom Properties → `studio_object_id`.

## Resetting it

Regeneration *is* the reset — it rebuilds from factory settings with an empty
scene:

```bash
PYTHONPATH=... python3 -m studio_fixtures.regenerate
```

Or delete `build/` and let the next test regenerate it: `ensure_seed_project()`
creates the file if it is absent.

## Tests

```bash
python3 -m pytest tests/fixtures        # spec + helper logic, no Blender
python3 -m pytest -m blender            # real Blender, opt-in and slower
```

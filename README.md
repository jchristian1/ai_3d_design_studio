# AI 3D Design Studio

Browser-based AI 3D design application: modify Blender scenes with natural
language. See `.kiro/steering/` for product, architecture and engineering
direction, and `.kiro/specs/001-core-vertical-slice/` for the current milestone.

## Local development setup

Python dependencies **and the monorepo's own packages** are declared in
`pyproject.toml` and installed into a project-local virtual environment. This is
the supported model — do not install into system Python.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

`.venv/` is git-ignored. `-e ".[dev]"` installs the runtime dependencies plus the
test tooling, and makes the runtime packages importable directly:

```
studio_types  studio_contracts  studio_validation  studio_spatial
studio_agent  studio_api        blender_mcp        blender_worker
```

The editable install maps each package to its existing location under
`packages/*/python/` and `services/*/` — nothing is copied or moved. So
`python -c "import studio_api"` works from any directory with no `PYTHONPATH`,
which is what lets a service be launched from the venv outside pytest.

Node dependencies (workspace links only, no external packages):

```bash
npm install
```

## Running the control plane

```bash
source .venv/bin/activate
uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`--factory` is required: `services/api` exposes an application factory rather than a
module-level `app`, so configuration is validated per application. Workers then
connect outbound to `ws://127.0.0.1:8000/ws/workers`.

Interactive API documentation is served at `/docs`. Configuration and the full route
list are documented in `services/api/README.md`.

```bash
export STUDIO_WORKER_TOKEN=...          # never commit; shared with the worker
export STUDIO_API_ENVIRONMENT=local     # defaults shown in services/api/README.md
```

## Running tests

With the virtual environment active:

```bash
# Fast Python suite (real-Blender tests excluded by default)
pytest

# TypeScript suite
npm run test:ts

# Integration: real HTTP + real WebSockets, fake Blender
pytest tests/integration

# Real-Blender tests — opt-in and slower
pytest -m blender

# The full slice: HTTP -> agent -> worker -> real Blender
pytest -m blender tests/e2e/test_api_blender_e2e.py
```

Without activating, prefix with the interpreter: `.venv/bin/python -m pytest`.

The test suite imports the runtime packages from the editable install, so run the
install step first. `pyproject.toml` keeps exactly one pytest `pythonpath` entry,
`tests/fixtures`, for the `studio_fixtures` seed-project tooling — that is test
tooling, not a runtime package, so it is deliberately not installed.

## Layout

```
packages/contracts   canonical JSON Schemas + TS/Python representations
packages/types       shared data types
packages/spatial     deterministic unit/direction utilities (no Blender, no AI)
packages/validation  reusable validation rules
services/agent       AgentProvider abstraction + deterministic RuleBasedProvider
services/api         FastAPI control plane (services/api/README.md);
                     worker-link boundary (worker_link/PROTOCOL.md)
services/blender-mcp semantic Blender operations (move_object) behind MCP
services/blender-worker  job execution, journal, locks, control-plane link
tests/fixtures       deterministic seed Blender project (generated, not committed)
                     plus the in-process control-plane test harness
tests/integration    real HTTP/WebSocket integration tiers
tests/e2e            HTTP -> agent -> worker -> real Blender
```

## Blender

Real-Blender tests locate the executable via
`blender_mcp.blender_runtime.find_blender_executable()`, which checks
`BLENDER_EXECUTABLE`, then `PATH`, then known locations including
`/snap/bin/blender`. No Blender path is hard-coded anywhere else.

Regenerate the seed fixture:

```bash
python -m studio_fixtures.regenerate
```

## Worker environment

The worker reads its identity and credentials from the environment; nothing
workstation-specific is in source:

```bash
export STUDIO_WORKER_ID=worker_legion_1
export STUDIO_WORKER_TOKEN=...        # never commit
export STUDIO_CONTROL_PLANE_URL=ws://127.0.0.1:8765/ws/workers
export STUDIO_WORKER_GPU_NAME="NVIDIA RTX 4070 Ti"   # optional
```

Generated runtime state (execution journal, locks, recovery snapshots) lives under
the git-ignored `runtime/` directory.

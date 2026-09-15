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

## Running the studio locally

Three processes: the control plane, the Blender worker, and the web app.

**Once**, create a project for the worker to open (this runs Blender briefly and
resets the project to Cube X = 0):

```bash
source .venv/bin/activate
python scripts/bootstrap_local_project.py
```

**Terminal 1 — control plane**

```bash
source .venv/bin/activate
export STUDIO_WORKER_TOKEN=local-dev-token
uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000
```

**Terminal 2 — Blender worker**

```bash
source .venv/bin/activate
export STUDIO_WORKER_ID=worker_local_1
export STUDIO_WORKER_TOKEN=local-dev-token          # same token as the API
export STUDIO_CONTROL_PLANE_URL=ws://127.0.0.1:8000/ws/workers
export STUDIO_WORKER_GPU_NAME="NVIDIA RTX 4070 Ti"  # optional
python -m blender_worker
```

Wait for `registered; waiting for design changes`.

**Terminal 3 — web app**

```bash
cd apps/web
npm install
npm run dev
```

Then open **http://localhost:3000**, type `Move Cube 50 cm to the right.`, and press
Send. The step-by-step acceptance walkthrough is in `apps/web/README.md`.

`--factory` is required for the API: `services/api` exposes an application factory
rather than a module-level `app`, so configuration is validated per application. The
worker connects **outbound** to the control plane, so the workstation needs no
inbound port and Blender is never exposed.

Interactive API documentation is served at http://127.0.0.1:8000/docs. Configuration
for each service is documented in `services/api/README.md`,
`services/preview/README.md`, and `apps/web/README.md`.

## Running tests

With the virtual environment active:

```bash
# Fast Python suite (real-Blender tests excluded by default)
pytest

# Shared TypeScript packages
npm run test:ts

# Browser interface
npm test --workspace @studio/web

# Integration: real HTTP + real WebSockets, fake Blender
pytest tests/integration

# Real-Blender tests — opt-in and slower
pytest -m blender

# The full slice: HTTP -> agent -> worker -> real Blender -> preview PNG
pytest -m blender tests/e2e
```

Without activating, prefix with the interpreter: `.venv/bin/python -m pytest`.

The test suite imports the runtime packages from the editable install, so run the
install step first. `pyproject.toml` keeps exactly one pytest `pythonpath` entry,
`tests/fixtures`, for the `studio_fixtures` seed-project tooling — that is test
tooling, not a runtime package, so it is deliberately not installed.

## Layout

```
apps/web             browser interface (apps/web/README.md)
packages/contracts   canonical JSON Schemas + TS/Python representations
packages/types       shared data types
packages/spatial     deterministic unit/direction utilities (no Blender, no AI)
packages/validation  reusable validation rules
services/agent       AgentProvider abstraction + deterministic RuleBasedProvider
services/api         FastAPI control plane (services/api/README.md);
                     worker-link boundary (worker_link/PROTOCOL.md)
services/blender-mcp semantic Blender operations (move_object) behind MCP
services/preview     preview generation + artifact store (services/preview/README.md)
scripts              local development helpers
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

The worker runs as `python -m blender_worker` and reads its identity and credentials
from the environment; nothing workstation-specific is in source:

```bash
export STUDIO_WORKER_ID=worker_legion_1
export STUDIO_WORKER_TOKEN=...        # never commit; must match the API's
export STUDIO_CONTROL_PLANE_URL=ws://127.0.0.1:8000/ws/workers
export STUDIO_WORKER_GPU_NAME="NVIDIA RTX 4070 Ti"   # optional
export STUDIO_WORKER_PROJECTS_ROOT=...               # default: runtime/projects
```

`project_id` resolves to `<projects root>/<project_id>.blend`, chosen by the worker
from local configuration. A job can never carry a filesystem path.

Generated runtime state (execution journal, locks, recovery snapshots, and preview
artifacts under `runtime/artifacts/`) lives in the git-ignored `runtime/` directory.

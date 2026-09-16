# AI 3D Design Studio

Browser-based AI 3D design application: describe a change in plain language, and
Blender makes it. Upload floor plans and reference photos, talk to the design
assistant, watch the model update in the browser.

See `.kiro/steering/` for product, architecture and engineering direction,
`.kiro/specs/001-core-vertical-slice/` for the first vertical slice, and
`.kiro/specs/002-intelligent-scene-agent/` for the current milestone.

Two things are worth knowing before reading further:

- **Blender is driven through the official Blender Lab MCP**
  (`https://projects.blender.org/lab/blender_mcp.git`), pinned in
  `external/official-blender-mcp.pin.json`. It is installed by a script, never
  vendored, and never exposed to the network.
- **There is no OpenAI API key anywhere.** The design assistant is reached through
  the locally installed Codex CLI using your own ChatGPT sign-in. A test
  (`tests/security/test_no_api_key.py`) enforces that over the real source tree.

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

Node dependencies (workspace links only, plus three.js for the 3D viewer):

```bash
npm install
```

The design assistant needs the official Codex CLI and the official Blender MCP:

```bash
npm install -g @openai/codex@0.154.0      # provides `codex`
python scripts/setup_official_blender_mcp.py
```

`setup_official_blender_mcp.py` clones the canonical Blender Lab repository at the
pinned commit into `runtime/external/` and installs the MCP server into the venv.
Pass `--check` to verify an existing install, `--force` to reinstall. The PyPI package
called `blender-mcp` is a DIFFERENT, community project — do not install it.

You do not need to run `codex login` from a terminal: the workspace offers a
**Sign in with ChatGPT** button, which starts the official sign-in and shows the link
(or a one-time code) in the browser. Nothing in this project ever sees your password.

## Running the studio locally

All three processes at once:

```bash
./scripts/run_studio.sh
```

Then open **http://localhost:3000**. That script is convenience only; it runs exactly
the three commands below, generating a shared worker token per run.

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

Then open **http://localhost:3000**. Upload a floor plan or a photo, and describe what
you want. Try `Move Cube 50 cm to the right.` on the seed project, or upload a plan and
ask for it to be built. The step-by-step acceptance walkthrough is in
`apps/web/README.md`.

The control plane keeps durable workspace state — projects, references, extracted
facts, conversation, approvals, scene snapshots and artifact index — in SQLite at
`runtime/studio.sqlite3`, with uploads under `runtime/references/`. Override with
`STUDIO_API_DATABASE_PATH` and `STUDIO_API_REFERENCE_ROOT`. The `.blend` file remains
the authoritative 3D state; the database only caches what the browser and the agent
need.

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

# The full offline stack: HTTP -> agent -> job -> worker -> capability backend
pytest tests/e2e tests/security

# Real-Blender tests — opt-in and slower
pytest -m blender

# The official Blender MCP against real Blender — opt-in
pytest -m mcp

# Real Astra through the Codex CLI — opt-in, needs a ChatGPT sign-in
pytest -m codex

# The full slice: HTTP -> agent -> worker -> real Blender -> preview PNG
pytest -m blender tests/e2e
```

The default suite excludes the `blender`, `mcp` and `codex` markers, so it runs in
seconds and needs neither Blender nor a network. The offline tiers are not stubs of
the interesting parts: they run the real routes, the real job contracts, the real
project lock and journal, and substitute only the two ends — the model
(`FakeLlmProvider`) and Blender (`FakeBlenderCapabilityProvider`), each at the same
Protocol boundary the production wiring uses.

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
packages/spatial     deterministic unit/direction/colour utilities (no Blender, no AI)
packages/validation  reusable validation rules, incl. model-code risk classification
services/agent       AgentProvider abstraction, Codex/Astra provider, proposal validation
services/api         FastAPI control plane (services/api/README.md); design chat,
                     uploads, approvals, workspace state;
                     worker-link boundary (worker_link/PROTOCOL.md)
services/blender-mcp semantic Blender operations (move_object) behind MCP
services/preview     preview generation + artifact store (services/preview/README.md)
scripts              local development helpers
services/blender-worker  job execution, capability plans, journal, locks, official
                     Blender MCP backend, control-plane link
tests/fixtures       deterministic seed Blender project (generated, not committed),
                     the in-process control-plane harness, and the offline full-stack
                     design harness (studio_fixtures/design_stack.py)
tests/integration    real HTTP/WebSocket integration tiers
tests/e2e            full-stack acceptance: offline by default, real Blender with -m blender
tests/mcp            the official Blender MCP against real Blender (-m mcp)
tests/security       no-API-key guard, layering guards, hostile model output
```

## Model-authored Python, approvals, and the real boundary

The assistant prefers specific capabilities (`create_wall`, `move_object`,
`set_material_color`, …), which are validated, verified after execution, and safe to
retry. It may also author Blender Python for things those capabilities cannot express.
That code is read with `ast` before anything runs
(`packages/validation/python/studio_validation/code_risk.py`) and classified:

- **scene-only** code runs unattended;
- code reaching the **filesystem, the network, a subprocess or `eval`/`exec`** stops and
  is shown to you in the chat, with the actual code and a plain-language reason. You
  approve or reject it. Approval is bound to a hash of that exact code, so it cannot be
  moved onto different code.

Be clear about what this is: **visibility and friction, not containment.** A determined
author defeats a static classifier, and the official Blender MCP executes what it is
given — its own `weak_sandbox.py` says as much. The real boundary is the operating
system. If you care about that, run the worker and Blender as a restricted user, or in a
container with only the project directory mounted.

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

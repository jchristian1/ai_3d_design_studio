#!/usr/bin/env bash
#
# Start the whole studio locally: control plane, Blender worker, web app.
#
#   ./scripts/run_studio.sh
#
# Then open http://localhost:3000.
#
# Ctrl-C stops all three. Logs from each process are prefixed, so one terminal is
# enough; run the three commands from README.md separately if you want them apart.
#
# Nothing here is required by the product — it is convenience for one developer on one
# machine. The processes, the environment variables and the ports are exactly the ones
# documented in README.md.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

# --- preconditions --------------------------------------------------------

if [[ ! -x .venv/bin/python ]]; then
  echo "No .venv found. Run:  python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -d node_modules ]]; then
  echo "No node_modules found. Run:  npm install" >&2
  exit 1
fi

PYTHON="$ROOT/.venv/bin/python"

# The worker and the API must share one token. Generated per run unless supplied, so no
# secret is ever committed and a stale token cannot linger in a shell profile.
export STUDIO_WORKER_TOKEN="${STUDIO_WORKER_TOKEN:-$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')}"
export STUDIO_WORKER_ID="${STUDIO_WORKER_ID:-worker_local_1}"
export STUDIO_CONTROL_PLANE_URL="${STUDIO_CONTROL_PLANE_URL:-ws://127.0.0.1:8000/ws/workers}"
export STUDIO_API_PROJECT_IDS="${STUDIO_API_PROJECT_IDS:-proj_seed}"

# The official Blender MCP is the Blender backend. Install it once; this only checks.
if ! "$PYTHON" scripts/setup_official_blender_mcp.py --check >/dev/null 2>&1; then
  echo "The official Blender MCP is not installed yet. Installing it now..."
  "$PYTHON" scripts/setup_official_blender_mcp.py
fi

# A project for the worker to open.
if [[ ! -f runtime/projects/proj_seed.blend ]]; then
  echo "Creating the local project (runs Blender briefly)..."
  "$PYTHON" scripts/bootstrap_local_project.py
fi

# --- run ------------------------------------------------------------------

pids=()

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "stopping..."
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

prefix() { sed -u "s/^/[$1] /"; }

echo "control plane -> http://127.0.0.1:8000    (docs at /docs)"
"$PYTHON" -m uvicorn studio_api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 2>&1 | prefix api &
pids+=($!)

# Give the control plane a moment to bind before the worker dials out. The worker
# retries with backoff anyway, so this only keeps the log tidy.
sleep 2

echo "blender worker -> connecting outbound (Blender is never exposed)"
"$PYTHON" -m blender_worker 2>&1 | prefix worker &
pids+=($!)

echo "web app -> http://localhost:3000"
npm run dev --workspace @studio/web 2>&1 | prefix web &
pids+=($!)

echo
echo "Open http://localhost:3000 . If Astra is not connected yet, the workspace shows"
echo "a 'Sign in with ChatGPT' button — that is a one-time browser sign-in."
echo

wait

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

# --- node / codex on PATH -------------------------------------------------
#
# `codex`, `node` and `npm` are commonly installed via nvm, whose bin directory is only
# on PATH in shells that have sourced nvm. When the studio is launched outside such a
# shell (the desktop icon, a fresh terminal, a service manager) that directory is absent,
# so `shutil.which("codex")` in the control plane fails and the workspace reports
# "Codex is not installed" even though it is. Sourcing nvm here makes the launch method
# irrelevant. This is a no-op when nvm is not installed or codex is already found.
if ! command -v codex >/dev/null 2>&1; then
  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    # shellcheck disable=SC1091
    \. "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true
    # The default node version may not be the one that carries codex, so if codex is
    # still not visible, walk installed versions and put the first one that has it on PATH.
    if ! command -v codex >/dev/null 2>&1; then
      for candidate in "$NVM_DIR"/versions/node/*/bin; do
        if [[ -x "$candidate/codex" ]]; then
          PATH="$candidate:$PATH"
          break
        fi
      done
    fi
  fi
fi

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

# How long one Astra turn may take. An ambitious scene ("furnish this room realistically")
# asks the model to author a large program and can legitimately run for many minutes, so
# the ceiling is generous. The browser turn poll (`TURN_TIMEOUT_MS`) is kept >= this so a
# real answer is never thrown away. Override by exporting STUDIO_CODEX_TIMEOUT_SECONDS.
export STUDIO_CODEX_TIMEOUT_SECONDS="${STUDIO_CODEX_TIMEOUT_SECONDS:-1500}"

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
# --reload so an edit to the control plane takes effect without anyone restarting this
# script. All three processes now pick up their own changes: the web app through Next's
# fast refresh, the worker by re-executing itself when its source changes, and the API
# here. A long-running process quietly serving replaced code was costing far more
# confusion than it was worth -- every symptom looked like a product bug.
#
# Scoped to the directories the API actually imports, because reloading on every write
# anywhere in the repository (including runtime/ artifacts the API itself produces) would
# restart it constantly.
"$PYTHON" -m uvicorn studio_api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 \
  --reload \
  --reload-dir services/api \
  --reload-dir services/agent \
  --reload-dir packages/contracts/python \
  --reload-dir packages/types/python \
  --reload-dir packages/validation/python \
  --reload-dir packages/spatial/python 2>&1 | prefix api &
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

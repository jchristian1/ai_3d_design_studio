#!/usr/bin/env bash
#
# Start the studio and open it in the browser. This is what the desktop icon runs.
#
#   ./scripts/launch_studio.sh          start (or reuse) the studio, then open it
#   ./scripts/launch_studio.sh --stop   stop everything it started
#
# Differences from run_studio.sh, which this wraps:
#
#   * it detaches, so closing a window or a terminal does not kill the studio;
#   * it is idempotent — launching again when it is already up just opens the browser;
#   * logs go to a file instead of a terminal nobody is watching;
#   * it reports failures with a desktop notification, because a double-clicked icon
#     that does nothing is indistinguishable from a broken program.

set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

LOG_DIR="$ROOT/runtime/logs"
LOG_FILE="$LOG_DIR/studio.log"
PID_FILE="$ROOT/runtime/studio.pid"
API_URL="http://127.0.0.1:8000/health"
WEB_URL="http://localhost:3000"

mkdir -p "$LOG_DIR" "$ROOT/runtime"

say() {
  printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

notify() {
  # Best effort: a desktop without notify-send still gets the log file.
  command -v notify-send >/dev/null 2>&1 &&
    notify-send --app-name="AI 3D Design Studio" "$1" "${2:-}" 2>/dev/null
  say "$1 ${2:-}"
}

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$WEB_URL" >/dev/null 2>&1 &
  else
    say "open $WEB_URL in your browser"
  fi
}

web_is_up() {
  curl -fsS --max-time 2 -o /dev/null "$WEB_URL" 2>/dev/null
}

api_is_up() {
  curl -fsS --max-time 2 -o /dev/null "$API_URL" 2>/dev/null
}

stop_studio() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      say "stopping the studio (pid $pid)"
      # Negative pid: the whole process group, so uvicorn, the worker and Next all go.
      kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      sleep 2
      kill -KILL "-$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  say "stopped"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_studio
  exit 0
fi

# --- already running? -----------------------------------------------------

if web_is_up; then
  say "the studio is already running; opening it"
  open_browser
  exit 0
fi

# --- preconditions --------------------------------------------------------

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  notify "The studio is not set up yet" \
    "Run: python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'"
  exit 1
fi

if [[ ! -d "$ROOT/node_modules" ]]; then
  notify "The studio is not set up yet" "Run: npm install"
  exit 1
fi

# --- start ----------------------------------------------------------------

: > "$LOG_FILE"
say "starting the studio (logs: $LOG_FILE)"

# setsid gives the children their own process group, which is what lets --stop take the
# whole studio down and what stops it dying with the launching window.
setsid bash "$ROOT/scripts/run_studio.sh" >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# Next.js compiles on first request, and Blender's first read takes a few seconds, so the
# wait is generous. A hard deadline is still better than an icon that hangs forever.
DEADLINE=$((SECONDS + 180))
until web_is_up; do
  if (( SECONDS > DEADLINE )); then
    notify "The studio did not start" "See $LOG_FILE"
    exit 1
  fi
  sleep 1
done

open_browser

if api_is_up; then
  say "ready: $WEB_URL"
else
  # The browser opens either way: the interface explains that the service is missing far
  # better than a notification can.
  notify "The studio interface is up, but the design service is not" "See $LOG_FILE"
fi

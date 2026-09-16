#!/usr/bin/env bash
#
# Put an "AI 3D Design Studio" icon on the desktop, and a note next to it with the
# terminal commands.
#
#   ./scripts/install_desktop_launcher.sh
#
# Installs three things:
#
#   ~/Desktop/AI 3D Design Studio.desktop            double-click to launch
#   ~/.local/share/applications/…                    so it also appears in the app grid
#   ~/Desktop/AI 3D Design Studio - commands.txt     the terminal route, written out
#
# Re-running is safe: everything is overwritten in place.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
APPS_DIR="$HOME/.local/share/applications"
LAUNCHER="$ROOT/scripts/launch_studio.sh"
ICON="$ROOT/assets/studio-icon.svg"
NAME="AI 3D Design Studio"
FILE_NAME="$NAME.desktop"
NOTES="$DESKTOP_DIR/$NAME - commands.txt"

mkdir -p "$DESKTOP_DIR" "$APPS_DIR"
chmod +x "$LAUNCHER"

write_entry() {
  cat > "$1" <<ENTRY
[Desktop Entry]
Type=Application
Version=1.0
Name=$NAME
GenericName=AI 3D design
Comment=Describe a change in plain language and Blender makes it
Exec=$LAUNCHER
Icon=$ICON
Terminal=false
Categories=Graphics;3DGraphics;
Keywords=blender;3d;design;ai;astra;
StartupNotify=true
Actions=Stop;

[Desktop Action Stop]
Name=Stop the studio
Exec=$LAUNCHER --stop
ENTRY
  chmod +x "$1"
}

write_entry "$DESKTOP_DIR/$FILE_NAME"
write_entry "$APPS_DIR/ai-3d-design-studio.desktop"

# GNOME refuses to run a desktop file it does not trust, and shows the raw text instead.
# Marking it trusted is what makes a double-click actually launch.
if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_DIR/$FILE_NAME" metadata::trusted true 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

cat > "$NOTES" <<NOTES_END
AI 3D DESIGN STUDIO — starting it by hand
=========================================

Normally you do not need any of this: double-click "$NAME" on the desktop.
This file is for when you want a terminal, or something has gone wrong.

Project folder:
  $ROOT


EVERYTHING AT ONCE
------------------
  cd $ROOT
  ./scripts/run_studio.sh

Then open:  http://localhost:3000
Stop it with Ctrl-C in that terminal.


THE SAME THING, DETACHED (what the desktop icon does)
-----------------------------------------------------
  $ROOT/scripts/launch_studio.sh            # start and open the browser
  $ROOT/scripts/launch_studio.sh --stop     # stop it

Logs:
  $ROOT/runtime/logs/studio.log


THE THREE PROCESSES SEPARATELY
------------------------------
Use these when you want to watch one of them. The API and the worker must share the
same token, so pick any value and export it in BOTH terminals.

Terminal 1 — control plane (the API):
  cd $ROOT
  source .venv/bin/activate
  export STUDIO_WORKER_TOKEN=pick-any-value
  uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000

Terminal 2 — Blender worker (this one talks to Blender):
  cd $ROOT
  source .venv/bin/activate
  export STUDIO_WORKER_TOKEN=pick-any-value          # the SAME value
  export STUDIO_CONTROL_PLANE_URL=ws://127.0.0.1:8000/ws/workers
  export STUDIO_WORKER_ID=worker_local_1
  python -m blender_worker

Terminal 3 — the interface:
  cd $ROOT
  npm run dev --workspace @studio/web

Then open:  http://localhost:3000


SIGNING IN TO ASTRA
-------------------
The studio uses your ChatGPT account through the official Codex client. There is no API
key anywhere. Click "Sign in with ChatGPT" in the studio, or from a terminal:

  codex login
  codex login status

If Codex is missing:
  npm install -g @openai/codex@0.154.0


IF SOMETHING LOOKS WRONG
------------------------
"Can't reach the design studio service"
  The API is not running, or an OLD one is still on port 8000 with stale code.
  Check and clear it:
      ss -ltnp | grep 8000
      pkill -f "uvicorn studio_api"
      pkill -f "python -m blender_worker"
  then start again.

"Blender is not connected"
  The worker is not running, or its token does not match the API's.
  Blender itself must be installed:  blender --version

The official Blender MCP is missing
      cd $ROOT
      .venv/bin/python scripts/setup_official_blender_mcp.py

Ports already in use
  8000 is the API, 3000 is the interface. Both are local only — nothing is exposed to
  the network, and Blender is never reachable from outside this machine.


CHECKING THE INSTALL
--------------------
  cd $ROOT
  .venv/bin/python -m pytest -q          # fast suite, no Blender needed
  npm test --workspace @studio/web       # interface
  .venv/bin/python -m pytest -q -m mcp   # against the real Blender (slower)

Written by scripts/install_desktop_launcher.sh — re-run it to refresh this file.
NOTES_END

printf 'Installed:\n  %s\n  %s\n  %s\n' \
  "$DESKTOP_DIR/$FILE_NAME" "$APPS_DIR/ai-3d-design-studio.desktop" "$NOTES"
printf '\nIf the desktop shows a text file instead of an icon, right-click it and choose\n'
printf '"Allow launching" (GNOME asks once per file).\n'

"""Locating the Blender executable — one place, not scattered assumptions.

Spec 001, Task 4.

Resolution order:
  1. ``BLENDER_EXECUTABLE`` environment variable (explicit override wins)
  2. ``blender`` on PATH
  3. Known install locations, including the snap wrapper /snap/bin/blender used
     on this development machine

Nothing else in the codebase should reference a Blender path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: Fallback locations checked after PATH. /snap/bin/blender is the snap wrapper
#: present on the current development workstation.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/snap/bin/blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/opt/blender/blender",
)

ENV_VAR = "BLENDER_EXECUTABLE"


def find_blender_executable() -> Optional[str]:
    """Return a usable Blender executable path, or None when unavailable."""
    override = os.environ.get(ENV_VAR)
    if override:
        return override if Path(override).exists() else None

    found = shutil.which("blender")
    if found:
        return found

    for candidate in CANDIDATE_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def blender_is_available() -> bool:
    """True when Blender can be located and reports a version."""
    executable = find_blender_executable()
    if executable is None:
        return False
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "Blender" in proc.stdout


def blender_version() -> Optional[str]:
    """The first line of ``blender --version``, or None."""
    executable = find_blender_executable()
    if executable is None:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else None


def run_blender_script(
    script_path: str,
    env: Optional[dict[str, str]] = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a Python script inside headless Blender.

    ``--background`` runs without a UI and ``--factory-startup`` ignores user
    preferences and add-ons, so the run is reproducible on any machine.
    """
    executable = find_blender_executable()
    if executable is None:
        raise RuntimeError("Blender executable not found")

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    return subprocess.run(
        [
            executable,
            "--background",
            "--factory-startup",
            "--python",
            script_path,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
    )

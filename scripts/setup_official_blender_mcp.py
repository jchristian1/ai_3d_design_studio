#!/usr/bin/env python3
"""Install the OFFICIAL Blender Lab MCP server at the pinned commit.

The official MCP is an EXTERNAL dependency. This script never copies it into the
repository and never modifies it. It:

  1. clones the canonical repository at the pinned commit into ``runtime/external``
     (gitignored) as a detached checkout,
  2. creates a dedicated virtual environment for it, so its GPL-3.0-or-later
     dependencies stay separate from this project's own environment,
  3. installs the server from its own ``mcp/`` subdirectory,
  4. verifies the installed console entry point exists,
  5. writes a resolved-install descriptor the worker reads at runtime.

The pin lives in ``external/official-blender-mcp.pin.json``. ``latest`` is never used.

Usage::

    python scripts/setup_official_blender_mcp.py            # install / verify
    python scripts/setup_official_blender_mcp.py --force    # rebuild from scratch
    python scripts/setup_official_blender_mcp.py --check    # verify only, no network
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / "external" / "official-blender-mcp.pin.json"
EXTERNAL_ROOT = REPO_ROOT / "runtime" / "external"
SOURCE_DIR = EXTERNAL_ROOT / "blender_mcp_source"
VENV_DIR = EXTERNAL_ROOT / "blender_mcp_venv"
INSTALL_DESCRIPTOR = EXTERNAL_ROOT / "official_blender_mcp_install.json"

# The add-on is copied OUT of the pinned checkout into a staging directory only so a
# human can install it into Blender by hand when interactive tools are wanted. The
# copy is inside gitignored runtime/, never inside the repository's tracked tree.
ADDON_STAGING_DIR = EXTERNAL_ROOT / "blender_mcp_addon_for_manual_install"


class SetupError(RuntimeError):
    """Raised when the pinned official MCP cannot be installed or verified."""


def load_pin() -> dict:
    if not PIN_FILE.exists():
        raise SetupError(f"missing pin file: {PIN_FILE}")
    return json.loads(PIN_FILE.read_text(encoding="utf-8"))


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SetupError(
            "command failed: {}\nstdout: {}\nstderr: {}".format(
                " ".join(command), completed.stdout.strip(), completed.stderr.strip()
            )
        )
    return completed.stdout


def clone_pinned_source(pin: dict, *, force: bool) -> None:
    repository = str(pin["canonical_repository"])
    commit = str(pin["pinned_commit"])

    if SOURCE_DIR.exists() and not force:
        head = _run(["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR).strip()
        if head == commit:
            return
        raise SetupError(
            f"{SOURCE_DIR} is at {head}, not the pinned {commit}. Re-run with --force."
        )

    if SOURCE_DIR.exists():
        shutil.rmtree(SOURCE_DIR)
    SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)

    # Full clone then detached checkout: a shallow clone cannot be pinned to an
    # arbitrary commit reliably.
    _run(["git", "clone", repository, str(SOURCE_DIR)])
    _run(["git", "checkout", "--detach", commit], cwd=SOURCE_DIR)

    head = _run(["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR).strip()
    if head != commit:
        raise SetupError(f"checkout landed on {head}, expected {commit}")


def create_environment(*, force: bool) -> Path:
    if VENV_DIR.exists() and force:
        shutil.rmtree(VENV_DIR)
    if not VENV_DIR.exists():
        _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    python = VENV_DIR / "bin" / "python"
    if not python.exists():  # pragma: no cover - platform specific
        python = VENV_DIR / "Scripts" / "python.exe"
    if not python.exists():
        raise SetupError(f"virtual environment has no interpreter: {VENV_DIR}")
    return python


def install_server(python: Path, pin: dict) -> None:
    subdirectory = str(pin["mcp_server"]["source_subdirectory"])
    server_source = SOURCE_DIR / subdirectory
    if not (server_source / "pyproject.toml").exists():
        raise SetupError(f"pinned checkout has no server project at {server_source}")
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    _run([str(python), "-m", "pip", "install", "--quiet", str(server_source)])


def stage_addon(pin: dict) -> Path:
    """Copy the pinned add-on into gitignored runtime/ for manual Blender install."""
    source = SOURCE_DIR / str(pin["blender_addon"]["source_subdirectory"])
    if not source.exists():
        raise SetupError(f"pinned checkout has no add-on at {source}")
    if ADDON_STAGING_DIR.exists():
        shutil.rmtree(ADDON_STAGING_DIR)
    shutil.copytree(source, ADDON_STAGING_DIR)
    return ADDON_STAGING_DIR


def verify_entry_point(pin: dict) -> Path:
    entry_point = VENV_DIR / "bin" / str(pin["mcp_server"]["console_entry_point"])
    if not entry_point.exists():  # pragma: no cover - platform specific
        entry_point = VENV_DIR / "Scripts" / (
            str(pin["mcp_server"]["console_entry_point"]) + ".exe"
        )
    if not entry_point.exists():
        raise SetupError(f"installed server has no entry point: {entry_point}")
    return entry_point


def write_descriptor(pin: dict, entry_point: Path) -> dict:
    head = _run(["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR).strip()
    descriptor = {
        "pinned_commit": head,
        "expected_commit": str(pin["pinned_commit"]),
        "server_command": [str(entry_point)],
        "server_transport": "stdio",
        "source_dir": str(SOURCE_DIR),
        "venv_dir": str(VENV_DIR),
        "addon_staging_dir": str(ADDON_STAGING_DIR),
        "declared_server_version": str(pin["mcp_server"]["declared_version"]),
        "declared_addon_version": str(pin["blender_addon"]["declared_version"]),
        "licence": str(pin["licence"]),
    }
    INSTALL_DESCRIPTOR.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_DESCRIPTOR.write_text(
        json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return descriptor


def check_only(pin: dict) -> dict:
    if not INSTALL_DESCRIPTOR.exists():
        raise SetupError(f"not installed: {INSTALL_DESCRIPTOR} is missing")
    descriptor = json.loads(INSTALL_DESCRIPTOR.read_text(encoding="utf-8"))
    if descriptor.get("pinned_commit") != str(pin["pinned_commit"]):
        raise SetupError(
            "installed commit {} does not match pin {}".format(
                descriptor.get("pinned_commit"), pin["pinned_commit"]
            )
        )
    command = descriptor.get("server_command") or []
    if not command or not Path(command[0]).exists():
        raise SetupError(f"installed server command is missing: {command}")
    return descriptor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild from scratch")
    parser.add_argument("--check", action="store_true", help="verify only, no network")
    args = parser.parse_args(argv)

    try:
        pin = load_pin()
        if args.check:
            descriptor = check_only(pin)
        else:
            clone_pinned_source(pin, force=args.force)
            python = create_environment(force=args.force)
            install_server(python, pin)
            stage_addon(pin)
            entry_point = verify_entry_point(pin)
            descriptor = write_descriptor(pin, entry_point)
    except SetupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print("official Blender MCP ready")
    print(f"  commit  : {descriptor['pinned_commit']}")
    print(f"  version : {descriptor['declared_server_version']}")
    print(f"  command : {' '.join(descriptor['server_command'])}")
    print(f"  licence : {descriptor['licence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

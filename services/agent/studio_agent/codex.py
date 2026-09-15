"""A thin client for the OFFICIAL locally installed Codex CLI.

## Why the CLI, and why `codex exec`

Three supported mechanisms exist in Codex 0.154: the experimental `app-server`, the
experimental `exec-server`, and `codex exec`. `codex exec` was chosen because it is the
only one not marked experimental, and because it already provides everything the platform
needs as first-class flags:

* `--output-schema FILE` — constrains the model's final response to a declared JSON
  Schema, which is exactly our `agent-response.schema.json` contract;
* `-o/--output-last-message FILE` — writes the final message to a file, so we parse a
  file rather than scraping decorative terminal output;
* `-i/--image FILE` — attaches real image bytes, which is what makes multimodal analysis
  genuinely multimodal;
* `--json` — machine-readable event stream, used for diagnostics only.

## What this module refuses to do

Authentication belongs to Codex. This module never implements OAuth, never scrapes
chatgpt.com, never calls undocumented endpoints, and never reads or parses Codex
credential files. It asks `codex login status` and believes the answer. There is no API
key anywhere in this product.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

_log = logging.getLogger(__name__)

#: The model this MVP is built around.
ASTRA_MODEL: Final = "gpt-6-astra"

#: The first Codex CLI release verified on this machine to carry the Astra model slug.
MIN_CODEX_VERSION: Final = (0, 154, 0)

DEFAULT_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_STATUS_TIMEOUT_SECONDS: Final = 20.0

# --- connection states, mirrored by the browser ---------------------------
NOT_INSTALLED: Final = "not_installed"
UPDATE_REQUIRED: Final = "update_required"
LOGIN_REQUIRED: Final = "login_required"
CONNECTING: Final = "connecting"
CONNECTED: Final = "connected"
ASTRA_UNAVAILABLE: Final = "astra_unavailable"
USAGE_UNAVAILABLE: Final = "usage_unavailable"

STATES: Final = (
    NOT_INSTALLED,
    UPDATE_REQUIRED,
    LOGIN_REQUIRED,
    CONNECTING,
    CONNECTED,
    ASTRA_UNAVAILABLE,
    USAGE_UNAVAILABLE,
)

#: What the user is told, per state. Deliberately actionable.
STATE_MESSAGES: Final[Mapping[str, str]] = {
    NOT_INSTALLED: "Codex is not installed. Install it with: npm install -g @openai/codex",
    UPDATE_REQUIRED: (
        "The installed Codex is too old to use Astra. Update it with: codex update"
    ),
    LOGIN_REQUIRED: "Sign in to ChatGPT to connect Astra. Run: codex login",
    CONNECTING: "Connecting to Astra…",
    CONNECTED: "Connected via ChatGPT",
    ASTRA_UNAVAILABLE: "Astra is not available to this Codex installation.",
    USAGE_UNAVAILABLE: "Your ChatGPT plan reported no available Astra usage.",
}

#: The action the UI should surface, when there is one the user can take.
STATE_ACTIONS: Final[Mapping[str, str]] = {
    NOT_INSTALLED: "npm install -g @openai/codex",
    UPDATE_REQUIRED: "codex update",
    LOGIN_REQUIRED: "codex login",
}


class CodexError(RuntimeError):
    """Raised when Codex cannot be used for a reason the caller should report."""


@dataclass(frozen=True)
class CodexStatus:
    """The connection state shown as "Astra via Codex" in the toolbar."""

    state: str
    detail: str = ""
    codex_version: Optional[str] = None
    model: Optional[str] = None
    action: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    @property
    def message(self) -> str:
        return self.detail or STATE_MESSAGES.get(self.state, "")

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": "Astra via Codex",
            "message": self.message,
            "codex_version": self.codex_version,
            "model": self.model,
            "action": self.action or STATE_ACTIONS.get(self.state),
            "connected": self.connected,
        }


def _parse_version(text: str) -> Optional[tuple[int, ...]]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


@dataclass
class CodexClient:
    """Runs the local Codex CLI. Owns no credentials of its own."""

    executable: Optional[str] = None
    model: str = ASTRA_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    status_timeout_seconds: float = DEFAULT_STATUS_TIMEOUT_SECONDS
    #: Overridable purely so tests can drive a fake CLI.
    runner: Optional[Any] = None
    _cached_version: Optional[str] = field(default=None, init=False, repr=False)

    # -- discovery ---------------------------------------------------------
    def resolve_executable(self) -> Optional[str]:
        if self.executable:
            return self.executable if Path(self.executable).exists() else None
        return shutil.which("codex")

    def _run(
        self,
        arguments: Sequence[str],
        *,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess[str]:
        executable = self.resolve_executable()
        if executable is None:
            raise CodexError(STATE_MESSAGES[NOT_INSTALLED])
        command = [executable, *arguments]
        if self.runner is not None:
            return self.runner(command, stdin=stdin, timeout=timeout, cwd=cwd)
        # A deliberately minimal environment: no API key is passed, and none is read.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID"}
        }
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout_seconds,
            cwd=str(cwd) if cwd else None,
            env=environment,
            check=False,
        )

    # -- status ------------------------------------------------------------
    def version(self) -> Optional[str]:
        if self._cached_version is not None:
            return self._cached_version
        try:
            completed = self._run(["--version"], timeout=self.status_timeout_seconds)
        except (CodexError, OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        self._cached_version = (completed.stdout or "").strip()
        return self._cached_version

    def status(self) -> CodexStatus:
        """Determine the connection state using only supported Codex commands."""
        if self.resolve_executable() is None:
            return CodexStatus(state=NOT_INSTALLED, action=STATE_ACTIONS[NOT_INSTALLED])

        version_text = self.version()
        if version_text is None:
            return CodexStatus(
                state=NOT_INSTALLED,
                detail="Codex is installed but did not report a version.",
            )

        parsed = _parse_version(version_text)
        if parsed is not None and parsed < MIN_CODEX_VERSION:
            wanted = ".".join(str(part) for part in MIN_CODEX_VERSION)
            return CodexStatus(
                state=UPDATE_REQUIRED,
                detail=(
                    f"Codex {version_text} cannot use Astra; "
                    f"{wanted} or newer is required. Update with: codex update"
                ),
                codex_version=version_text,
                action=STATE_ACTIONS[UPDATE_REQUIRED],
            )

        try:
            completed = self._run(["login", "status"], timeout=self.status_timeout_seconds)
        except (CodexError, OSError, subprocess.SubprocessError) as error:
            return CodexStatus(
                state=LOGIN_REQUIRED,
                detail=f"Could not determine Codex login status: {error}",
                codex_version=version_text,
                action=STATE_ACTIONS[LOGIN_REQUIRED],
            )

        output = f"{completed.stdout or ''} {completed.stderr or ''}".strip().lower()
        if completed.returncode != 0 or "not logged in" in output:
            return CodexStatus(
                state=LOGIN_REQUIRED,
                codex_version=version_text,
                action=STATE_ACTIONS[LOGIN_REQUIRED],
            )

        # Logged in with a new enough Codex. An API-key login is deliberately rejected:
        # this product uses the ChatGPT allowance, and silently accepting a key would
        # contradict that.
        if "api key" in output:
            return CodexStatus(
                state=LOGIN_REQUIRED,
                detail=(
                    "Codex is authenticated with an API key. This studio uses your "
                    "ChatGPT sign-in instead. Run: codex login"
                ),
                codex_version=version_text,
                action=STATE_ACTIONS[LOGIN_REQUIRED],
            )

        return CodexStatus(
            state=CONNECTED,
            codex_version=version_text,
            model=self.model,
        )

    # -- structured completion --------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        images: Iterable[Path] = (),
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Run one turn and return the model's schema-constrained JSON response.

        Raises :class:`CodexError` when Codex fails, times out, or returns something that
        is not the declared shape.
        """
        with tempfile.TemporaryDirectory(prefix="studio_codex_") as workspace_name:
            workspace = Path(workspace_name)
            schema_path = workspace / "response-schema.json"
            schema_path.write_text(
                json.dumps(strict_schema(schema), indent=2), encoding="utf-8"
            )
            output_path = workspace / "last-message.json"

            arguments = [
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--color",
                "never",
                "--cd",
                str(workspace),
            ]
            for image in images:
                arguments.extend(["--image", str(image)])

            try:
                completed = self._run(
                    arguments,
                    stdin=prompt,
                    timeout=timeout_seconds or self.timeout_seconds,
                    cwd=workspace,
                )
            except subprocess.TimeoutExpired as error:
                raise CodexError(
                    f"Astra did not respond within {int(timeout_seconds or self.timeout_seconds)}s."
                ) from error
            except OSError as error:
                raise CodexError(f"Could not run Codex: {error}") from error

            if completed.returncode != 0:
                raise CodexError(_failure_detail(completed))

            if not output_path.exists():
                raise CodexError(
                    "Codex finished without writing a response. " + _failure_detail(completed)
                )
            body = output_path.read_text(encoding="utf-8").strip()

        if not body:
            raise CodexError("Astra returned an empty response.")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            raise CodexError(f"Astra's response was not valid JSON: {error.msg}") from error
        if not isinstance(parsed, dict):
            raise CodexError("Astra's response was not a JSON object.")
        return parsed


def strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Strip authoring metadata a structured-output schema will not accept.

    Our canonical schemas carry ``$schema``, ``$id`` and ``title`` for the repository's
    own tooling. Those are not part of the constrained-generation subset, so they are
    removed here rather than being omitted from the canonical file.
    """
    stripped = {
        key: value for key, value in schema.items() if key not in {"$schema", "$id", "title"}
    }
    return stripped


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Summarise a Codex failure without treating stderr as model output."""
    stderr = (completed.stderr or "").strip()
    lowered = stderr.lower()
    if "not logged in" in lowered or "unauthorized" in lowered or "401" in lowered:
        return STATE_MESSAGES[LOGIN_REQUIRED]
    if "usage" in lowered and "limit" in lowered:
        return STATE_MESSAGES[USAGE_UNAVAILABLE]
    if "model" in lowered and ("not found" in lowered or "unavailable" in lowered):
        return STATE_MESSAGES[ASTRA_UNAVAILABLE]
    for line in reversed(stderr.splitlines()):
        if line.strip():
            return f"Codex reported: {line.strip()[:300]}"
    return f"Codex exited with status {completed.returncode}."


def parse_event_stream(stdout: str) -> list[dict[str, Any]]:
    """Parse the ``--json`` event stream. Diagnostics only, never model output."""
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events

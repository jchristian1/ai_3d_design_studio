"""A synchronous client for the OFFICIAL Blender Lab MCP server.

The worker is synchronous; the MCP SDK is asyncio. Rather than colour the whole
worker async, this module owns one background event loop in a daemon thread, keeps
one long-lived stdio session to the pinned server, and exposes blocking calls.

The server is launched from the install descriptor written by
``scripts/setup_official_blender_mcp.py``. It is never imported: this is a
separate-process protocol integration, which is also what keeps the official
project's GPL-3.0-or-later licence at arm's length.

Nothing here is exposed to a network. The MCP server is a child process on stdio,
and the only socket the official stack can open is the add-on's loopback listener,
which this transport does not use at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DESCRIPTOR = REPO_ROOT / "runtime" / "external" / "official_blender_mcp_install.json"

#: Environment variable overrides, so a different install can be pointed at without
#: touching code.
DESCRIPTOR_ENV = "STUDIO_OFFICIAL_MCP_DESCRIPTOR"
BLENDER_PATH_ENV = "BLENDER_EXECUTABLE"

#: A single tool call gets a generous ceiling: the official CLI transport starts a
#: fresh headless Blender per call, and the upstream helper itself times out at 120s.
DEFAULT_CALL_TIMEOUT_SECONDS = 180.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0


class McpSessionError(RuntimeError):
    """Raised when the official MCP server cannot be reached or used."""


@dataclass(frozen=True)
class McpInstall:
    """The resolved, pinned official MCP installation."""

    server_command: tuple[str, ...]
    pinned_commit: str
    server_version: str
    addon_version: str
    licence: str

    @classmethod
    def load(cls, descriptor_path: Optional[Path] = None) -> "McpInstall":
        path = descriptor_path
        if path is None:
            override = os.environ.get(DESCRIPTOR_ENV)
            path = Path(override) if override else DEFAULT_DESCRIPTOR
        if not path.exists():
            raise McpSessionError(
                "the official Blender MCP is not installed: "
                f"{path} is missing. Run: python scripts/setup_official_blender_mcp.py"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        command = tuple(str(part) for part in raw.get("server_command") or ())
        if not command or not Path(command[0]).exists():
            raise McpSessionError(f"installed MCP server command is missing: {command}")
        return cls(
            server_command=command,
            pinned_commit=str(raw.get("pinned_commit", "")),
            server_version=str(raw.get("declared_server_version", "")),
            addon_version=str(raw.get("declared_addon_version", "")),
            licence=str(raw.get("licence", "")),
        )


def _blender_executable() -> Optional[str]:
    """Locate Blender, reusing the existing worker-side discovery."""
    override = os.environ.get(BLENDER_PATH_ENV)
    if override:
        return override
    try:
        from blender_mcp.blender_runtime import find_blender_executable

        return find_blender_executable()
    except Exception:  # pragma: no cover - discovery is best effort
        return None


class OfficialMcpSession:
    """One long-lived MCP stdio session, usable from synchronous code."""

    def __init__(
        self,
        install: Optional[McpInstall] = None,
        *,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._install = install
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._exit_stack: Any = None
        self._tools: tuple[str, ...] = ()
        self._server_info: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def install(self) -> McpInstall:
        if self._install is None:
            self._install = McpInstall.load()
        return self._install

    @property
    def started(self) -> bool:
        return self._started

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tools

    @property
    def server_info(self) -> Mapping[str, Any]:
        return dict(self._server_info)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            install = self.install
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="official-mcp-loop",
                daemon=True,
            )
            thread.start()
            self._loop = loop
            self._thread = thread
            try:
                self._submit(self._open(install), timeout=self._startup_timeout)
            except Exception as error:
                self._teardown_loop()
                raise McpSessionError(f"could not start the official MCP server: {error}") from error
            self._started = True

    async def _open(self, install: McpInstall) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        blender = _blender_executable()
        if blender:
            # The official *_for_cli tools locate Blender through BLENDER_PATH.
            env["BLENDER_PATH"] = blender
        # Keep the official add-on transport pointed at loopback. This session does
        # not use it, but an inherited value must never widen it.
        env.setdefault("BLENDER_MCP_HOST", "127.0.0.1")

        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=install.server_command[0],
            args=list(install.server_command[1:]),
            env=env,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        initialised = await session.initialize()
        listed = await session.list_tools()

        self._exit_stack = stack
        self._session = session
        self._tools = tuple(sorted(tool.name for tool in listed.tools))
        self._server_info = {
            "name": initialised.serverInfo.name,
            "version": initialised.serverInfo.version,
        }

    def close(self) -> None:
        with self._lock:
            if not self._started:
                self._teardown_loop()
                return
            try:
                self._submit(self._shutdown(), timeout=15.0)
            except Exception as error:  # pragma: no cover - best effort shutdown
                _log.debug("official MCP shutdown raised: %s", error)
            finally:
                self._teardown_loop()
                self._session = None
                self._exit_stack = None
                self._started = False

    async def _shutdown(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()

    def _teardown_loop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop, self._thread = None, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10.0)
        if loop is not None:
            try:
                loop.close()
            except Exception:  # pragma: no cover
                pass

    def _submit(self, coroutine: Any, *, timeout: float) -> Any:
        loop = self._loop
        if loop is None:
            raise McpSessionError("the MCP session is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result(timeout=timeout)

    # -- tool calls --------------------------------------------------------
    def list_tools(self) -> tuple[str, ...]:
        if not self._started:
            self.start()
        return self._tools

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Call one official tool and return its parsed JSON payload.

        Raises :class:`McpSessionError` when the server is unreachable or the tool
        reports an error. The server's own logging goes to stderr and is never
        treated as output.
        """
        if not self._started:
            self.start()
        try:
            raw = self._submit(
                self._call(name, dict(arguments)), timeout=self._call_timeout
            )
        except McpSessionError:
            raise
        except Exception as error:
            raise McpSessionError(f"official MCP call {name} failed: {error}") from error
        return raw

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise McpSessionError("the MCP session is not initialised")
        outcome = await session.call_tool(name, arguments)
        texts: list[str] = []
        for block in outcome.content:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        joined = "\n".join(texts).strip()
        if outcome.isError:
            raise McpSessionError(joined or f"tool {name} reported an error")
        if not joined:
            return {}
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError:
            # The official tools return JSON for structured tools; anything else is
            # surfaced verbatim rather than guessed at.
            return {"text": joined}
        if not isinstance(parsed, dict):
            return {"value": parsed}
        return parsed

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "OfficialMcpSession":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

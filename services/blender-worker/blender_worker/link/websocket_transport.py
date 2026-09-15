"""Outbound WebSocket transport.

Spec 001, Task 8.

One implementation of ``WorkerTransport``. It CONNECTS OUT to the control plane and
never listens, so the workstation needs no inbound public port and Blender, bpy and
the local MCP boundary stay unreachable from the network.

Production readiness
--------------------
The only thing that changes for production is the URL scheme::

    ws://127.0.0.1:8765/ws/workers      local development
    wss://api.example.com/ws/workers    production, TLS terminated by the server

``wss://`` is handled by the library's standard TLS support — no custom
cryptography is implemented here, which is deliberate. ``require_secure`` lets a
deployment refuse plaintext outright.

Dependency
----------
``websockets`` (pinned, ~170 KB, no transitive dependencies) is imported lazily, so
the rest of the worker — and every fast test, which uses ``InMemoryTransport`` —
runs without it installed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from studio_contracts import worker_protocol as protocol

from .transport import TransportClosed, TransportError

DEFAULT_OPEN_TIMEOUT = 10.0
DEFAULT_RECEIVE_TIMEOUT = 1.0


class WebSocketWorkerTransport:
    """A synchronous outbound WebSocket link."""

    def __init__(
        self,
        url: str,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT,
        require_secure: bool = False,
    ) -> None:
        if require_secure and not url.startswith("wss://"):
            raise TransportError(
                f"refusing an insecure control-plane URL {url!r}; wss:// is required"
            )
        self.url = url
        self.open_timeout = open_timeout
        self.receive_timeout = receive_timeout
        self._connection: Any = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        try:
            from websockets.sync.client import connect as ws_connect
        except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
            raise TransportError(
                "the 'websockets' package is required for the WebSocket transport"
            ) from exc

        try:
            # Outbound only. There is no bind/listen anywhere in this class.
            self._connection = ws_connect(self.url, open_timeout=self.open_timeout)
        except Exception as exc:
            self._connection = None
            raise TransportError(f"could not connect to {self.url}: {exc}") from exc

    def send(self, message: dict[str, Any]) -> None:
        if self._connection is None:
            raise TransportClosed("transport is not connected")
        try:
            # encode() validates against the canonical schema, so an invalid
            # message never reaches the wire.
            self._connection.send(protocol.encode(message))
        except protocol.ProtocolError:
            raise
        except Exception as exc:
            self._connection = None
            raise TransportError(f"send failed: {exc}") from exc

    def receive(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        if self._connection is None:
            raise TransportClosed("transport is not connected")
        wait = self.receive_timeout if timeout is None else timeout
        try:
            raw = self._connection.recv(timeout=wait)
        except TimeoutError:
            return None
        except Exception as exc:
            self._connection = None
            name = type(exc).__name__
            if "Closed" in name or "Connection" in name:
                raise TransportClosed(f"connection closed: {exc}") from exc
            raise TransportError(f"receive failed: {exc}") from exc

        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TransportError(f"peer sent invalid JSON: {exc}") from exc

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # pragma: no cover - closing must never raise
            pass

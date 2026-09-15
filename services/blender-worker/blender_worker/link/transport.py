"""The worker transport boundary.

Spec 001, Task 8.

    WorkerLinkClient
        v
    WorkerTransport            (this module — the seam)
        ├── InMemoryTransport   deterministic, used by every fast test
        └── WebSocketWorkerTransport   real outbound WSS/WS connection

The Task 6 ``WorkerExecutor`` sits *below* the client and never sees a transport
at all, so nothing about socket handling reaches Blender execution. Swapping
WebSockets for another transport means writing another implementation of this
Protocol.

Direction of connection
-----------------------
Every implementation must CONNECT OUTBOUND. There is no listen/bind here and no
implementation may add one: the workstation must not require an inbound public
port, and Blender, bpy and the local MCP boundary must never be reachable from
the network (see .kiro/steering/security.md).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional, Protocol


class TransportError(RuntimeError):
    """The transport failed; the caller should treat the link as lost."""


class TransportClosed(TransportError):
    """The peer closed the connection."""


class WorkerTransport(Protocol):
    """A message-oriented, outbound-only link to the control plane."""

    @property
    def connected(self) -> bool: ...

    def connect(self) -> None:
        """Open the link. Outbound only. Raises TransportError on failure."""
        ...

    def send(self, message: dict[str, Any]) -> None:
        """Send one protocol message. Raises TransportError if the link is lost."""
        ...

    def receive(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Receive one protocol message, or None if nothing arrived in time.

        Raises TransportClosed when the peer has gone away.
        """
        ...

    def close(self) -> None:
        """Close the link. Safe to call when already closed."""
        ...


class InMemoryTransport:
    """Two in-process queues standing in for a socket.

    Used by every fast test so the whole connection state machine — auth,
    registration, heartbeats, job offers, reconnect — is exercised without
    networking or timing flakiness.

    ``drop()`` simulates the link dying mid-conversation, which is how the
    result-delivery-failure and reconnect paths are tested.
    """

    def __init__(self, server: Optional["InMemoryServerEndpoint"] = None) -> None:
        self._server = server or InMemoryServerEndpoint()
        self._connected = False
        #: Number of times connect() was called, so reconnect can be asserted.
        self.connect_count = 0
        self.send_failures = 0
        #: When set, the next send raises. Simulates a socket dying on write.
        self.fail_next_send = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server(self) -> "InMemoryServerEndpoint":
        return self._server

    def connect(self) -> None:
        self.connect_count += 1
        self._server.reset_stream()
        self._connected = True

    def send(self, message: dict[str, Any]) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            self.send_failures += 1
            self._connected = False
            raise TransportError("simulated send failure")
        if not self._connected:
            raise TransportClosed("transport is not connected")
        self._server.to_server.append(dict(message))

    def receive(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        if not self._connected:
            raise TransportClosed("transport is not connected")
        if not self._server.to_worker:
            return None
        return self._server.to_worker.popleft()

    def close(self) -> None:
        self._connected = False

    # -- test helpers -------------------------------------------------------

    def drop(self) -> None:
        """Simulate the link disappearing without a clean close."""
        self._connected = False


class InMemoryServerEndpoint:
    """The control-plane side of an InMemoryTransport."""

    def __init__(self) -> None:
        self.to_worker: deque[dict[str, Any]] = deque()
        self.to_server: deque[dict[str, Any]] = deque()
        #: Every message the worker ever sent, including across reconnects.
        self.received: list[dict[str, Any]] = []

    def reset_stream(self) -> None:
        """A fresh connection starts with empty queues, like a new socket."""
        self.to_worker.clear()
        self.to_server.clear()

    def push_to_worker(self, message: dict[str, Any]) -> None:
        self.to_worker.append(dict(message))

    def drain_from_worker(self) -> list[dict[str, Any]]:
        drained = list(self.to_server)
        self.to_server.clear()
        self.received.extend(drained)
        return drained

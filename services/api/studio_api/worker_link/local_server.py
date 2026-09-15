"""Local development control-plane WebSocket server.

Spec 001, Task 8.

A minimal server so the outbound-connection architecture can be proven end to end
on one Ubuntu machine:

    worker  --outbound-->  ws://127.0.0.1:8765/ws/workers  -->  WorkerConnectionManager

It binds to 127.0.0.1 by default, so nothing is reachable from the network. This is
the CONTROL PLANE listening, which is correct — the workstation still never listens,
and Blender/bpy/MCP remain unexposed.

Boundary: the production endpoint is a FastAPI/Starlette route added in the API
task. All decision logic already lives in ``WorkerConnectionManager``, so that
binding is thin and `ws://127.0.0.1:8765/ws/workers` becomes
`wss://api.example.com/ws/workers` without touching WorkerExecutor.

SECURITY NOTE: this development server authenticates workers with a pre-shared
token at the application layer and has no TLS of its own. It is intended for
loopback development only. A production deployment must terminate TLS (wss://) and
should replace the shared token with mTLS or signed short-lived credentials.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from studio_contracts import worker_protocol as protocol

from .manager import WorkerConnectionManager

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WORKER_PATH = "/ws/workers"


@dataclass
class LocalControlPlaneServer:
    """A loopback WebSocket server wrapping a WorkerConnectionManager."""

    manager: WorkerConnectionManager
    host: str = DEFAULT_HOST
    port: int = 0  # 0 lets the OS choose a free port, which tests rely on
    _server: Any = None
    _thread: Optional[threading.Thread] = None
    #: Offers for workers that are not connected yet, by worker_id. Flushed at
    #: registration.
    pending_offers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Live connections by worker_id, so an offer can be PUSHED to an idle worker.
    _connections: dict[str, Any] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock)
    #: Every raw message received, redacted so no token is ever retained.
    transcript: list[dict[str, Any]] = field(default_factory=list)

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.bound_port}{WORKER_PATH}"

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.socket.getsockname()[1]

    def start(self) -> "LocalControlPlaneServer":
        from websockets.sync.server import serve

        self._server = serve(self._handle_connection, self.host, self.port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="control-plane-ws"
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "LocalControlPlaneServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- offers ------------------------------------------------------------

    def queue_offer(self, worker_id: str, job: dict[str, Any]) -> dict[str, Any]:
        """Offer a canonical Job, pushing it immediately if the worker is live.

        A control plane must be able to hand work to an IDLE worker without
        waiting for the worker to speak first. Sending only in reaction to inbound
        traffic deadlocks: the server waits to read while the worker waits to be
        offered a job.
        """
        offer = self.manager.build_job_offer(job)
        with self._lock:
            connection = self._connections.get(worker_id)
            if connection is None:
                self.pending_offers.setdefault(worker_id, []).append(offer)
                return offer
        self._send(connection, offer)
        return offer

    def _send(self, connection: Any, message: dict[str, Any]) -> bool:
        try:
            connection.send(protocol.encode(message))
            return True
        except Exception as exc:  # pragma: no cover - depends on socket teardown
            logger.debug("could not send to worker: %s", exc)
            return False

    # -- connection handling ----------------------------------------------

    def _handle_connection(self, connection: Any) -> None:
        """Serve one outbound worker connection.

        This loop only handles INBOUND messages. Offers are pushed from
        ``queue_offer`` on the caller's thread, so an idle worker is not starved.
        """
        worker_id: Optional[str] = None
        try:
            for raw in connection:
                reply = self._process(raw)
                if reply is None:
                    continue

                self._send(connection, reply)

                if reply["type"] == protocol.WORKER_REGISTERED:
                    worker_id = reply["worker_id"]
                    self._register_connection(worker_id, connection)
                elif reply["type"] == protocol.WORKER_REJECTED:
                    # An unauthenticated worker gets no further conversation.
                    break
        except Exception as exc:  # pragma: no cover - transport teardown varies
            logger.debug("worker connection ended: %s", exc)
        finally:
            if worker_id is not None:
                with self._lock:
                    if self._connections.get(worker_id) is connection:
                        del self._connections[worker_id]
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass

    def _register_connection(self, worker_id: str, connection: Any) -> None:
        """Record the live connection and flush anything queued before it existed."""
        with self._lock:
            self._connections[worker_id] = connection
            queued = self.pending_offers.pop(worker_id, [])
        for offer in queued:
            self._send(connection, offer)

    def _process(self, raw: Any) -> Optional[dict[str, Any]]:
        try:
            parsed = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            # Redacted before it is ever retained or logged.
            self.transcript.append(protocol.redact(parsed))
        except Exception:
            self.transcript.append({"type": "unparseable"})
        return self.manager.handle_message(raw)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "WORKER_PATH",
    "LocalControlPlaneServer",
]

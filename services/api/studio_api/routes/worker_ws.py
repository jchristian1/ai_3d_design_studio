"""WS /ws/workers — the control plane's worker endpoint.

Spec 001, Task 9. This is the FastAPI/Starlette binding for Task 8's link.

    Blender workstation  --outbound WS-->  /ws/workers  ->  WorkerGateway
                                                                v
                                                        WorkerConnectionManager

WHAT IS EXPOSED, AND WHAT IS NOT
--------------------------------
The control plane listens; the workstation still connects OUT. That is not a
weakening of the security posture — it is the posture: the workstation needs no
inbound port, so Blender, ``bpy``, and the local MCP boundary remain unreachable
from the network.

What this endpoint accepts is only the constrained worker protocol. Its canonical
schema has a closed message enum and ``additionalProperties: false`` at every
level, so it cannot express a shell command, a Python expression, a script, or a
filesystem path. Exposing this route does not expose Blender or MCP.

Authentication is the protocol's, not the transport's: ``worker_hello`` carries the
pre-shared token, the manager compares it in constant time and discards it, and an
unauthenticated worker is rejected and disconnected without ever being registered.

TLS: production terminates ``wss://`` in front of this route (or at the ASGI
server). This module adds no cryptography of its own, which is deliberate.

CONCURRENCY DESIGN
------------------
Two properties drive the structure:

1. **One writer.** Starlette does not serialize concurrent sends on one socket, so
   *every* outbound message — protocol replies and pushed job offers alike — goes
   through a single queue drained by one sender task. Ordering is preserved, so a
   ``worker_registered`` always precedes offers flushed at registration.

2. **Sending must not depend on receiving.** The sender task is independent of the
   receive loop. This is the Task 8 anti-deadlock invariant: an idle worker that
   sends nothing must still be offered queued work. A regression test pins it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from studio_contracts import worker_protocol as protocol

from ..worker_link.gateway import WorkerGateway

logger = logging.getLogger(__name__)

router = APIRouter()

WORKER_PATH = "/ws/workers"

#: How long to keep flushing queued outbound messages after the conversation ends,
#: so a rejection reply is not lost to an immediate close.
DRAIN_TIMEOUT_SECONDS = 2.0


@router.websocket(WORKER_PATH)
async def worker_link(websocket: WebSocket) -> None:
    """Serve one outbound worker connection."""
    gateway: WorkerGateway = websocket.app.state.dependencies.gateway

    await websocket.accept()

    outbound: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    loop_thread_id = threading.get_ident()

    def sink(message: dict) -> bool:
        """Hand one message to the single writer.

        Thread-aware: an offer enqueued from an async route runs on this loop, but
        a future synchronous caller would be on a worker thread, where
        ``put_nowait`` is not safe. Handling both here prevents a subtle,
        intermittent failure later.
        """
        try:
            if threading.get_ident() == loop_thread_id:
                outbound.put_nowait(message)
            else:
                loop.call_soon_threadsafe(outbound.put_nowait, message)
            return True
        except Exception:  # pragma: no cover - loop shutdown races
            return False

    sender = asyncio.create_task(_sender_loop(websocket, outbound))
    worker_id: Optional[str] = None

    try:
        while True:
            raw = await _receive(websocket)
            if raw is None:
                break

            reply = gateway.handle_inbound(raw)
            if reply is None:
                continue

            # Replies travel through the same queue as offers, so there is exactly
            # one writer and message order is guaranteed.
            sink(reply)

            if reply["type"] == protocol.WORKER_REGISTERED:
                worker_id = reply["worker_id"]
                # Registers the live connection AND flushes offers queued while
                # this worker was offline. Enqueued after the registration reply.
                gateway.attach(worker_id, sink)
            elif reply["type"] == protocol.WORKER_REJECTED:
                # An unauthenticated or unsupported worker gets no further
                # conversation.
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport teardown varies
        logger.debug("worker websocket ended unexpectedly", exc_info=True)
    finally:
        # Flush anything still queued (notably a rejection) before closing.
        await _drain(outbound, DRAIN_TIMEOUT_SECONDS)
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, Exception):  # pragma: no cover
            pass
        if worker_id is not None:
            # Deregisters the worker, so it can no longer be selected for work.
            gateway.detach(worker_id, sink)
        try:
            await websocket.close()
        except Exception:  # pragma: no cover - already closed
            pass


async def _receive(websocket: WebSocket) -> Optional[Any]:
    """Receive one frame as text or bytes, or None when the peer went away."""
    message = await websocket.receive()
    kind = message.get("type")
    if kind == "websocket.disconnect":
        return None
    if message.get("text") is not None:
        return message["text"]
    if message.get("bytes") is not None:
        return message["bytes"]
    return None


async def _sender_loop(websocket: WebSocket, outbound: asyncio.Queue) -> None:
    """The single writer for this connection."""
    while True:
        message = await outbound.get()
        try:
            # encode() validates against the canonical schema, so an invalid
            # message never reaches the wire.
            await websocket.send_text(protocol.encode(message))
        except Exception:  # pragma: no cover - peer closed mid-send
            logger.debug("could not send to worker", exc_info=True)
            return
        finally:
            outbound.task_done()


async def _drain(outbound: asyncio.Queue, timeout_seconds: float) -> None:
    """Wait briefly for queued outbound messages to be written."""
    try:
        await asyncio.wait_for(outbound.join(), timeout=timeout_seconds)
    except (asyncio.TimeoutError, Exception):  # pragma: no cover
        pass


__all__ = ["WORKER_PATH", "router"]

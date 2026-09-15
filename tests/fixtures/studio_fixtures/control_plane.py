"""Running the control plane in-process for tests.

Spec 001, Task 9. TEST TOOLING — not part of any shipped service.

Lives in ``studio_fixtures`` because both ``tests/integration`` and ``tests/e2e``
need it, and that package is the repository's designated test-tooling path (it is
deliberately not installed as a runtime package).

Starts the real FastAPI application under a real uvicorn server on a real loopback
port, in a background thread. That matters: the Task 9 WebSocket binding is
asynchronous and its anti-deadlock behaviour depends on a live event loop with a
sender task independent of the receive loop, which an in-process test client cannot
exercise.

    pytest (main thread)                  uvicorn thread
      |                                      |
      |-- HTTP POST /api/chat -------------->| FastAPI route
      |                                      |    v
      |                                   WorkerGateway pushes offer
      |                                      |
      |<-- WorkerLinkClient over real WS ----|  /ws/workers
      |

Binds to 127.0.0.1 on an OS-assigned free port, so tests never collide and nothing
is reachable from the network.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from studio_api.app import create_app
from studio_api.dependencies import AppDependencies, build_dependencies
from studio_api.settings import Settings


def free_port() -> int:
    """Reserve a free loopback port and release it for uvicorn to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class ControlPlaneServer:
    """The control plane running under uvicorn in a background thread."""

    settings: Settings
    dependencies: AppDependencies
    host: str = "127.0.0.1"
    port: int = 0
    _server: Any = None
    _thread: Optional[threading.Thread] = None
    _app: Any = field(default=None, repr=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        settings: Settings,
        dependencies: Optional[AppDependencies] = None,
        **dependency_overrides: Any,
    ) -> "ControlPlaneServer":
        resolved = dependencies or build_dependencies(settings, **dependency_overrides)
        return cls(settings=settings, dependencies=resolved, port=free_port())

    # -- urls --------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def worker_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws/workers"

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout_seconds: float = 20.0) -> "ControlPlaneServer":
        import uvicorn

        self._app = create_app(self.settings, self.dependencies)
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            # The worker link is a WebSocket, so WS support must be active.
            # "auto" selects the maintained implementation; naming "websockets"
            # explicitly would pin the deprecated legacy one.
            ws="auto",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="control-plane-uvicorn"
        )
        self._thread.start()
        self._await_ready(timeout_seconds)
        return self

    def _await_ready(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError("the control plane did not start in time")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._server = None

    def __enter__(self) -> "ControlPlaneServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- convenience -------------------------------------------------------

    def client(self):
        """An httpx client bound to this server."""
        import httpx

        return httpx.Client(base_url=self.base_url, timeout=30.0)


__all__ = ["ControlPlaneServer", "free_port"]

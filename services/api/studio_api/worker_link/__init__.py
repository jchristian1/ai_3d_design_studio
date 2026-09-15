"""Control-plane side of the worker link.

    manager.py       WorkerConnectionManager — authentication, liveness, eligibility
                     (Task 8; framework-independent by design)
    gateway.py       WorkerGateway — live connection registry and offer dispatch
                     (Task 9; transport-neutral, used by the FastAPI route)
    local_server.py  LocalControlPlaneServer — loopback development WS server
                     (Task 8; superseded for application use by /ws/workers)
"""

from .gateway import WorkerGateway
from .local_server import LocalControlPlaneServer
from .manager import RegisteredWorker, WorkerConnectionManager

__all__ = [
    "LocalControlPlaneServer",
    "RegisteredWorker",
    "WorkerConnectionManager",
    "WorkerGateway",
]

"""Connection status for the toolbar: Astra, and the design machine.

Deliberately honest. Each state names something the user can act on, and no state ever
claims Astra is connected when a different model would be used instead.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from studio_agent import codex

from .support import get_dependencies

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/astra")
def astra_status(request: Request) -> dict[str, Any]:
    """Report the Astra-via-Codex connection state.

    The provider owns this: a provider that is not Codex-backed simply reports itself as
    connected, because there is nothing to sign in to. Nothing here reads a credential.
    """
    dependencies = get_dependencies(request)
    provider = dependencies.design_provider

    status_method = getattr(provider, "status", None)
    if callable(status_method):
        try:
            status = status_method()
        except Exception as error:  # a status probe must never break the page
            _log.warning("Astra status probe failed: %s", error)
            return {
                "state": codex.LOGIN_REQUIRED,
                "label": "Astra via Codex",
                "message": f"Could not determine the Codex status: {error}",
                "connected": False,
                "provider": getattr(provider, "name", "unknown"),
            }
        payload = status.snapshot()
        payload["provider"] = getattr(provider, "name", "unknown")
        return payload

    # A provider with no notion of sign-in (the offline scripted one, or a Spec 001
    # provider through the adapter). Saying "connected" is accurate: it is ready.
    return {
        "state": codex.CONNECTED,
        "label": getattr(provider, "name", "agent"),
        "message": "Ready",
        "connected": True,
        "model": getattr(provider, "model", None),
        "provider": getattr(provider, "name", "unknown"),
        "action": None,
    }


@router.get("/blender")
def blender_status(request: Request) -> dict[str, Any]:
    """Report whether a Blender-capable worker is connected.

    Derived from the worker link rather than by probing Blender from the control plane:
    the control plane deliberately cannot see Blender, and asking the link is both
    accurate and free.
    """
    dependencies = get_dependencies(request)
    snapshots = dependencies.gateway.worker_snapshots()

    ready = [
        snapshot
        for snapshot in snapshots
        if snapshot.get("connected")
        and (snapshot.get("capabilities") or {}).get("blender_available")
    ]
    capable = ready[0] if ready else None
    supported = list((capable or {}).get("capabilities", {}).get("supported_job_types") or ())

    if capable is not None:
        return {
            "state": "connected",
            "label": "Blender",
            "message": "Connected",
            "connected": True,
            "blender_version": (capable.get("capabilities") or {}).get("blender_version"),
            "supports_modelling": "apply_capabilities" in supported,
            "worker_count": len(ready),
        }

    if snapshots:
        return {
            "state": "no_blender",
            "label": "Blender",
            "message": (
                "A design machine is connected but Blender is not available on it."
            ),
            "connected": False,
            "supports_modelling": False,
            "worker_count": 0,
        }

    return {
        "state": "disconnected",
        "label": "Blender",
        "message": "The design machine is not connected. Start the worker to model.",
        "connected": False,
        "supports_modelling": False,
        "worker_count": 0,
    }

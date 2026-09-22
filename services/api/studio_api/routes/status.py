"""Connection status for the toolbar: Astra, and the design machine.

Deliberately honest. Each state names something the user can act on, and no state ever
claims Astra is connected when a different model would be used instead.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from studio_agent import codex
from studio_agent.codex import CodexError

from .. import errors, worker_reload
from .support import ControlPlaneHTTPError, get_dependencies

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
            # The worker reloads itself when its code changes, so there is no staleness
            # for the control plane to detect — and it could not detect it anyway
            # without reading the worker's filesystem, which it must not do. What is
            # offered instead is the manual override, available whenever a worker is
            # connected.
            "can_reload": True,
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


@router.get("/gpu")
def gpu_status(request: Request) -> dict[str, Any]:
    """Report whether the connected design machine has a usable GPU.

    Like the Blender status, this is derived from the worker link rather than probed from
    the control plane, which cannot see the workstation. The worker advertises
    ``gpu_available``/``gpu_name`` at registration (from ``STUDIO_WORKER_GPU_NAME`` or an
    ``nvidia-smi`` probe), so this is both accurate and free.
    """
    dependencies = get_dependencies(request)
    snapshots = dependencies.gateway.worker_snapshots()

    connected = [s for s in snapshots if s.get("connected")]
    with_gpu = [
        s for s in connected if (s.get("capabilities") or {}).get("gpu_available")
    ]
    capable = with_gpu[0] if with_gpu else None

    if capable is not None:
        gpu_name = (capable.get("capabilities") or {}).get("gpu_name") or "GPU"
        return {
            "state": "connected",
            "label": "GPU",
            "message": gpu_name,
            "connected": True,
            "gpu_name": gpu_name,
            "worker_count": len(with_gpu),
        }

    if connected:
        return {
            "state": "no_gpu",
            "label": "GPU",
            "message": (
                "A design machine is connected but no GPU was detected on it. "
                "Rendering will fall back to the CPU."
            ),
            "connected": False,
            "gpu_name": None,
            "worker_count": 0,
        }

    return {
        "state": "disconnected",
        "label": "GPU",
        "message": "The design machine is not connected. Start the worker to use the GPU.",
        "connected": False,
        "gpu_name": None,
        "worker_count": 0,
    }



# --- signing in to ChatGPT -------------------------------------------------
#
# These endpoints drive the OFFICIAL `codex login` flow and relay what it prints. The
# platform never implements OAuth, never contacts auth.openai.com itself, never reads a
# credential file, and never asks for a password: Codex owns authentication end to end.
#
# The command is fixed. Nothing from the request body reaches the argument list; the only
# choice is which of the two official modes to use. The control plane binds loopback in
# local development, so this is the user driving their own Codex client from their own
# browser on their own machine.


class LoginRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Use the device-code flow, for when the localhost callback cannot be reached.
    device_auth: bool = False


def _login_capable(provider: Any) -> bool:
    return callable(getattr(provider, "begin_login", None))


def _no_login_support(provider: Any) -> dict[str, Any]:
    return {
        "supported": False,
        "state": "idle",
        "message": (
            f"The configured provider ({getattr(provider, 'name', 'unknown')}) does not "
            "sign in to anything."
        ),
        "waiting": False,
        "verification_url": None,
        "user_code": None,
    }


@router.post("/astra/login")
def begin_astra_login(body: LoginRequestBody, request: Request) -> dict[str, Any]:
    """Start signing in to ChatGPT, and return what the user has to do next."""
    dependencies = get_dependencies(request)
    provider = dependencies.design_provider
    if not _login_capable(provider):
        return _no_login_support(provider)

    try:
        session = provider.begin_login(device_auth=body.device_auth)
    except CodexError as error:
        failure = errors.failure(errors.PROVIDER_UNAVAILABLE, "PROVIDER_UNAVAILABLE", str(error))
        raise ControlPlaneHTTPError(failure.http_status, failure.body()) from error

    return {"supported": True, **session.snapshot(), "status": provider.status().snapshot()}


@router.get("/astra/login")
def astra_login_state(request: Request) -> dict[str, Any]:
    """Poll the in-progress sign-in. The browser calls this while the user authorises."""
    dependencies = get_dependencies(request)
    provider = dependencies.design_provider
    if not _login_capable(provider):
        return _no_login_support(provider)

    session = provider.login_session()
    return {"supported": True, **session.snapshot(), "status": provider.status().snapshot()}


@router.delete("/astra/login")
def cancel_astra_login(request: Request) -> dict[str, Any]:
    """Abandon a sign-in, stopping the local callback server it started."""
    dependencies = get_dependencies(request)
    provider = dependencies.design_provider
    if not _login_capable(provider):
        return _no_login_support(provider)

    provider.cancel_login()
    return {"supported": True, **provider.login_session().snapshot()}


# A comment, not a docstring: this is served to the browser in /openapi.json, and how the
# request reaches the worker is an implementation detail (see worker_reload).
#
# Under /api/status because that is where the staleness is reported and where the browser
# is already looking; the action belongs next to the condition it resolves.
@router.post("/worker/reload")
def reload_worker(request: Request) -> dict[str, Any]:
    """Ask the design machine to reload the studio's current code.

    Safe to call at any time. The machine finishes whatever it is doing first.
    """
    dependencies = get_dependencies(request)
    snapshots = dependencies.gateway.worker_snapshots()
    connected = [snapshot for snapshot in snapshots if snapshot.get("connected")]

    if not connected:
        failure = errors.failure(
            errors.INVALID_REQUEST,
            "VALIDATION_ERROR",
            "The design machine is not connected, so there is nothing to reload.",
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    if not worker_reload.request_reload():
        failure = errors.failure(
            errors.INTERNAL,
            "INTERNAL_ERROR",
            "The reload could not be requested on this machine.",
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    return {
        "requested": True,
        # The worker reloads at its next idle moment, which is within a couple of
        # seconds unless it is mid-job. Saying so sets the right expectation instead of
        # implying the reload has already happened.
        "message": "Reloading the design machine. This takes a few seconds.",
    }

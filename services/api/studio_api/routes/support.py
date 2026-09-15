"""FastAPI plumbing shared by the routes.

Spec 001, Task 9.

Keeps framework glue in one place so route modules stay short and so the
service//domain layer never imports FastAPI. Dependencies are read from
``app.state``, which is set once by ``create_app`` — there is no module-level
singleton to leak between tests.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Header, Request

from ..chat_service import ChatService
from ..dependencies import AppDependencies
from ..errors import UNAUTHENTICATED, error_body
from ..identity import IdentityError, TrustedIdentity
from ..settings import Settings


class ControlPlaneHTTPError(Exception):
    """An HTTP failure with an already-rendered structured body.

    Used instead of ``HTTPException`` so every error response has the SAME shape
    (``{"error": {"code", "message"}}``) rather than FastAPI's ``{"detail": ...}``.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


def get_dependencies(request: Request) -> AppDependencies:
    return request.app.state.dependencies


def get_settings(request: Request) -> Settings:
    return get_dependencies(request).settings


def get_chat_service(request: Request) -> ChatService:
    return get_dependencies(request).chat_service


def get_identity(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> TrustedIdentity:
    """Establish the caller's trusted identity.

    This is the seam real authentication replaces. The ``Authorization`` header is
    accepted and forwarded to the resolver, but the Spec 001 development resolver
    ignores it entirely — it is declared here so the signature does not have to
    change when a real resolver starts reading it.

    A resolver that refuses (for example the development resolver outside the
    local environment) produces 401 with a structured body, never a traceback.
    """
    resolver = get_dependencies(request).identity_resolver
    try:
        return resolver.resolve(authorization)
    except IdentityError as exc:
        raise ControlPlaneHTTPError(
            status_code=401,
            body=error_body("VALIDATION_ERROR", str(exc)),
        ) from exc


__all__ = [
    "UNAUTHENTICATED",
    "ControlPlaneHTTPError",
    "get_chat_service",
    "get_dependencies",
    "get_identity",
    "get_settings",
]

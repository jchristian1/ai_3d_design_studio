"""The FastAPI application factory.

Spec 001, Task 9.

    create_app(settings=None, dependencies=None) -> FastAPI

A factory, not a module-level ``app``, for three reasons: configuration is
validated at construction so a misconfigured process fails at startup; tests build
an application with fake collaborators without monkeypatching; and two applications
can exist in one process without sharing state. The only mutable state is
``app.state.dependencies``, set once here.

Local run::

    source .venv/bin/activate
    uvicorn studio_api.app:create_app --factory --host 127.0.0.1 --port 8000

Routes
------
| Method | Path                                        | Purpose                     |
|--------|---------------------------------------------|-----------------------------|
| GET    | ``/health``                                 | control-plane health         |
| GET    | ``/api/workers``                            | connected workers (safe)     |
| POST   | ``/api/chat``                               | submit a design change       |
| POST   | ``/api/projects/{id}/chat``                 | project-scoped form          |
| GET    | ``/api/projects/{id}/jobs/{job_id}``        | status/result of a change    |
| WS     | ``/ws/workers``                             | worker link (Task 8)         |

Job retrieval is project-scoped by path and has no unscoped variant: a ``job_id``
is an identifier, not a capability.

``/api`` is the versionable prefix: a future breaking change becomes ``/api/v2``
without disturbing ``/health`` (an infrastructure probe) or ``/ws/workers`` (whose
compatibility is governed by the protocol's own ``protocol_version``).

Error discipline
----------------
Three handlers guarantee that every failure leaves as the same structured body
``{"error": {"code", "message"}}``:

  - ``ControlPlaneHTTPError`` — deliberate failures from routes and services;
  - ``RequestValidationError`` — FastAPI's 422, reshaped from ``{"detail": ...}``
    and reduced to field locations without echoing submitted values;
  - ``Exception`` — anything unexpected becomes a fixed ``INTERNAL_ERROR`` body
    while the traceback goes to the server log. No stack trace, exception class,
    filesystem path, or configuration value reaches a client.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .dependencies import AppDependencies, build_dependencies
from .errors import error_body, internal_error_body
from .routes import chat, health, jobs, worker_ws, workers
from .routes.support import ControlPlaneHTTPError
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)

API_TITLE = "AI 3D Design Studio — Control Plane"
API_VERSION = "0.1.0"

API_DESCRIPTION = """
The control plane for Spec 001. Accepts a natural-language design change, resolves
it to a canonical Job through the AgentProvider boundary, and dispatches it to an
outbound-connected Blender worker.

Blender is never exposed by this service. Workers connect out to `/ws/workers` and
speak only the constrained worker protocol.
"""


def create_app(
    settings: Optional[Settings] = None,
    dependencies: Optional[AppDependencies] = None,
) -> FastAPI:
    """Build the control-plane application.

    ``settings`` defaults to the environment. ``dependencies`` defaults to the real
    wiring; tests pass their own container.
    """
    resolved_settings = (settings or load_settings()).validate()
    resolved_dependencies = dependencies or build_dependencies(resolved_settings)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
    )
    # The single piece of application state, set once at construction.
    app.state.dependencies = resolved_dependencies

    _install_cors(app, resolved_dependencies.settings)
    _install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(jobs.router)
    app.include_router(workers.router)
    app.include_router(worker_ws.router)

    return app


def _install_cors(app: FastAPI, settings: Settings) -> None:
    """Install CORS only when origins are explicitly configured.

    No origins means the middleware is NOT installed, which is the correct posture
    while no browser client exists: there is nothing to relax yet. When the web app
    arrives, its origin is listed explicitly through configuration — never ``*``,
    which ``Settings.validate`` refuses outside local development.
    """
    if not settings.allowed_origins:
        return

    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        # Narrow by default: only what the chat/status flow actually needs.
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ControlPlaneHTTPError)
    async def _control_plane_error(
        request: Request, exc: ControlPlaneHTTPError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("VALIDATION_ERROR", _describe_validation(exc)),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # The detail belongs in the log, not in the response.
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=internal_error_body())


def _describe_validation(exc: RequestValidationError) -> str:
    """Describe validation failures by FIELD, never by submitted VALUE.

    Echoing the rejected input back would risk reflecting a secret a client
    mistakenly sent, and Pydantic's raw errors can include the offending value.
    Only the field location and the rule that failed are reported.
    """
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part not in ("body",)
        )
        reason = str(error.get("msg", "is invalid"))
        problems.append(f"{location or 'request'}: {reason}")

    if not problems:  # pragma: no cover - FastAPI always supplies at least one
        return "the request does not satisfy the ChatRequest contract"
    return "the request does not satisfy the ChatRequest contract (" + "; ".join(
        problems
    ) + ")"


def app_factory() -> FastAPI:
    """Alias for ASGI servers that expect a zero-argument factory."""
    return create_app()


__all__: list[str] = ["API_TITLE", "API_VERSION", "app_factory", "create_app"]


def __getattr__(name: str) -> Any:  # pragma: no cover - convenience only
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}; this module exposes an "
        "application FACTORY (create_app), not a module-level 'app' instance"
    )

"""Centralized application configuration.

Spec 001, Task 9.

Configuration is read from the environment in exactly ONE place. Route handlers
never touch ``os.environ``: they receive a frozen ``Settings`` through the
application's dependency container, so behaviour is reproducible in tests and an
operator has a single surface to inspect.

    environment variables  ->  load_settings()  ->  Settings (frozen)
                                                       |
                                                       +-> create_app(settings=...)

Secrets
-------
The worker token is read from the environment (``STUDIO_WORKER_TOKEN``, the same
variable the worker uses in Task 8) and is excluded from ``repr``, so it cannot
reach a log line or a traceback through casual string formatting. No secret is
committed and no default token exists.

Deployment posture
------------------
Defaults are deliberately LOCAL and conservative: loopback host, no CORS origins,
the deterministic rule-based provider, and a development identity. Anything that
would be unsafe outside local development is refused by ``Settings.validate()``
when ``environment`` is not ``local`` — a missing worker token or a wildcard CORS
origin is a configuration error, not a warning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

#: The environment name that enables development-only affordances (fixed
#: development user identity, blank worker token, permissive defaults).
LOCAL_ENVIRONMENT = "local"

ENV_PREFIX = "STUDIO_API_"

ENV_ENVIRONMENT = f"{ENV_PREFIX}ENVIRONMENT"
ENV_HOST = f"{ENV_PREFIX}HOST"
ENV_PORT = f"{ENV_PREFIX}PORT"
ENV_ALLOWED_ORIGINS = f"{ENV_PREFIX}ALLOWED_ORIGINS"
ENV_AGENT_PROVIDER = f"{ENV_PREFIX}AGENT_PROVIDER"
ENV_DEVELOPMENT_USER_ID = f"{ENV_PREFIX}DEVELOPMENT_USER_ID"
ENV_PROJECT_IDS = f"{ENV_PREFIX}PROJECT_IDS"
ENV_HEARTBEAT_INTERVAL = f"{ENV_PREFIX}HEARTBEAT_INTERVAL_SECONDS"
ENV_ARTIFACT_ROOT = f"{ENV_PREFIX}ARTIFACT_ROOT"

#: Shared with the worker (Task 8) on purpose: it is the same pre-shared secret,
#: so it must not have two different names.
ENV_WORKER_TOKEN = "STUDIO_WORKER_TOKEN"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_AGENT_PROVIDER = "rule_based"
DEFAULT_DEVELOPMENT_USER_ID = "user_dev_local"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0

#: Spec 001 has exactly one project: the seed fixture. The control plane knows
#: this LOGICAL id only — never a filesystem path (see projects.py).
DEFAULT_PROJECT_IDS: tuple[str, ...] = ("proj_seed",)

#: Origins the Next.js dev server actually runs on. Applied ONLY in the ``local``
#: environment and only when nothing is configured, so `npm run dev` talks to
#: `uvicorn` without anyone having to set a variable first.
#:
#: This is an explicit two-entry allow-list, not a wildcard: `*` remains refused
#: outside local development, and a non-local deployment inherits NO default at all
#: and must name its origins.
DEFAULT_LOCAL_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


class ConfigurationError(RuntimeError):
    """The application is configured in a way that must not be allowed to run."""


def _split(value: Optional[str]) -> tuple[str, ...]:
    """Parse a comma-separated environment list, dropping blanks."""
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable control-plane configuration.

    Frozen so a request cannot mutate the configuration it runs under.
    """

    environment: str = LOCAL_ENVIRONMENT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    #: Pre-shared worker token. Verified by WorkerConnectionManager and then
    #: discarded; excluded from repr so it cannot leak through logs.
    worker_token: str = field(default="", repr=False)

    #: Explicit browser origins. Empty means CORS middleware is NOT installed at
    #: all. In the local environment this defaults to the Next.js dev server's
    #: loopback origins; outside local it defaults to nothing, so a deployment must
    #: name its origins deliberately.
    allowed_origins: tuple[str, ...] = ()

    #: Which AgentProvider to resolve. Configuration, not code: switching to
    #: "astra" later needs no route change.
    agent_provider: str = DEFAULT_AGENT_PROVIDER

    #: TEMPORARY: the fixed user identity the development identity resolver
    #: returns until real authentication exists.
    development_user_id: str = DEFAULT_DEVELOPMENT_USER_ID

    #: Logical project ids this control plane will accept.
    project_ids: tuple[str, ...] = DEFAULT_PROJECT_IDS

    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS

    #: Where generated preview artifacts live (Task 10). ``None`` means the
    #: artifact store's own git-ignored default under ``runtime/artifacts``.
    #:
    #: TEMPORARY LOCAL COUPLING: for Spec 001 the control plane and the worker run
    #: on one machine and share this directory, so the API can serve bytes the
    #: worker wrote. In a real deployment the worker uploads to object storage and
    #: the API reads from there — the same ``ArtifactStore`` boundary, a different
    #: implementation, and no shared filesystem.
    artifact_root: Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.environment == LOCAL_ENVIRONMENT

    def validate(self) -> "Settings":
        """Refuse configurations that would be unsafe to serve.

        Called by ``create_app``, so a misconfigured process fails at startup
        rather than at the first request.
        """
        if not self.environment.strip():
            raise ConfigurationError(f"{ENV_ENVIRONMENT} must not be blank")

        if "*" in self.allowed_origins and not self.is_local:
            raise ConfigurationError(
                f"{ENV_ALLOWED_ORIGINS} must not contain '*' outside the "
                f"'{LOCAL_ENVIRONMENT}' environment; list origins explicitly"
            )

        if not self.is_local and not self.worker_token.strip():
            raise ConfigurationError(
                f"{ENV_WORKER_TOKEN} is required outside the "
                f"'{LOCAL_ENVIRONMENT}' environment; workers must authenticate"
            )

        if not self.development_user_id.strip():
            raise ConfigurationError(f"{ENV_DEVELOPMENT_USER_ID} must not be blank")

        return self


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build Settings from the environment.

    Accepts an explicit mapping so tests never mutate the real process
    environment.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return (source.get(name) or default).strip()

    try:
        port = int(get(ENV_PORT, str(DEFAULT_PORT)))
    except ValueError as exc:
        raise ConfigurationError(f"{ENV_PORT} must be an integer") from exc

    try:
        heartbeat = float(
            get(ENV_HEARTBEAT_INTERVAL, str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
        )
    except ValueError as exc:
        raise ConfigurationError(f"{ENV_HEARTBEAT_INTERVAL} must be a number") from exc

    project_ids = _split(source.get(ENV_PROJECT_IDS)) or DEFAULT_PROJECT_IDS
    environment = get(ENV_ENVIRONMENT, LOCAL_ENVIRONMENT)

    # A configured value always wins. The loopback default applies only when the
    # operator said nothing AND this is local development.
    origins = _split(source.get(ENV_ALLOWED_ORIGINS))
    if not origins and environment == LOCAL_ENVIRONMENT:
        origins = DEFAULT_LOCAL_ALLOWED_ORIGINS

    return Settings(
        environment=environment,
        host=get(ENV_HOST, DEFAULT_HOST),
        port=port,
        # Not stripped: a token's surrounding whitespace could be significant.
        worker_token=source.get(ENV_WORKER_TOKEN) or "",
        allowed_origins=origins,
        agent_provider=get(ENV_AGENT_PROVIDER, DEFAULT_AGENT_PROVIDER),
        development_user_id=get(ENV_DEVELOPMENT_USER_ID, DEFAULT_DEVELOPMENT_USER_ID),
        project_ids=project_ids,
        heartbeat_interval_seconds=heartbeat,
        artifact_root=get(ENV_ARTIFACT_ROOT) or None,
    )


__all__ = [
    "DEFAULT_AGENT_PROVIDER",
    "DEFAULT_DEVELOPMENT_USER_ID",
    "DEFAULT_HOST",
    "DEFAULT_LOCAL_ALLOWED_ORIGINS",
    "DEFAULT_PORT",
    "DEFAULT_PROJECT_IDS",
    "ENV_ALLOWED_ORIGINS",
    "ENV_AGENT_PROVIDER",
    "ENV_ARTIFACT_ROOT",
    "ENV_DEVELOPMENT_USER_ID",
    "ENV_ENVIRONMENT",
    "ENV_HOST",
    "ENV_PORT",
    "ENV_PROJECT_IDS",
    "ENV_WORKER_TOKEN",
    "LOCAL_ENVIRONMENT",
    "ConfigurationError",
    "Settings",
    "load_settings",
]

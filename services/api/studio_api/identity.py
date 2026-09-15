"""Trusted identity — who the control plane believes is making a request.

Spec 001, Task 9.

THE RULE
--------
``user_id`` is NEVER read from the request body. The canonical ChatRequest has no
``user_id`` field at all (``additionalProperties: false``), so a client cannot even
send one; this module is what supplies it, from the server side.

    HTTP request  ->  IdentityResolver  ->  TrustedIdentity  ->  AgentContext
                      ^^^^^^^^^^^^^^^^
                      the ONLY source of user_id

Why an abstraction for a fixed string
-------------------------------------
Real authentication does not exist yet (it is a later task), but the *seam* where
it will live must exist now, otherwise identity handling leaks into route code and
becomes expensive to correct. ``DevelopmentIdentityResolver`` is deliberately
trivial and deliberately isolated: replacing it with a session/JWT/mTLS resolver is
a one-line change in the dependency container, and no route, service, or provider
is touched.

TEMPORARY BY DESIGN
-------------------
``DevelopmentIdentityResolver`` refuses to operate outside the ``local``
environment. It cannot be shipped by accident: a deployment that forgets to
install a real resolver fails closed with a structured error instead of silently
authenticating every caller as the development user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from .settings import LOCAL_ENVIRONMENT, Settings

#: How an identity was established. Recorded so tests and logs can prove the
#: identity did not come from the request body.
SOURCE_DEVELOPMENT_DEPENDENCY = "development_dependency"

#: Reserved for the real authentication task; listed here so the vocabulary is
#: visible rather than invented later.
SOURCE_SESSION_TOKEN = "session_token"


class IdentityError(RuntimeError):
    """The caller's identity could not be established."""


@dataclass(frozen=True)
class TrustedIdentity:
    """A server-established identity.

    Frozen: a request handler cannot escalate or rewrite the identity it was
    given.
    """

    user_id: str
    #: Which mechanism established this identity.
    source: str

    @property
    def is_development(self) -> bool:
        return self.source == SOURCE_DEVELOPMENT_DEPENDENCY


@runtime_checkable
class IdentityResolver(Protocol):
    """Establishes the trusted identity for one request.

    ``credentials`` is whatever the transport carries (an Authorization header
    today, a cookie or client certificate later). It is deliberately opaque here
    so the abstraction does not presuppose a mechanism.
    """

    def resolve(self, credentials: Optional[str] = None) -> TrustedIdentity:
        """Return the trusted identity, or raise IdentityError."""
        ...


@dataclass(frozen=True)
class DevelopmentIdentityResolver:
    """Resolves every request to one fixed development user.

    TEMPORARY. This is not authentication: it performs no verification and
    accepts no credentials. It exists so the vertical slice can run before the
    authentication task, and it is scoped to local development by an explicit
    environment check.
    """

    user_id: str
    environment: str = LOCAL_ENVIRONMENT

    def resolve(self, credentials: Optional[str] = None) -> TrustedIdentity:
        if self.environment != LOCAL_ENVIRONMENT:
            # Fail closed. A real deployment must install a real resolver.
            raise IdentityError(
                "the development identity resolver is only available in the "
                f"'{LOCAL_ENVIRONMENT}' environment; configure real "
                "authentication before serving other environments"
            )
        if not self.user_id.strip():
            raise IdentityError("no development user is configured")
        return TrustedIdentity(
            user_id=self.user_id, source=SOURCE_DEVELOPMENT_DEPENDENCY
        )


def default_identity_resolver(settings: Settings) -> IdentityResolver:
    """The resolver Spec 001 runs with.

    A single, obvious place to swap in real authentication.
    """
    return DevelopmentIdentityResolver(
        user_id=settings.development_user_id, environment=settings.environment
    )


__all__ = [
    "SOURCE_DEVELOPMENT_DEPENDENCY",
    "SOURCE_SESSION_TOKEN",
    "DevelopmentIdentityResolver",
    "IdentityError",
    "IdentityResolver",
    "TrustedIdentity",
    "default_identity_resolver",
]

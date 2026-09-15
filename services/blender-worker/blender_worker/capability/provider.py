"""``BlenderCapabilityProvider`` — the platform's Blender boundary.

Everything above this protocol is backend-agnostic. Nothing above it names an
official MCP tool, speaks MCP, or knows that Blender is driven by Python.

The point of the boundary is replaceability. When the official Blender MCP grows
native semantic tools, or a different backend is chosen, only an implementation of
this protocol changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from . import classifier

#: Error codes this layer can produce, all drawn from the canonical vocabulary.
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
BLENDER_UNAVAILABLE = "BLENDER_UNAVAILABLE"
MUTATION_FAILED = "MUTATION_FAILED"
VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass(frozen=True)
class CapabilityRequest:
    """One capability invocation against one project.

    ``arguments`` are validated canonical values: metres, radians, linear sRGB,
    and identifiers that exist in the scene. For ``execute_blender_python`` the
    arguments carry the model's ``code`` plus the approval token, if one was needed.
    """

    capability: str
    project_id: str
    project_path: Path
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: Present when a classified operation was explicitly approved by the user.
    approval_token: Optional[str] = None

    def argument(self, name: str, default: Any = None) -> Any:
        return self.arguments.get(name, default)


@dataclass(frozen=True)
class CapabilityResult:
    """The canonical outcome of a capability invocation."""

    ok: bool
    capability: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    #: Set when the backend refused because the user has not approved the step.
    assessment: Optional[classifier.RiskAssessment] = None

    @classmethod
    def success(cls, capability: str, data: Mapping[str, Any]) -> "CapabilityResult":
        return cls(ok=True, capability=capability, data=dict(data))

    @classmethod
    def failure(
        cls,
        capability: str,
        code: str,
        message: str,
        *,
        assessment: Optional[classifier.RiskAssessment] = None,
    ) -> "CapabilityResult":
        return cls(
            ok=False,
            capability=capability,
            error_code=code,
            error_message=message,
            assessment=assessment,
        )


@dataclass(frozen=True)
class BackendHealth:
    """Whether the Blender backend can currently serve capabilities."""

    available: bool
    backend: str
    detail: str = ""
    blender_version: Optional[str] = None
    server_version: Optional[str] = None
    pinned_commit: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "detail": self.detail,
            "blender_version": self.blender_version,
            "server_version": self.server_version,
            "pinned_commit": self.pinned_commit,
        }


@runtime_checkable
class BlenderCapabilityProvider(Protocol):
    """The only Blender interface the rest of the platform is allowed to use."""

    name: str

    def capabilities(self) -> tuple[str, ...]:
        """Capability names this backend can currently serve."""
        ...

    def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        """Execute one capability and return a canonical result."""
        ...

    def health(self) -> BackendHealth:
        """Report whether the backend is usable, without mutating anything."""
        ...

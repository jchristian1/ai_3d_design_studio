"""Artifact identity and storage.

Spec 001, Task 10. This is a security boundary.

    PreviewGenerator  --bytes-->  ArtifactStore  --(project_id, artifact_id)-->  API
                                       v
                              runtime/artifacts/<project_id>/<artifact_id>.png
                                                            <artifact_id>.json

WHAT AN ARTIFACT STORE MAY ADDRESS
----------------------------------
Only artifacts it created. Three independent defences make anything else
unreachable rather than merely unauthorized:

  1. ``artifact_id`` must match ``ARTIFACT_ID_PATTERN`` — a type prefix plus
     lowercase hex. A single safe path segment by construction, so ``..``,
     separators, and absolute paths are unrepresentable.
  2. The filename is built by the store from that id plus a fixed extension
     derived from the media type. A caller never supplies a filename, so a
     ``.blend``, a journal record, a recovery snapshot, an ``.env`` file, or a
     directory listing cannot be named.
  3. The resolved path is checked for containment inside the project directory
     AFTER symlink resolution, so a swapped symlink cannot escape either.

Identity is DERIVED, not allocated
----------------------------------
``derive_artifact_id(project_id, job_id, artifact_type)`` is a pure hash. That
single decision provides the retry semantics Task 10 requires without any
counters or locks:

  - the same completed job always maps to the same artifact, so a retry REUSES it
    instead of accumulating duplicates;
  - a genuinely new request has a new ``job_id`` and therefore a new artifact, so
    version history accrues naturally and an earlier preview is never overwritten.

Metadata lives beside the bytes
-------------------------------
Each artifact is two files: the image and a ``.json`` sidecar. The store reports
an artifact as present only when BOTH exist, so an interrupted write is treated as
absent and regenerated rather than served as a truncated image. The sidecar is
written last for exactly that reason.

Replacement path: ``LocalArtifactStore`` satisfies ``ArtifactStore``; an S3/object
storage implementation replaces it without touching the worker, the generator, or
the API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from studio_types import ARTIFACT_TYPES, PREVIEW_IMAGE, ArtifactType, PreviewArtifact

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Git-ignored root for generated artifacts, alongside the worker's runtime state.
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "runtime" / "artifacts"

#: Version tag so the derivation can evolve without silently colliding.
ARTIFACT_ID_VERSION = "v1"

#: The id prefix per artifact type. Kept explicit rather than derived from the
#: type name so a renamed type cannot silently change existing artifact ids.
ID_PREFIX_BY_TYPE: dict[str, str] = {PREVIEW_IMAGE: "preview"}

#: Mirrors the canonical ``preview-artifact.schema.json`` pattern. Both ends
#: enforce it: the contract rejects a bad id on the wire, and the store refuses to
#: touch the filesystem with one.
ARTIFACT_ID_PATTERN = re.compile(r"^(preview)_[a-z0-9]{8,64}$")

#: Media type -> file extension. A fixed, closed map: the extension is never taken
#: from a caller, so no arbitrary suffix (``.blend``, ``.json``, ``.py``) can be
#: written or read through this store.
EXTENSION_BY_MEDIA_TYPE: dict[str, str] = {"image/png": ".png"}

METADATA_SUFFIX = ".json"

#: A safe project_id: a single non-traversing path segment.
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ArtifactError(RuntimeError):
    """The artifact request is not usable."""


class UnsafeArtifactIdError(ArtifactError):
    """The identifier could not be used as a safe storage key."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def derive_artifact_id(
    project_id: str,
    job_id: str,
    artifact_type: ArtifactType = PREVIEW_IMAGE,
) -> str:
    """Derive the deterministic identity of an artifact.

    Project-scoped, so the same ``job_id`` in two projects yields two artifacts
    and project isolation holds even in the identifier space.

    Deterministic rather than random specifically so a retry of a completed job
    resolves to the artifact that already exists.
    """
    if artifact_type not in ARTIFACT_TYPES:
        raise ArtifactError(f"unknown artifact_type {artifact_type!r}")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ArtifactError("project_id must be a non-blank string")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ArtifactError("job_id must be a non-blank string")

    canonical = "|".join(
        [ARTIFACT_ID_VERSION, artifact_type, project_id, job_id]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{ID_PREFIX_BY_TYPE[artifact_type]}_{digest}"


def assert_safe_artifact_id(artifact_id: object) -> str:
    """Validate an artifact_id as a safe single path segment."""
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_PATTERN.match(artifact_id):
        raise UnsafeArtifactIdError(
            f"artifact_id {artifact_id!r} is not a valid artifact identifier"
        )
    return artifact_id


def assert_safe_project_id(project_id: object) -> str:
    """Validate a project_id as a safe single path segment."""
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
        raise UnsafeArtifactIdError(
            f"project_id {project_id!r} is not a safe storage segment"
        )
    return project_id


def checksum_of(data: bytes) -> str:
    """``sha256:<hex>`` of the given bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Store boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class ArtifactStore(Protocol):
    """Durable storage for generated artifacts.

    Contract an implementation MUST honour:

      1. Every operation is project-scoped. ``project_id`` is a required leading
         argument, and artifact ids are never resolved across projects.
      2. Only identifiers satisfying ``ARTIFACT_ID_PATTERN`` are accepted, and the
         stored filename is derived by the store — never supplied by a caller.
      3. ``get`` reports an artifact only when its bytes AND its metadata are both
         durably present, so a partially written artifact is treated as absent.
      4. ``put`` is idempotent for a given ``(project_id, artifact_id)``: writing
         the same artifact again replaces it in place rather than creating a
         second one.
    """

    def put(
        self,
        project_id: str,
        artifact_id: str,
        data: bytes,
        media_type: str,
        width: int,
        height: int,
        artifact_type: ArtifactType = PREVIEW_IMAGE,
        job_id: Optional[str] = None,
        engine: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> PreviewArtifact: ...

    def get(self, project_id: str, artifact_id: str) -> Optional[PreviewArtifact]: ...

    def read_bytes(self, project_id: str, artifact_id: str) -> Optional[bytes]: ...

    def exists(self, project_id: str, artifact_id: str) -> bool: ...

    def list_for_project(self, project_id: str) -> tuple[PreviewArtifact, ...]: ...


class LocalArtifactStore:
    """Local filesystem artifact store for the Spec 001 vertical slice.

    Layout under a git-ignored root::

        <root>/<project_id>/<artifact_id>.png     the bytes
        <root>/<project_id>/<artifact_id>.json    the metadata sidecar

    The root is chosen by the server, never by a request.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or DEFAULT_ARTIFACT_ROOT)

    # -- paths (all private; no path ever leaves this class) --------------

    def _project_dir(self, project_id: str) -> Path:
        return self.root / assert_safe_project_id(project_id)

    def _extension_for(self, media_type: str) -> str:
        extension = EXTENSION_BY_MEDIA_TYPE.get(media_type)
        if extension is None:
            raise ArtifactError(
                f"unsupported media_type {media_type!r}; supported: "
                + ", ".join(sorted(EXTENSION_BY_MEDIA_TYPE))
            )
        return extension

    def _metadata_path(self, project_id: str, artifact_id: str) -> Path:
        return self._project_dir(project_id) / (
            assert_safe_artifact_id(artifact_id) + METADATA_SUFFIX
        )

    def _data_path(
        self, project_id: str, artifact_id: str, media_type: str
    ) -> Path:
        return self._project_dir(project_id) / (
            assert_safe_artifact_id(artifact_id) + self._extension_for(media_type)
        )

    def _assert_contained(self, project_id: str, path: Path) -> Path:
        """Refuse a path that escapes its project directory.

        Checked after ``resolve()``, so a symlink swapped in underneath a
        previously valid entry cannot be used to read or write elsewhere.
        """
        project_dir = self._project_dir(project_id).resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(project_dir)
        except ValueError as exc:
            raise UnsafeArtifactIdError(
                "refusing to touch a path outside the artifact store"
            ) from exc
        return resolved

    # -- ArtifactStore ----------------------------------------------------

    def put(
        self,
        project_id: str,
        artifact_id: str,
        data: bytes,
        media_type: str,
        width: int,
        height: int,
        artifact_type: ArtifactType = PREVIEW_IMAGE,
        job_id: Optional[str] = None,
        engine: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> PreviewArtifact:
        """Store bytes plus metadata, returning the artifact reference."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            # An empty file is a failed render, not an artifact.
            raise ArtifactError("refusing to store an empty artifact")
        if artifact_type not in ARTIFACT_TYPES:
            raise ArtifactError(f"unknown artifact_type {artifact_type!r}")
        if width < 1 or height < 1:
            raise ArtifactError("artifact dimensions must be positive")

        data_path = self._data_path(project_id, artifact_id, media_type)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_contained(project_id, data_path.parent)

        artifact = PreviewArtifact(
            artifact_id=artifact_id,
            project_id=project_id,
            artifact_type=artifact_type,
            media_type=media_type,
            created_at=created_at or utc_now(),
            width=int(width),
            height=int(height),
            size_bytes=len(data),
            checksum=checksum_of(bytes(data)),
            job_id=job_id,
            engine=engine,
        )

        # Bytes first, metadata second. The metadata sidecar is what makes an
        # artifact visible, so a crash between the two leaves it ABSENT rather
        # than half-present — and the next attempt regenerates it.
        _atomic_write_bytes(data_path, bytes(data))
        _atomic_write_json(
            self._metadata_path(project_id, artifact_id), _to_wire(artifact)
        )
        return artifact

    def get(self, project_id: str, artifact_id: str) -> Optional[PreviewArtifact]:
        try:
            metadata_path = self._metadata_path(project_id, artifact_id)
        except ArtifactError:
            # An unsafe identifier is simply "not found" to a caller: the reason
            # is a server-side detail and must not become a probing oracle.
            return None
        if not metadata_path.exists():
            return None
        try:
            self._assert_contained(project_id, metadata_path)
            wire = json.loads(metadata_path.read_text("utf-8"))
            artifact = _from_wire(wire)
        except (ArtifactError, ValueError, OSError, KeyError, TypeError):
            return None

        # Metadata without bytes is not an artifact.
        try:
            data_path = self._data_path(project_id, artifact_id, artifact.media_type)
        except ArtifactError:
            return None
        if not data_path.exists():
            return None
        return artifact

    def read_bytes(self, project_id: str, artifact_id: str) -> Optional[bytes]:
        artifact = self.get(project_id, artifact_id)
        if artifact is None:
            return None
        try:
            path = self._assert_contained(
                project_id,
                self._data_path(project_id, artifact_id, artifact.media_type),
            )
            return path.read_bytes()
        except (ArtifactError, OSError):
            return None

    def exists(self, project_id: str, artifact_id: str) -> bool:
        return self.get(project_id, artifact_id) is not None

    def list_for_project(self, project_id: str) -> tuple[PreviewArtifact, ...]:
        """All artifacts for one project, oldest first.

        Only registered artifacts appear: the glob matches the metadata sidecar,
        and every candidate id is re-validated, so an unrelated file dropped into
        the directory is ignored rather than listed.
        """
        try:
            project_dir = self._project_dir(project_id)
        except ArtifactError:
            return ()
        if not project_dir.exists():
            return ()

        found: list[PreviewArtifact] = []
        for path in sorted(project_dir.glob(f"*{METADATA_SUFFIX}")):
            artifact_id = path.name[: -len(METADATA_SUFFIX)]
            if not ARTIFACT_ID_PATTERN.match(artifact_id):
                continue
            artifact = self.get(project_id, artifact_id)
            if artifact is not None:
                found.append(artifact)
        return tuple(sorted(found, key=lambda a: (a.created_at, a.artifact_id)))


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _to_wire(artifact: PreviewArtifact) -> dict[str, object]:
    """Serialize an artifact, dropping unset optionals."""
    wire: dict[str, object] = {
        "artifact_id": artifact.artifact_id,
        "project_id": artifact.project_id,
        "artifact_type": artifact.artifact_type,
        "media_type": artifact.media_type,
        "created_at": artifact.created_at,
        "width": artifact.width,
        "height": artifact.height,
        "size_bytes": artifact.size_bytes,
        "checksum": artifact.checksum,
    }
    if artifact.job_id:
        wire["job_id"] = artifact.job_id
    if artifact.engine:
        wire["engine"] = artifact.engine
    return wire


def to_artifact_wire(artifact: PreviewArtifact) -> dict[str, object]:
    """Public alias: the canonical wire document for an artifact."""
    return _to_wire(artifact)


def _from_wire(wire: dict) -> PreviewArtifact:
    return PreviewArtifact(
        artifact_id=assert_safe_artifact_id(wire["artifact_id"]),
        project_id=str(wire["project_id"]),
        artifact_type=wire["artifact_type"],
        media_type=str(wire["media_type"]),
        created_at=str(wire["created_at"]),
        width=int(wire["width"]),
        height=int(wire["height"]),
        size_bytes=int(wire["size_bytes"]),
        checksum=str(wire["checksum"]),
        job_id=wire.get("job_id"),
        engine=wire.get("engine"),
    )


def artifact_from_wire(wire: dict) -> PreviewArtifact:
    """Rebuild a typed artifact from its wire document."""
    return _from_wire(wire)


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes so a crash can never leave a truncated artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_bytes(
        path, json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    )


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "ARTIFACT_ID_VERSION",
    "DEFAULT_ARTIFACT_ROOT",
    "EXTENSION_BY_MEDIA_TYPE",
    "ArtifactError",
    "ArtifactStore",
    "LocalArtifactStore",
    "UnsafeArtifactIdError",
    "artifact_from_wire",
    "assert_safe_artifact_id",
    "assert_safe_project_id",
    "checksum_of",
    "derive_artifact_id",
    "to_artifact_wire",
    "utc_now",
]

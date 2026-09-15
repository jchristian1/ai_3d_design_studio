"""Keep the workspace's cached scene and artifact index following the worker.

The worker is the only component that can see Blender. When it finishes a capability
plan it reports the resulting scene and any artifacts it produced on the job result.
This observer copies that into durable workspace state, so:

* the next agent turn can be grounded without paying for another Blender read, and
* the browser can ask "what is the latest model?" without scanning job history.

Registered as a gateway observer, exactly like the job reconciler, so the gateway stays
unaware that either the scene cache or the artifact index exists.

The cache is never authoritative. Every mutation re-reads Blender and re-checks the
scene version in-lock, so a stale entry here can only ever make the agent reason about
an older scene — it cannot cause a wrong mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from studio_contracts.worker_protocol import JOB_RESULT

from .storage.repositories import StudioRepositories

_log = logging.getLogger(__name__)


@dataclass
class SceneReporter:
    """Copies scene and artifact facts from worker reports into durable state."""

    repositories: StudioRepositories

    def observe(self, message: Mapping[str, Any]) -> None:
        """Handle one validated worker message. Never raises."""
        try:
            self._observe(message)
        except Exception as error:  # reporting must never break the worker link
            _log.warning("scene reporting failed: %s", error)

    def _observe(self, message: Mapping[str, Any]) -> None:
        if message.get("type") != JOB_RESULT:
            return
        project_id = message.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return

        result = message.get("result")
        if isinstance(result, Mapping):
            self._record_scene(project_id, result.get("scene"))
            self._record_artifact(
                project_id,
                result.get("model"),
                job_id=message.get("job_id"),
                scene_version=_scene_version(result.get("scene")),
            )

        # A preview is reported at the top level of the result message, not inside
        # ``result``, because Spec 001 put it there.
        self._record_artifact(
            project_id,
            message.get("preview"),
            job_id=message.get("job_id"),
            scene_version=_scene_version(
                result.get("scene") if isinstance(result, Mapping) else None
            ),
        )

    def _record_scene(self, project_id: str, scene: Any) -> None:
        if not isinstance(scene, Mapping):
            return
        if not scene.get("scene_version") or not isinstance(scene.get("objects"), list):
            # Partial or malformed: better no cache than a misleading one.
            return
        self.repositories.scenes.put(project_id, dict(scene))
        self.repositories.projects.record_scene_version(
            project_id, str(scene["scene_version"])
        )

    def _record_artifact(
        self,
        project_id: str,
        artifact: Any,
        *,
        job_id: Optional[Any] = None,
        scene_version: Optional[str] = None,
    ) -> None:
        if not isinstance(artifact, Mapping):
            return
        artifact_id = artifact.get("artifact_id")
        artifact_type = artifact.get("artifact_type")
        media_type = artifact.get("media_type")
        if not (
            isinstance(artifact_id, str)
            and isinstance(artifact_type, str)
            and isinstance(media_type, str)
        ):
            return
        self.repositories.artifacts.record(
            project_id=project_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            media_type=media_type,
            job_id=str(job_id) if job_id else None,
            size_bytes=int(artifact.get("size_bytes") or 0),
            checksum=str(artifact.get("checksum") or ""),
            scene_version=scene_version,
            created_at=str(artifact.get("created_at")) if artifact.get("created_at") else None,
        )


def _scene_version(scene: Any) -> Optional[str]:
    if isinstance(scene, Mapping):
        value = scene.get("scene_version")
        return value if isinstance(value, str) else None
    return None

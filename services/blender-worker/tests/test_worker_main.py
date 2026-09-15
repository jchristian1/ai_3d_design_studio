"""Worker process entrypoint tests (Spec 001, Task 11).

The composition and startup behaviour of ``python -m blender_worker``. No sockets and
no Blender: the link client is a stub, because what matters here is the process's
DECISIONS — which projects it serves, when it retries, and when it refuses to.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

import pytest
from blender_worker.link.client import BackoffPolicy
from blender_worker.main import (
    _Stopping,
    discover_projects,
    register_with_retry,
    serve,
)

PROJECT_ID = "proj_seed"


class StubClient:
    """A link client that answers a scripted sequence of registration attempts."""

    def __init__(self, outcomes: list[bool], error: Optional[dict] = None) -> None:
        self.outcomes = list(outcomes)
        self.attempts = 0
        self.last_error = error or {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "could not connect",
        }
        self.backoff = BackoffPolicy(initial_seconds=0.0, max_seconds=0.0)
        self.state = "ready"
        self.reconciled = 0
        self.disconnected = False
        self.heartbeats = 0

    def connect_and_register(self, timeout: float = 0.0) -> bool:
        self.attempts += 1
        if not self.outcomes:
            return False
        return self.outcomes.pop(0)

    def reconcile(self) -> list[str]:
        self.reconciled += 1
        return []

    def handle_next(self, timeout: Optional[float] = None) -> Optional[str]:
        return None

    def send_heartbeat(self) -> bool:
        self.heartbeats += 1
        return True

    def disconnect(self) -> None:
        self.disconnected = True


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------


def test_projects_are_discovered_by_convention(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj_seed.blend").write_bytes(b"x")
    (root / "proj_other.blend").write_bytes(b"x")

    found = discover_projects(root)

    assert set(found) == {"proj_seed", "proj_other"}
    assert found["proj_seed"] == root / "proj_seed.blend"


def test_only_blend_files_are_registered(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj_seed.blend").write_bytes(b"x")
    # Things that must NOT become projects.
    (root / "notes.txt").write_text("hello")
    (root / "proj_seed.blend1").write_bytes(b"backup")
    (root / ".env").write_text("STUDIO_WORKER_TOKEN=secret")

    assert set(discover_projects(root)) == {"proj_seed"}


def test_a_missing_projects_root_yields_nothing(tmp_path: Path):
    assert discover_projects(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# Startup resilience
# ---------------------------------------------------------------------------


def test_registration_retries_while_the_control_plane_is_absent():
    """Terminal startup order must not matter."""
    client = StubClient([False, False, True])
    stopping = _Stopping()

    assert register_with_retry(client, stopping) is True
    assert client.attempts == 3


def test_registration_does_not_retry_a_refusal():
    """A bad token fails identically forever; retrying would only bury the reason."""
    client = StubClient(
        [False],
        error={"code": "VALIDATION_ERROR", "message": "worker authentication failed"},
    )

    assert register_with_retry(client, _Stopping()) is False
    assert client.attempts == 1, "a rejection must not be retried"


def test_registration_stops_when_shutdown_is_requested():
    client = StubClient([False, False, False])
    stopping = _Stopping()
    stopping.request()

    assert register_with_retry(client, stopping) is False
    assert client.attempts == 0, "no attempt after shutdown was requested"


def test_serve_returns_zero_when_stopped_before_registering():
    """Ctrl-C while waiting for the API is a clean exit, not a failure."""
    client = StubClient([])
    stopping = _Stopping()
    stopping.request()

    assert serve(client, stopping) == 0


def test_serve_reports_failure_when_registration_is_refused():
    client = StubClient(
        [False],
        error={"code": "VALIDATION_ERROR", "message": "authentication failed"},
    )
    assert serve(client, _Stopping()) == 1


def test_serve_reconciles_undelivered_results_at_startup():
    """A result the control plane never received is reported, not re-executed."""
    client = StubClient([True])
    stopping = _Stopping()

    class OneShot(_Stopping):
        """Stop after the first loop iteration so the test terminates."""

        def __init__(self, target: StubClient) -> None:
            super().__init__()
            self.target = target

        @property  # type: ignore[override]
        def requested(self) -> bool:  # noqa: D401
            return self.target.heartbeats >= 1

        @requested.setter
        def requested(self, _value: bool) -> None:
            pass

    assert serve(client, OneShot(client)) == 0
    assert client.reconciled >= 1
    assert client.disconnected is True
    del stopping


def test_shutdown_is_cooperative():
    stopping = _Stopping()
    assert stopping.requested is False
    stopping.request()
    assert stopping.requested is True
    # Idempotent: a second signal must not change anything.
    stopping.request()
    assert stopping.requested is True


# ---------------------------------------------------------------------------
# Layering and safety
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_the_entrypoint_imports_no_bpy():
    main_path = Path(__file__).resolve().parents[1] / "blender_worker" / "main.py"
    assert "bpy" not in _imports(main_path)


def test_the_entrypoint_hardcodes_no_credentials_or_workstation_paths():
    """No secret value and no machine-specific path may be baked into the process.

    The credential check is AST-based: `main.py` legitimately PRINTS
    `STUDIO_WORKER_TOKEN=<...>` in a help message, and a substring search would flag
    that guidance rather than a real hardcoded secret. What must not exist is a token
    or password assigned a literal value.
    """
    main_path = Path(__file__).resolve().parents[1] / "blender_worker" / "main.py"
    text = main_path.read_text("utf-8")

    for forbidden in ("/home/", "/snap/bin/blender", "legion"):
        assert forbidden.lower() not in text.lower(), f"main.py contains {forbidden}"

    tree = ast.parse(text)
    secret_names = {"token", "password", "secret", "api_key", "credential"}

    for node in ast.walk(tree):
        # token = "literal"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.lower() in secret_names
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value
                ):
                    raise AssertionError(f"main.py hardcodes {target.id}")
        # f(token="literal")
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg
                    and keyword.arg.lower() in secret_names
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and keyword.value.value
                ):
                    raise AssertionError(f"main.py passes a literal {keyword.arg}")


def test_project_resolution_goes_through_the_trusted_registry(tmp_path: Path):
    """A path outside the allowed root must be refused, not served."""
    from blender_worker.registry import MappingProjectRegistry, UnknownProjectError

    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj_seed.blend").write_bytes(b"x")

    registry = MappingProjectRegistry(root, discover_projects(root))

    assert registry.blend_path_for(PROJECT_ID) == (root / "proj_seed.blend").resolve()
    with pytest.raises(UnknownProjectError):
        registry.blend_path_for("proj_not_registered")

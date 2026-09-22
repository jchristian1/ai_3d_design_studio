"""The worker noticing its own code changed, and restarting itself.

This exists because of a specific, repeated failure: the worker ran for an hour while
its source was edited three times, and every symptom looked like a product bug. The
agent reported a material did not exist (it did), asked for approval that had been
fixed (it had), and rendered with a replaced renderer. Nothing in the running system
could state the one fact that mattered.

What is asserted here is the behaviour that prevents it, plus the two properties that
keep it safe: a reload must fire only when it can do no harm, and a request must fire
exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from blender_worker.reload import ReloadWatcher, short_fingerprint, source_fingerprint
from studio_contracts.worker_control import (
    reload_request_path,
    request_worker_reload,
    take_worker_reload_request,
)


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    """A throwaway source tree, so a test never watches the real one."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "__init__.py").write_text("x = 1\n", "utf-8")
    (root / "thing.py").write_text("def f():\n    return 1\n", "utf-8")
    return root


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_stable_for_unchanged_sources(sources: Path) -> None:
    assert source_fingerprint([sources]) == source_fingerprint([sources])


def test_editing_a_file_changes_the_fingerprint(sources: Path) -> None:
    before = source_fingerprint([sources])
    target = sources / "thing.py"
    target.write_text("def f():\n    return 2\n", "utf-8")
    # Saving normally moves mtime; set it explicitly so the test cannot depend on the
    # filesystem's timestamp resolution.
    import os

    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert source_fingerprint([sources]) != before


def test_adding_a_file_changes_the_fingerprint(sources: Path) -> None:
    before = source_fingerprint([sources])
    (sources / "extra.py").write_text("y = 2\n", "utf-8")
    assert source_fingerprint([sources]) != before


def test_bytecode_caches_are_ignored(sources: Path) -> None:
    """__pycache__ mirrors the sources it came from, so watching it is pure noise."""
    before = source_fingerprint([sources])
    cache = sources / "__pycache__"
    cache.mkdir()
    (cache / "thing.cpython-314.pyc").write_bytes(b"\x00\x01")
    (cache / "thing.py").write_text("not really source\n", "utf-8")
    assert source_fingerprint([sources]) == before


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    """A partial checkout must not stop a worker from starting."""
    assert source_fingerprint([tmp_path / "nope"]) == source_fingerprint([tmp_path / "nope"])


def test_the_short_form_is_short_and_derived() -> None:
    full = source_fingerprint()
    assert short_fingerprint(full) == full[:12]
    assert len(short_fingerprint(full)) == 12


# ---------------------------------------------------------------------------
# Deciding to restart
# ---------------------------------------------------------------------------


def test_an_unchanged_worker_does_not_restart(sources: Path, runtime: Path) -> None:
    watcher = ReloadWatcher(runtime, paths=[sources])
    assert watcher.should_restart() is None


def test_a_changed_worker_restarts(sources: Path, runtime: Path) -> None:
    watcher = ReloadWatcher(runtime, paths=[sources])
    (sources / "new.py").write_text("z = 3\n", "utf-8")

    reason = watcher.should_restart()
    assert reason and "code changed" in reason


def test_auto_reload_can_be_turned_off(sources: Path, runtime: Path) -> None:
    """Someone debugging a live worker must be able to stop it moving under them."""
    watcher = ReloadWatcher(runtime, auto_reload=False, paths=[sources])
    (sources / "new.py").write_text("z = 3\n", "utf-8")

    assert watcher.should_restart() is None


def test_an_explicit_request_restarts_even_with_auto_reload_off(
    sources: Path, runtime: Path
) -> None:
    """The button is an override, so it must not be subject to the same switch."""
    watcher = ReloadWatcher(runtime, auto_reload=False, paths=[sources])
    request_worker_reload(runtime)

    reason = watcher.should_restart()
    assert reason and "requested" in reason


def test_a_request_fires_exactly_once(sources: Path, runtime: Path) -> None:
    """Consuming the request is what stops a failed reload looping forever."""
    watcher = ReloadWatcher(runtime, auto_reload=False, paths=[sources])
    request_worker_reload(runtime)

    assert watcher.should_restart() is not None
    assert watcher.should_restart() is None


def test_the_request_file_is_removed_when_taken(runtime: Path) -> None:
    request_worker_reload(runtime)
    assert reload_request_path(runtime).is_file()

    assert take_worker_reload_request(runtime) is True
    assert not reload_request_path(runtime).is_file()
    assert take_worker_reload_request(runtime) is False


def test_the_control_plane_and_the_worker_agree_on_the_location(runtime: Path) -> None:
    """One source of truth for the path, because two would silently diverge.

    The control plane may not import the worker's package — a test enforces it — so the
    convention lives in the shared contracts package and both sides resolve it there.
    """
    watcher = ReloadWatcher(runtime, paths=[])
    assert watcher.request_path == reload_request_path(runtime)


# ---------------------------------------------------------------------------
# Restarting
# ---------------------------------------------------------------------------


def test_restarting_replaces_the_process_rather_than_spawning_one(
    sources: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``execv``, not a child.

    A child would outlive the supervisor's process group and become an orphan still
    holding the worker's token. Replacing the process keeps its identity and its place
    in the group that the studio's start script stops.
    """
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "blender_worker.reload.os.execv",
        lambda path, argv: calls.append((path, list(argv))),
    )

    ReloadWatcher(runtime, paths=[sources]).restart("because")

    assert len(calls) == 1
    _, argv = calls[0]
    assert argv[1:] == ["-m", "blender_worker"]

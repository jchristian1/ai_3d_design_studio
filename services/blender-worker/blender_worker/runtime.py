"""Where the worker keeps local runtime state.

Spec 001, Task 6.

Runtime state is generated, machine-local, and never committed. It lives under a
git-ignored ``runtime/`` directory at the repository root — deliberately NOT
inside ``tests/fixtures/``, which holds committed fixture sources.

    runtime/worker/
    ├── executions/<project_id>/<job_id>.json    execution journal
    ├── idempotency/<project_id>/<key>.json      mutation-identity bindings
    ├── locks/<project_id>.lock                  project locks
    └── recovery/<project_id>/<job_id>/...       pre-mutation .blend snapshots
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Git-ignored root for all generated worker state.
DEFAULT_RUNTIME_ROOT = REPO_ROOT / "runtime" / "worker"


def journal_root(runtime_root: Path | None = None) -> Path:
    return Path(runtime_root or DEFAULT_RUNTIME_ROOT)


def recovery_root(runtime_root: Path | None = None) -> Path:
    return Path(runtime_root or DEFAULT_RUNTIME_ROOT) / "recovery"

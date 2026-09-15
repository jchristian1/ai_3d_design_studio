"""blender-worker: executes Blender mutation jobs safely and repeatably.

Spec 001, Task 6.

Layering — each piece is replaceable without touching the others:

    executor.py     WorkerExecutor: orchestration only (no bpy, no fcntl)
    journal.py      WorkerExecutionStore + local atomic filesystem journal
    locks.py        ProjectLockProvider + flock implementation
    registry.py     ProjectLocator: project_id -> trusted .blend path
    blender_ops.py  BlenderOperationExecutor + fake and subprocess impls
    phases.py       internal execution phases (separate from the public Job status)
    runtime.py      where local runtime state lives

Local milestone limits, stated plainly: the lock is a single-machine flock, the
journal is local files, and each Blender operation spawns a headless subprocess.
All three sit behind abstractions so a distributed lock, a database journal, or a
persistent Blender process can replace them later.
"""

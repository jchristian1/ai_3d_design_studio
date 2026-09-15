"""HTTP and WebSocket route bindings.

Spec 001, Task 9.

Each module owns one concern and stays thin: parse, delegate to a service, render.
The orchestration lives in ``studio_api.chat_service``, so nothing here needs to be
rewritten when the agent provider, the job store, or the worker scheduler changes.

    health.py     GET  /health
    chat.py       POST /api/chat, POST /api/projects/{project_id}/chat
    jobs.py       GET  /api/projects/{project_id}/jobs/{job_id}
    artifacts.py  GET  /api/projects/{project_id}/artifacts/{artifact_id}
    workers.py    GET  /api/workers
    worker_ws.py  WS   /ws/workers
"""

from . import artifacts, chat, health, jobs, worker_ws, workers

__all__ = ["artifacts", "chat", "health", "jobs", "worker_ws", "workers"]

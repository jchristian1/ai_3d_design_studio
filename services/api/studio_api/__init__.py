"""API / control plane.

Spec 001. Task 8 added the worker-link boundary (authentication, registration,
liveness, job offering). Task 9 adds the FastAPI application that binds it to the
first application-facing API:

    HTTP ChatRequest
        v
    FastAPI control plane          app.py, routes/
        v
    trusted application context    identity.py, projects.py
        v
    AgentProvider                  (studio_agent — the replaceable AI boundary)
        v
    AgentPlan
        v
    JobFactory                     (studio_agent)
        v
    canonical Job                  (studio_contracts.jobs — Task 3)
        v
    WorkerConnectionManager        worker_link/manager.py (Task 8)
        v
    outbound-connected worker      routes/worker_ws.py

Modules:
    app.py               create_app factory, CORS, error handlers
    settings.py          all configuration, read from the environment once
    dependencies.py      the explicit dependency container
    identity.py          trusted identity (development resolver for now)
    projects.py          logical project registry — never filesystem paths
    chat_service.py      the orchestrator: interpret -> job -> offer
    job_records.py       control-plane job projection + in-memory store
    reconciliation.py    applies worker reports to job records
    worker_selection.py  which worker gets a job
    errors.py            structured failures and their HTTP mapping
    models.py            HTTP view models
    routes/              thin HTTP/WebSocket bindings
    worker_link/         the Task 8 protocol boundary + push gateway

Nothing in this package imports ``bpy``, Blender, or any worker implementation. The
control plane speaks canonical Jobs and the worker protocol only.

The application is created through the FACTORY ``studio_api.app.create_app``; there
is deliberately no module-level ``app`` instance.
"""

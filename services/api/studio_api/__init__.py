"""API / control plane.

Spec 001. Task 8 adds the worker-link boundary (authentication, registration,
liveness, job offering) plus a loopback development WebSocket server. The FastAPI
application and its HTTP/WebSocket route bindings are the API task's work; all
framework-independent decision logic already lives in
``studio_api.worker_link.manager`` so that binding stays thin.
"""

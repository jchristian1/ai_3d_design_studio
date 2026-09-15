"""API / control-plane service (FastAPI).

Responsibilities (see .kiro/steering/architecture.md): authentication, projects,
sessions, jobs, files, versions, realtime events, worker coordination. Validates
inbound requests against the shared contract and invokes the AgentProvider
abstraction — never a concrete provider directly.
"""

"""Agent orchestration service.

The rest of the system communicates only through the AgentProvider abstraction
(see .kiro/steering/agent-context.md). Concrete providers live in
``providers/``. Spec 001 uses the deterministic RuleBasedProvider; AstraProvider
and CodexProvider are added later WITHOUT changing API, jobs, MCP, worker, or
Blender code.
"""

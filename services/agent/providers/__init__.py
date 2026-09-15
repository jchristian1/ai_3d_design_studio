"""AgentProvider implementations.

Structure (see .kiro/steering/agent-context.md):

    AgentProvider              (abstract interface — defined in Task 6)
    ├── RuleBasedProvider      (Spec 001 initial, deterministic — Task 6)
    ├── AstraProvider          (added later, no caller changes)
    └── CodexProvider          (added later, no caller changes)

Task 1 only reserves this package. The interface and RuleBasedProvider are
implemented in Task 6. Callers (API, jobs, MCP, worker, Blender) depend only on
the AgentProvider interface and never on a concrete provider.
"""

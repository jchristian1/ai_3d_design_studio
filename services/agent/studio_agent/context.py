"""AgentContext — the trusted context an interpretation runs against.

Spec 001, Task 7.

Identity comes from the control plane, never from the provider and never from the
user's message. A provider that could name its own ``project_id`` would be a
tenant-isolation hole, so identity is supplied here and the AgentPlan a provider
returns carries no identity at all.

Deliberately small for this milestone. The fields Spec 001 needs are present; the
richer context described in .kiro/steering/agent-context.md (project brief, hard
constraints, design decisions, scene snapshot, session summary, recent
conversation) is future work and is NOT stubbed here — persistent project memory
and context retrieval are not part of Task 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AgentContext:
    """Trusted, control-plane-supplied context for one interpretation.

    Frozen so an interpretation cannot mutate the identity it was handed.
    """

    #: Authenticated user. Supplied by the control plane; never parsed from text.
    user_id: str
    project_id: str
    session_id: str
    #: The object the user currently has selected in the browser, when any.
    selected_object_id: Optional[str] = None

    # Future expansion, intentionally absent for now:
    #   project_brief, constraints, design_decisions, scene_snapshot,
    #   session_summary, recent_messages, references

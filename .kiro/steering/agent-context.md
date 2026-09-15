# Agent and Context Architecture

## Fundamental Rule

Conversation history is not permanent project storage.

The platform owns durable project memory.

## Context Builder

For an agent request, construct context using:

1. system rules
2. project brief
3. hard constraints
4. design decisions
5. current scene snapshot
6. relevant files and references
7. session summary
8. recent conversation
9. selected browser object/state
10. current user request

## Project Memory

Persist information such as:

- design style
- dimensions
- constraints
- materials
- design decisions
- preferences
- important revisions
- current Blender version
- references

Example:

Project:
Beach House Kitchen

Constraints:
- ceiling height: 2.70 m
- minimum walkway: 1.00 m

Decisions:
- white oak cabinetry
- black quartz countertops
- 3000K lighting

## Sessions

A project may contain multiple sessions.

A session references:

- project_id
- user_id
- agent provider
- provider session/thread ID when applicable
- messages
- summary
- tool calls
- versions created

## Agent Provider

Do not couple the application directly to one model.

Architecture:

AgentProvider
├── AstraProvider
├── CodexProvider
└── FutureProvider

The rest of the system communicates with AgentProvider.

## Agent Responsibilities

The agent may:

- understand user requests
- inspect context
- plan
- call MCP tools
- inspect results
- retry safely
- explain completed work

The agent must not be treated as persistent storage.

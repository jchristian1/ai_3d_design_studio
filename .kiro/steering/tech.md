# Technology Direction

## Development Environment

Operating System:
Ubuntu

Primary Workstation:
Lenovo Legion T7

GPU:
NVIDIA RTX 4070 Ti

3D Engine:
Blender

## Preferred Stack

Frontend:
- TypeScript
- React
- Next.js
- Three.js

Backend:
- Python
- FastAPI

Agent orchestration:
- provider abstraction
- Astra / Codex initially
- model provider must be replaceable

AI tool interface:
- Model Context Protocol (MCP)

Database:
- PostgreSQL

Queue and locking:
- Redis

Realtime communication:
- WebSocket initially
- WebRTC may be introduced later

3D browser preview:
- glTF / GLB
- Three.js

Rendering:
- Blender
- Cycles
- NVIDIA RTX acceleration

Infrastructure:
- Docker where appropriate
- local development first
- cloud control plane later

## Engineering Principles

Prefer:

- explicit interfaces
- modular architecture
- typed contracts
- testable services
- provider abstractions
- semantic MCP tools

Avoid:

- tight coupling to one AI provider
- arbitrary Python execution as the main Blender interface
- exposing Blender publicly
- treating chat history as project storage

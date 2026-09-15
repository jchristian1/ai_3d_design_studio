# Repository Structure

This project uses a monorepo.

Expected structure:

ai_3d_design_studio/
├── apps/
│   └── web/
├── services/
│   ├── api/
│   ├── agent/
│   ├── blender-worker/
│   ├── blender-mcp/
│   └── preview/
├── packages/
│   ├── contracts/
│   ├── types/
│   ├── ui/
│   └── validation/
├── database/
│   └── migrations/
├── infra/
│   ├── local/
│   └── docker/
├── tests/
│   ├── integration/
│   ├── blender/
│   ├── agent/
│   └── e2e/
└── .kiro/
    ├── steering/
    └── specs/

## Responsibilities

apps/web
Browser user interface.

services/api
Authentication, projects, sessions, jobs, files, versions and realtime events.

services/agent
Agent orchestration, context building, planning, tool loops and model provider abstraction.

services/blender-worker
Runs on machines performing actual Blender operations.

services/blender-mcp
Provides safe semantic Blender operations through MCP.

services/preview
Creates browser previews and manages preview/render events.

packages/contracts
Shared API and event contracts.

packages/types
Shared data types.

packages/validation
Reusable validation rules.

## Rule

Logical boundaries must exist from the beginning even if multiple components initially run in one process.

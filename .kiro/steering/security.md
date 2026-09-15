# Security Architecture

## Core Rule

Blender must never be directly exposed to the public internet.

Do not publicly expose:

- Blender ports
- Blender Python console
- local Flask command servers
- local MCP endpoints

## Worker Connection

The Blender Worker establishes an outbound authenticated encrypted connection to the control plane.

Preferred architecture:

Blender Worker
→ TLS / WSS
→ Control Plane

The control plane sends authorized jobs through this secure connection.

## Identity

Requests should eventually include:

- user_id
- project_id
- session_id

## Authorization

Users may only access projects they own or have permission to access.

## Project Isolation

Every job must explicitly identify its project.

A worker must never accidentally modify another project's Blender file.

## MCP Safety

Prefer semantic tools.

Avoid unrestricted:

execute_python(code)

as the normal AI interface.

Developer-only dangerous tooling must be isolated and disabled in production.

## Secrets

Secrets must use environment variables.

Never commit:

- API keys
- passwords
- tokens
- private certificates

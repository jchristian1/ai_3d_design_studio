# System Architecture

## Main Flow

Browser
→ API / Control Plane
→ Agent Orchestrator
→ MCP
→ Blender Worker
→ Blender
→ Preview / Render
→ Browser

## Web Application

Responsibilities:

- login
- project picker
- chat
- uploads
- interactive preview
- render viewing
- object selection
- job status
- version history

## API / Control Plane

Responsibilities:

- authentication
- users
- projects
- sessions
- project memory
- jobs
- files
- versions
- permissions
- realtime events
- worker coordination

## Agent Orchestrator

Responsibilities:

- receive user requests
- construct relevant context
- reason about requested changes
- select MCP tools
- execute tool loops
- verify results
- summarize work

Agents must be accessed through an AgentProvider abstraction.

## MCP

MCP is the structured tool interface between AI reasoning and Blender capabilities.

Example tools:

- inspect_scene
- get_object
- move_object
- resize_object
- create_wall
- create_opening
- measure_distance
- set_material
- set_light
- create_camera
- render_preview
- save_version

## Blender Worker

Responsibilities:

- connect securely to control plane
- receive jobs
- acquire project lock
- load project
- create recovery point
- execute Blender operations
- verify results
- autosave
- generate previews
- report status

## Persistent State

PostgreSQL stores:

- users
- projects
- sessions
- constraints
- design decisions
- jobs
- versions
- metadata

Object storage stores:

- floor plans
- photos
- textures
- renders
- GLB previews
- Blender snapshots

The .blend file remains the authoritative detailed 3D scene state.

## First Vertical Slice

The first milestone must support:

User enters:

"Move Cube 50 cm to the right."

Flow:

Browser
→ API
→ AgentProvider
→ MCP
→ Blender Worker
→ Blender

Blender must:

- move Cube +0.50 meters on X
- save the project
- generate an updated preview

Then:

Preview
→ API
→ Browser

The browser shows successful completion and the updated result.

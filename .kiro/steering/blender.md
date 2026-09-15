# Blender Rules

## Units

Meters are the canonical unit.

Examples:

50 cm = 0.50 m
240 cm = 2.40 m

## Source of Truth

The .blend file is the authoritative source of detailed 3D scene state.

The database may maintain scene indexes and summaries but does not replace Blender's scene model.

## Modification Workflow

For important scene changes:

1. identify project
2. acquire project lock
3. create recovery point
4. validate operation
5. execute operation
6. inspect resulting state
7. save
8. update scene snapshot
9. generate preview when appropriate
10. release lock

## Object Identity

Use stable machine-readable IDs whenever possible.

Example:

object_id:
obj_8d83f

display_name:
Kitchen Island

Blender name:
KitchenIsland_obj_8d83f

## MCP Philosophy

Prefer focused tools:

- move_object
- resize_object
- create_wall
- create_door
- set_material
- measure_distance

Arbitrary Python execution must not be the primary interface.

## Preview and Rendering

Support progressively:

1. viewport screenshot
2. quick preview render
3. final Cycles render
4. interactive GLB preview
5. optional live viewport streaming

## GPU

Use NVIDIA RTX acceleration where available.

Initial machine:
RTX 4070 Ti

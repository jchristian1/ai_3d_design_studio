# Testing Strategy

## Principle

Every critical system boundary needs automated testing.

## Unit Tests

Test:

- validation
- transformations
- context construction
- job state logic
- provider adapters

## MCP Tests

Test Blender operations independently.

Example:

Given:
Cube X = 0

When:
move_object delta_x = 0.50

Then:
Cube X must equal 0.50

## Worker Tests

Test:

- job claiming
- locks
- retries
- failure reporting
- autosave
- recovery points

## Integration Tests

Test:

API
→ job
→ worker
→ MCP
→ Blender

## End-to-End Test

First mandatory E2E scenario:

1. open test project
2. send "Move Cube 50 cm to the right"
3. agent determines correct operation
4. worker receives job
5. Blender moves cube
6. Blender saves
7. preview updates
8. browser reports success

## Verification

Do not assume a Blender operation worked simply because no exception occurred.

Important operations must verify resulting scene state.

## Failure Cases

Test:

- Blender unavailable
- worker disconnected
- MCP failure
- project lock conflict
- render failure
- invalid object
- invalid units
- duplicate job
- interrupted operation

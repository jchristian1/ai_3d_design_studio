"""The worker side of the control-plane link.

Spec 001, Task 8.

    transport.py            WorkerTransport Protocol + InMemoryTransport
    websocket_transport.py  outbound WebSocket implementation (ws:// or wss://)
    identity.py             worker identity + capabilities, from the environment
    client.py               WorkerLinkClient: connection state, protocol,
                            reconnect with bounded backoff, reconciliation

The worker CONNECTS OUTBOUND. Nothing here listens or binds, so the workstation
needs no inbound public port and Blender, bpy and the local MCP boundary stay
unreachable from the network.
"""

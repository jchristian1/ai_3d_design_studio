# Worker ↔ Control-Plane Protocol (Spec 001, Task 8)

How a Blender Worker on the Ubuntu workstation connects to the control plane and
receives authorized jobs.

## Sequence

```
Worker                         Control Plane
  |                                  |
  |------ outbound connect --------->|
  |------ hello + auth ------------->|
  |<----- worker_registered ---------|
  |                                  |
  |<----- job_offer -----------------|  control plane pushes
  |------ job_accepted ------------->|
  |                                  |
  |       WorkerExecutor             |
  |          Blender                 |
  |                                  |
  |------ job_result --------------->|
  |                                  |
```

## 1. Protocol versioning

Every message carries `protocol_version`. The current version is `2`, and
`SUPPORTED_PROTOCOL_VERSIONS` lists what a build can speak — currently `(1, 2)`. A
worker presenting an unsupported version is rejected at hello with a clear reason and
is never registered, so it can never be offered a job.

The vocabulary itself is a closed enum in `worker-message.schema.json`; both ends
validate against that canonical schema, so neither side can invent a message the
other does not understand.

### Why v2, for an *optional* field

Task 10 added optional `preview` and `preview_error` fields to `job_result`. That
still bumped the version, deliberately.

Because every message is validated with `additionalProperties: false`, a peer that
predates a new field **rejects** any message carrying it. So an additive optional
field is a breaking vocabulary change on the wire, even though it looks harmless.
Relying on "optional means compatible" would produce mysterious validation failures
against an older peer.

v1 remains supported: a v1 worker simply never reports a preview, which is a degraded
but entirely valid worker. The control plane can therefore be upgraded before the
workstations are.

Contrast the worker's on-disk journal, which did **not** bump its record version for
the same change: `from_wire` filters to known fields and every new field has a
default, so a v1 record loads correctly and a v1 reader ignores the new keys. Bumping
there would make existing local journals unreadable for no safety gain.

## 2. Outbound worker connection

**The worker always initiates.** The workstation never listens, so it needs no
inbound public port, and Blender, `bpy` and the local MCP boundary are never
reachable from the network.

`WorkerTransport` implementations must connect out only. A test walks the AST of
every module under `blender_worker/link/` and fails if any of them calls `bind()`,
`listen()`, `serve()` or `serve_forever()`.

The control plane is the side that listens — which is correct and does not weaken
the workstation's posture.

## 3. Worker authentication

`worker_hello` carries `worker_id`, `protocol_version`, `capabilities` and a
pre-shared `token`. The token:

- comes from `STUDIO_WORKER_TOKEN` in the environment, never from source
- is compared with `hmac.compare_digest`, so comparison is constant time
- is **never stored** by the control plane — it is verified and discarded
- is excluded from `WorkerIdentity.__repr__`, so a traceback cannot print it
- is redacted by `protocol.redact()` before anything is logged or retained
- is forbidden on every other message type by a schema conditional

Rejection messages are deliberately vague ("worker authentication failed") and
never echo the presented value.

This is application-layer authentication over an assumed-secure transport. No
custom cryptography is implemented. It is replaceable later by mTLS or signed
short-lived credentials without touching `WorkerExecutor`.

## 4. Registration

The control plane validates, in order: protocol version, then token. Only then does
it record a `RegisteredWorker` and reply `worker_registered` with a
`connection_id` and the `heartbeat_interval_seconds` the worker should use. An
invalid worker is never recorded and therefore never appears in
`available_workers()`.

## 5. Live connection registry

On registration the local server records the live connection under `worker_id`
(guarded by a lock) and removes it when the connection ends. This registry is what
makes server-initiated dispatch possible.

## 6. Server-pushed job offers

`queue_offer(worker_id, job)` sends immediately when the worker is connected, and
only queues when it is not — queued offers are flushed at registration.

### The deadlock that was found

The first implementation flushed queued offers only *in reaction to* an inbound
worker message, while `_handle_connection` blocked on `for raw in connection:`. An
idle worker therefore deadlocked with the server: the server waited to read while
the worker waited to be offered a job. Symptoms were a process at 0% CPU parked in
`futex_do_wait` with no Blender process alive.

It was masked in testing because the original happy-path test called
`send_heartbeat()` before waiting, which incidentally gave the server an inbound
message to react to.

**Invariant, now pinned by a regression test:**

> Dispatch of a queued job to a connected ready worker MUST NOT depend on an
> inbound heartbeat or other worker message.

`test_idle_worker_receives_a_pushed_offer_without_sending_anything_first` registers
a worker, sends nothing else at all, queues an offer, and asserts the offer arrives
while the server's inbound transcript still contains exactly one message (the
hello). Reinstating the defect makes it fail.

## 7. Job acceptance and rejection

Acceptance is explicit — socket delivery is not acceptance. Before accepting, the
worker validates that:

- the message satisfies the protocol envelope
- the carried job satisfies the canonical Job contract (`validate_job`)
- the envelope's `job_id`/`project_id` match the job it carries
- `job_type` is in this worker's supported list
- the worker is not already busy

Anything else produces `job_rejected` with a structured code:
`VALIDATION_ERROR` for an invalid job, unsupported type or envelope mismatch, and
`LOCK_CONFLICT` when busy. Nothing reaches `WorkerExecutor` until every check
passes.

The `job` field is deliberately **not** `$ref`'d to `job.schema.json` in the
envelope schema. Envelope validation validates envelopes; the Job contract is
checked separately at both ends. If a malformed job failed envelope validation it
would be dropped as unparseable and the control plane would never receive the
`job_rejected` explaining why.

## 8. Heartbeats and liveness

The worker sends `heartbeat` with `worker_state` of `ready` or `busy`; the control
plane replies `heartbeat_ack`. Liveness is derived from the last heartbeat against
`heartbeat_interval_seconds × liveness_grace_multiplier`, giving three
distinguishable states:

| State | Meaning |
|---|---|
| `healthy` | connected, idle, offerable |
| `busy` | connected and working — not offerable |
| `lost` | no heartbeat within the grace window |

The interval and grace multiplier are configuration, and the clock is injected, so
tests never sleep or depend on wall-clock timing.

## 9. Job progress and results

`job_progress` carries the internal execution phase for observability only — it is
never a contract. `job_result` carries `job_status` of `succeeded`, `failed` or
`duplicate`, plus the structured result or error. A failed result must carry an
error, enforced by a schema conditional.

`duplicate` means the job was already completed locally and the stored result is
being reported, not re-executed.

### Preview fields (v2)

`job_result` may also carry `preview` (a `PreviewArtifact` reference) and
`preview_error`. Both are deliberately separate from `result` and `error`:

- a preview describes a *picture of* the mutation, not the mutation itself;
- a failed preview must never be able to make a durably-saved design change look
  like a failed one.

Two schema conditionals keep that honest: the preview fields are forbidden on every
message type except `job_result`, and `preview` is forbidden when `job_status` is
`failed` — attaching one would imply a picture of a change that was never applied.

The reference contains no path and no URL. `(project_id, artifact_id)` is the whole
address, because a worker must not know the control plane's route shape.

## 10. Disconnect behaviour

A send or receive failure moves the client to `disconnected`. The control plane
independently sees a silent worker become `lost` once the heartbeat grace window
passes.

Connection state (`disconnected`, `connecting`, `authenticating`, `ready`, `busy`,
`reconnecting`) is kept strictly separate from the Task 6 execution phases — a test
asserts the two vocabularies do not overlap. **Network state is never the source of
truth for whether a Blender mutation happened.** The durable journal is.

## 11. Reconnect behaviour

`reconnect()` uses bounded exponential backoff (`BackoffPolicy`: configurable
initial delay, multiplier, hard ceiling, optional attempt cap). No jitter, so the
delay sequence is deterministic and assertable; the ceiling and attempt cap mean it
can never spin in a tight loop. Sleep is injected, so tests do not wait.

Every reconnect re-authenticates and re-registers from scratch and receives a new
`connection_id` — the control plane never trusts a resumed connection. After
registering, the client reconciles.

## 12. Durable result reconciliation

`_deliver_result` records in the journal whether the result actually reached the
control plane (`result_delivered`). After a reconnect, `reconcile()` reads the
journal — not the network — for terminal executions still undelivered and resends
them as `duplicate`.

Resending a stored result cannot mutate Blender, so reconciliation is inherently
safe to repeat. `result_delivered` is purely a *reporting* flag: it never affects
whether the mutation happened.

## 13. Duplicate delivery semantics

Redelivery of the same `job_id` over the socket is safe because safety lives below
the transport, in Task 6. The executor finds the completed record, returns
`duplicate=True`, and performs no mutation.

Three deliveries of the same offer produce statuses
`["succeeded", "duplicate", "duplicate"]` with exactly one Blender mutation. A
genuinely new request (new `request_id`) still moves the object again, so
idempotency never blocks a legitimate repeat command.

### Why a dropped result channel cannot duplicate a mutation

The mutation and the `.blend` save complete before the result is sent. If the send
fails:

1. the mutation is already durable on disk and recorded `completed` in the journal
2. the journal marks the result undelivered
3. reconnect + reconcile **resends the stored result**

Re-executing is never the recovery path. And even if the control plane re-offers
the same job, the executor recognises it as complete and mutates nothing.

## 14. Security boundaries

The protocol cannot express arbitrary execution. `type` is a closed enum and
`additionalProperties: false` applies at every level, so there is no
`execute_python`, no shell command, no `eval`/`exec`, no `subprocess`, no script or
code field, and no filesystem path — tested field by field. `MoveObjectPayload` is
likewise closed to `{target, delta_meters}`.

Other guarantees:

- all network input is untrusted and validated before use; malformed messages are
  dropped and counted, never acted on
- `WorkerExecutor` imports no transport, no `websockets`, no `socket`, no
  `asyncio`, and never mentions `WorkerTransport` or the protocol — asserted by AST
- nothing under `blender_worker/link/` imports `bpy`
- the control-plane manager imports no web framework, so it is reusable behind a
  FastAPI route
- no natural language crosses the link: the path carries canonical Jobs only

## 15. Local development vs production transport

| | Local development | Production |
|---|---|---|
| URL | `ws://127.0.0.1:<port>/ws/workers` | `wss://api.example.com/ws/workers` |
| Server | `LocalControlPlaneServer` (loopback) | FastAPI/Starlette route (API task) |
| Transport security | none; loopback only | TLS, terminated at the server |
| Worker auth | pre-shared token | token, then mTLS / short-lived credentials |

Only the URL changes for the worker: `wss://` is handled by the library's standard
TLS support, and `WebSocketWorkerTransport(require_secure=True)` refuses plaintext
outright. All decision logic already lives in the framework-independent
`WorkerConnectionManager`, so the future FastAPI binding is thin and
`WorkerExecutor` is untouched.

The local server authenticates at the application layer and has no TLS of its own.
It is for loopback development only.

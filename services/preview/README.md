# Preview and Artifacts — `services/preview` (Spec 001, Task 10)

Turns a verified Blender mutation into a browser-consumable image.

```
canonical Job
     ↓
WorkerExecutor            mutate → verify → save
     ↓
PreviewGenerator          generator.py — the abstraction
     ↓
Blender subprocess        blender_preview.py → blender_scripts/render_preview.py
     ↓
PNG bytes
     ↓
ArtifactStore             artifacts.py — durable, project-scoped
     ↓
control plane             reports (project_id, artifact_id) — NEVER a path
     ↓
GET /api/projects/{project_id}/artifacts/{artifact_id}
```

## Modules

| File | Responsibility |
|---|---|
| `generator.py` | `PreviewGenerator` Protocol, `PreviewRequest`/`PreviewRender`/`PreviewOutcome`, `FakePreviewGenerator` |
| `artifacts.py` | `ArtifactStore` Protocol, `LocalArtifactStore`, derived artifact identity, checksums |
| `blender_preview.py` | `BlenderPreviewGenerator` — host side, subprocess, **no bpy** |
| `blender_scripts/render_preview.py` | the only code that runs inside Blender and imports `bpy` |

### Deliberate import layering

The package exports the artifact and generator **boundaries** only.
`BlenderPreviewGenerator` is intentionally not re-exported: it must be imported from
`studio_preview.blender_preview`.

That is not style. The control plane needs `ArtifactStore` to serve previews over
HTTP but must never acquire a dependency on Blender, on subprocess execution, or on
the render scripts. A test asserts the API imports only `studio_preview.artifacts`.

## PreviewGenerator

```python
class PreviewGenerator(Protocol):
    name: str
    def generate(self, request: PreviewRequest) -> PreviewOutcome: ...
```

The worker contains no rendering logic. It hands over a trusted project path plus a
target size and receives pixels or a structured error.

**Why bytes, not a path.** Rendering and persistence are separate concerns: the
generator produces an image, `ArtifactStore` decides where bytes live and what they
are called. Returning bytes means the generator never picks a storage location, so a
future object-storage backend needs no change here, and a caller can never be handed
a path to leak. Preview images are tens to low hundreds of kilobytes, so holding one
in memory is cheaper than coordinating temp files.

**Failures are values.** Blender absent, render timeout, unreadable project — every
failure returns a `PreviewOutcome` with a canonical error code. Nothing raises,
because a failed preview must never make a durably-saved mutation look failed.

**Future implementations** fit without touching `WorkerExecutor`: a quick Eevee
render, a final Cycles render, a GLB export for interactive Three.js preview. A live
viewport stream would *not* use this Protocol — a stream is not a single artifact,
and pretending otherwise would distort both.

## Camera strategy

The seed fixture has one Cube and **no camera**, so a preview needs one.

The render script adds a temporary camera to the in-memory scene and **never saves
the `.blend`**. There is no `save_as_mainfile` or `save_mainfile` anywhere in it, and
a test asserts the file's SHA-256 is unchanged by rendering, plus that the saved
project still contains only `Cube`.

That is what makes "add a camera" safe. Permanently adding one so previews have
something to render through would mean every preview silently edits the design, and
the user would find objects they never asked for.

Framing, computed rather than hand-tuned:

```
position (7, -7, 5) m, looking at the world origin, 50 mm lens
```

The look-at rotation is derived from the direction vector, so there are no magic
Euler angles. The view is deliberately off-axis on X, so movement along world +X
appears as a clear horizontal displacement — the entire point of the Spec 001
preview.

This is **not** automatic architectural camera composition. It frames a small scene
near the origin and nothing more; framing a real room needs a bounding-box or
scene-aware strategy, which is future work.

## Render settings

Workbench, fixed small resolution (default 640×360; tests use 320×180), fixed flat
background `(0.05, 0.05, 0.07)`, `render_aa = 8`.

| Choice | Reason |
|---|---|
| `BLENDER_WORKBENCH` | Solid-shading rasteriser: no light sampling, so no noise, no seed dependence, no denoiser version differences. Two renders of one scene are byte-identical. |
| Workbench studio lighting | Independent of scene lights, so a preview works in a scene with no lights — which the seed fixture is. |
| No GPU | Workbench rasterises on the CPU. A preview must never be why CI or a headless machine cannot verify the slice. Cycles and RTX belong to the final-render path. |
| Fixed AA, fixed resolution | Nothing adaptive, so output cannot vary per run. |

Determinism matters beyond tidiness: it is what makes the checksum a usable signal
for "did the visible scene change?".

### Embedded metadata is stripped — a security fix

Blender stamps render metadata into PNG `tEXt` chunks. Left enabled, every served
preview embedded:

```
File\0/abs/path/to/the/project.blend      ← absolute server path
Date\02026/09/15 05:40:12                 ← wall-clock timestamp
RenderTime\000:00.24                      ← measured duration
Camera, Scene, Frame, Time
```

The `File` chunk is a real information leak: the artifact is served straight to a
browser, so the absolute path of the design file — and the project filename, which
may be a client's name — travelled with the image. JSON responses are carefully
path-free; the image bytes must be too.

This was found while investigating why two renders of an unchanged scene produced
different checksums. Disabling all stamp flags fixed both problems at once: no leak,
and byte-reproducible output. Regression tests assert the served PNG contains no
path, no hostname, and no `tEXt`/`iTXt`/`zTXt`/`tIME` chunk.

## ArtifactStore and local layout

```
runtime/artifacts/<project_id>/<artifact_id>.png     the bytes
runtime/artifacts/<project_id>/<artifact_id>.json    the metadata sidecar
```

`runtime/` is git-ignored. The root is server-chosen configuration
(`STUDIO_API_ARTIFACT_ROOT`); a request can never influence it.

**Two files, metadata written last.** An artifact is reported as present only when
both exist, so a crash between the two leaves it *absent* and the next attempt
regenerates it — rather than serving a truncated image. Both writes are atomic
(temp file → `fsync` → `os.replace` → directory `fsync`).

### Derived identity

```python
derive_artifact_id(project_id, job_id, artifact_type) -> "preview_<32 hex>"
```

A pure hash of `(version, artifact_type, project_id, job_id)`. That single decision
provides the retry and versioning semantics with no counters and no locks:

- the same completed job always maps to the same artifact → a retry **reuses** it;
- a new request has a new `job_id` → a **new** artifact, so history accrues and an
  earlier preview is never overwritten;
- project-scoped, so the same `job_id` in two projects cannot collide.

## Security boundaries

The store can address **registered artifacts only**, by construction rather than by
filtering:

1. `artifact_id` must match `^(preview)_[a-z0-9]{8,64}$` — a single safe path
   segment, so `..`, separators, and absolute paths are unrepresentable. The same
   pattern is in the canonical schema, so a bad id is rejected on the wire too.
2. The filename is built by the store from that id plus a fixed extension mapped
   from the media type. A caller never supplies a filename, so a `.blend`, an
   execution journal record, a recovery snapshot, an `.env` file, or a directory
   listing cannot be named.
3. Containment is re-checked after `resolve()`, so a swapped symlink cannot escape.
4. `project_id` must be a safe segment and must be in the trusted registry.

There is no listing route and no route that accepts a path. Tests plant a
`.blend`, a journal record, a `.env` containing a token, and a recovery copy *inside*
the artifact directory and assert every attempt to fetch them returns 404 with none
of their content in the response.

The checksum is for corruption detection, test verification, and caching. It is
**not** an access credential — knowing it grants nothing; authorization is always
project scope.

## Retry semantics

| Situation | Behaviour |
|---|---|
| Retry of a completed job | Mutation not repeated. Existing artifact **reused**; no new render. |
| Crash after save, before the result was delivered | Mutation not repeated. Artifact reused if durable. |
| Crash after render, before the artifact became durable | Preview **regenerated** from the already-saved project, writing the same derived id. Safe: rendering cannot change the design. |
| Preview failed earlier, renderer now healthy | Retried on the next delivery, so a degraded preview is not permanent. |
| Same mutation identity under a different `job_id` | The owner's preview is reported; this job performs nothing. |

Reuse trusts the **store**, not the journal: a journal entry written before the bytes
became durable would otherwise advertise a preview that cannot be served.

## Preview failure semantics — option B

**The mutation remains successful and durable even if preview generation fails.**

```
mutate → verify → save → generate preview → record preview → complete
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                      everything here is REPORTING
```

Once `PROJECT_SAVED` is reached the design change is on disk. `_attach_preview`
never raises and never touches `job_status`; a failure is recorded in
`preview_error` while the job stays `succeeded`. Reporting a saved change as failed
because a picture of it could not be produced would be a lie about the user's design.

At the HTTP boundary this is visible and honest:

```json
{
  "job_status": "succeeded",
  "preview": null,
  "preview_error": { "code": "BLENDER_UNAVAILABLE", "message": "…" },
  "chat": {
    "status": "success",
    "summary": "The change has been applied and the project was saved, but a preview image could not be generated."
  }
}
```

`mutation failed` and `preview failed` are therefore distinguishable:
`job_status: "failed"` with `error` set, versus `job_status: "succeeded"` with
`preview_error` set. A failed job never carries a preview — the protocol schema
forbids that combination outright, since it would imply a picture of a change that
was never applied.

## Testing

```bash
# fast: no Blender  (artifact store 50, worker integration 29)
pytest services/preview services/blender-worker/tests/test_worker_preview.py

# fast: HTTP boundary  (51)
pytest services/api/tests/test_artifact_routes.py

# real Blender rendering  (11, opt-in)
pytest -m blender tests/blender/test_preview_blender.py

# full HTTP → agent → worker → Blender → PNG  (8, opt-in)
pytest -m blender tests/e2e/test_api_blender_e2e.py
```

The real-Blender tests assert valid PNG signatures, correct encoded dimensions
parsed from the IHDR chunk, non-empty output, and that **checksums differ when the
visible scene differs**. They deliberately do **not** compare against a golden
image: Blender versions, drivers, and colour-management defaults shift individual
pixels, so a reference-image test would fail for reasons unrelated to this code.

## Future replacement

| Now | Later |
|---|---|
| `LocalArtifactStore` on a shared local directory | S3 / object storage implementing `ArtifactStore`; the worker uploads, the API issues signed URLs or proxies |
| `BLENDER_WORKBENCH` still image | quick Eevee preview, then final Cycles render with RTX — each a new `PreviewGenerator` |
| PNG only | `glb_scene` artifact type for interactive Three.js preview (Task 11+) |
| Fixed camera at the origin | scene-aware framing from a bounding box; multiple cameras |
| Preview per job | preview per project *version*, once version history exists |
| One artifact type | `render_image`, `glb_scene`, `viewport_stream` — each needs its own media-type handling and its own decision about who may request it |
| Control plane and worker share a filesystem | separate machines; the shared directory disappears with object storage |

`artifact-type.schema.json` is a closed enum containing only `preview_image`, so
adding a type is a deliberate contract change in both languages rather than an
accident.

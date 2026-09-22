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
the user would find objects they never asked for. The same applies to the temporary
sun and sky world described under *Lighting*.

### Framing is computed from the scene, and quantised

The camera used to sit at a fixed `(7, -7, 5)` looking at the origin. That framed the
seed fixture and nothing else: a house, or anything not built on the origin, fell
outside the frame.

It is now derived from the scene's renderable bounding box — but **not** from the
exact box, and that distinction is load-bearing:

| | behaviour | consequence |
|---|---|---|
| fixed viewpoint | camera never moves | a real building is out of frame |
| exact bounding box | camera follows the geometry | moving the *only* object produces an **identical** picture, so the preview reports "nothing changed" about the one thing that did |
| **quantised box** | camera moves in coarse steps | both properties hold |

So the radius is snapped up onto a ~25% ladder, and the centre is snapped to a grid
of half that radius. A small move leaves the framing bit-identical and therefore
shows up as movement inside a stable frame; a design that genuinely outgrows the
frame crosses a boundary and is re-framed. The camera behaves like a tripod that is
only occasionally repositioned.

Snapping lets the subject sit off-centre, so the margin has to absorb it. The bound
is exact rather than hopeful:

```
grid            g = 0.5 R
max axis error  g / 2             = 0.25 R
max 3D drift    sqrt(3) · 0.25 R  ≈ 0.433 R
max extent      r + drift        <= 1.433 R     (r <= R by construction)
```

which is why `CAMERA_MARGIN` is 1.5. The framed radius is deliberately *not* grown by
the measured drift: that would make zoom a function of position, reintroducing the
wobble the quantisation removes.

The look-at rotation is still derived from the direction vector, so there are no magic
Euler angles, and the view is still off-axis on X so movement along world +X appears
as a clear horizontal displacement — the original point of the Spec 001 preview,
verified by `test_preview_before_and_after_the_move_differ_visibly`.

## Render settings

EEVEE, fixed small resolution (default 640×360; tests use 320×180), sky-based
environment lighting, AgX filmic colour management.

| Choice | Reason |
|---|---|
| `BLENDER_EEVEE` | Renders real materials, textures, shadows and raytraced reflections. Workbench is a solid-shading rasteriser that ignores materials by design, so it could never answer "does this look real?". On Blender 5.2 the identifier is `BLENDER_EEVEE` — the "Next" rewrite became the default and the `_NEXT` suffix was dropped. |
| Raytracing enabled | Without it EEVEE has no reflections and only a crude ambient term, so glass, polished floors and interior corners all look wrong. |
| Sky Texture world | Real image-based lighting — soft directional light plus sky-coloured bounce — with no HDRI asset to ship. |
| Sun matched to the sky | Elevation and azimuth are shared, so shadows fall away from the bright part of the sky instead of contradicting it. A ~1.5° sun disc gives shadows a soft edge, which is most of what stops a render looking synthetic. |
| AgX view transform, set explicitly | Rolls highlights off like film instead of clipping to flat white. Pinned rather than inherited so one design does not look different on two machines. |
| `use_taa_reprojection = False`, pinned samples | Reusing samples across frames would make a still render depend on history. Disabling it makes the **pixels** reproducible. |
| No motion blur | A still preview has no time dimension, and it is a source of per-run variation. |

### Determinism is asserted on pixels, not on bytes

Workbench was byte-reproducible; EEVEE is not, and the tests were changed to ask the
right question rather than to look the other way.

Measured on the pinned Blender: two renders of one unchanged scene are **identical to
0.0 mean absolute pixel difference**, while their PNG *files* differ — PNG compression
is not required to be bit-stable. So stability is asserted on decoded pixels
(`test_the_same_scene_renders_to_the_same_pixels`), paired with a sensitivity test
(`test_a_changed_scene_renders_to_visibly_different_pixels`) because a renderer could
otherwise pass a stability check by emitting a constant image.

Cost: the first render in a process pays shader compilation (~10 s observed); later
renders take a fraction of a second. EEVEE also needs more GL capability than
Workbench — verified working headless with no GPU configured, and a preview failure is
already a value that can never fail a mutation, so the worst case is "no picture".

## Lighting

Lighting is **supplemented, never overridden**. Astra can author its own lights, and a
preview that replaced them would hide the very thing the user asked for.

| Condition | Action |
|---|---|
| scene has no world, or a world that emits nothing | add a temporary Sky Texture world |
| scene has no lights of its own | full sun, fill and ambient — something has to make the geometry visible |
| scene lights itself | the same rig at reduced strength, so the scene's own fittings lead |

The "emits nothing" test matters: a world that exists but is black lights nothing, and
treating its presence as the user's choice is how a scene renders pitch dark. Every
addition is in-memory only and is reported in the `scene` phase payload
(`lighting.added_sun`, `lighting.added_sky_world`, `lighting.scene_lights_itself`).

### Stepping back, not getting out of the way

Ambient bright enough to make an unlit scene legible is also bright enough to erase real
interior lighting: pools of light need somewhere darker to be brighter than, and warm
fittings are diluted by white fill from every direction. That is why an interior used to
read as a diagram no matter how good its materials were.

How far to step back is a compromise, measured on two real scenes that pull in opposite
directions:

| sun / ambient | a closed lobby | a part-lit clinic |
|---|---|---|
| 0.8 / 0.15 | warm, dramatic, right | whole building nearly black |
| 3.0 / 0.55 | flat, washed out | correct daylight |
| **1.9 / 0.30** | warmth survives | readable, reception glows |

A sun is therefore always added and only its strength varies. Worth knowing: a **ceiling
does not rescue this**. It seems as though geometry should separate interior from
exterior by itself, but a doll's-house view is open to the camera by definition, so
daylight arrives through the opening regardless — which is also why Astra is told not to
roof a room it wants seen.

**The honest limit.** A genuinely photographic interior needs a camera *inside* the room,
with no daylight in frame at all. The preview has one fixed aerial viewpoint, so the
table above is the best a single frame can do for both at once. An interior view is its
own feature, not a tuning problem.

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
| EEVEE still image | final Cycles render with RTX — a new `PreviewGenerator` |
| PNG only | `glb_scene` artifact type for interactive Three.js preview (Task 11+) |
| One quantised three-quarter camera | multiple cameras; interior views; composition that understands rooms rather than bounding boxes |
| Preview per job | preview per project *version*, once version history exists |
| One artifact type | `render_image`, `glb_scene`, `viewport_stream` — each needs its own media-type handling and its own decision about who may request it |
| Control plane and worker share a filesystem | separate machines; the shared directory disappears with object storage |

`artifact-type.schema.json` is a closed enum containing only `preview_image`, so
adding a type is a deliberate contract change in both languages rather than an
accident.

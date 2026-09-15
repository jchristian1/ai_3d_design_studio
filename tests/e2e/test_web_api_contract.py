"""The browser's API client against the REAL control plane (Spec 001, Task 11).

OPT-IN: marked ``blender``, because the full path runs real Blender.

    pytest -m blender tests/e2e/test_web_api_contract.py -v

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
The frontend tests in ``apps/web/tests`` drive the real components against a STUB
transport: they prove the UI behaves correctly given a response shape. This test
proves the other half — that the response shapes the UI is built on are the ones the
real API actually produces:

    apps/web/lib/api  --(real HTTP)-->  FastAPI  -->  worker  -->  Blender  -->  PNG

It runs the browser's own TypeScript client in Node against a live uvicorn + a live
worker, so a contract drift between the API and the UI fails here rather than in a
browser.

It is deliberately NOT a browser automation test. Full browser automation with a real
Blender render would mean adding Playwright plus a browser download to the
repository, and it would verify the same HTTP conversation this test already
verifies, plus rendering that the component tests already cover. The genuinely
un-automated remainder — that the assembled page looks right and feels right — is
covered by the documented manual acceptance scenario in ``apps/web/README.md``, and
is honestly labelled as manual rather than claimed as automated.

CORS is exercised too, because the browser's request really is cross-origin: the
client sends an ``Origin`` header and the response must carry the matching
``Access-Control-Allow-Origin``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from blender_mcp.blender_runtime import find_blender_executable  # noqa: E402
from studio_contracts import worker_protocol as protocol  # noqa: E402
from studio_fixtures.slice_stack import build_slice_stack  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "apps" / "web"

TOKEN = "web-contract-token-not-committed"
WORKER_ID = "worker_web_contract"
PROJECT_ID = "proj_seed"
MOVE_COMMAND = "Move Cube 50 cm to the right."

#: The origin the Next.js dev server runs on. The API must allow it explicitly.
BROWSER_ORIGIN = "http://localhost:3000"

pytestmark = pytest.mark.blender


@pytest.fixture
def live_stack(tmp_path: Path):
    """A real control plane and a real Blender worker.

    Assembled by the shared ``studio_fixtures.slice_stack`` builder so this file
    contains assertions only — there is one place that wires the local slice
    together, not one per test module.
    """
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    if shutil.which("node") is None:
        pytest.skip("node not available")

    with build_slice_stack(
        tmp_path,
        token=TOKEN,
        worker_id=WORKER_ID,
        # Exactly what local browser development uses.
        allowed_origins=(BROWSER_ORIGIN, "http://127.0.0.1:3000"),
    ) as stack:
        yield {
            "server": stack.server,
            "http": stack.http,
            "worker": stack.worker,
            "project_path": stack.project_path,
            "stack": stack,
        }


def run_web_client(script: str, base_url: str, timeout: int = 600) -> dict:
    """Execute a snippet against the browser's real TypeScript API client.

    Run through the repository's own client module, not a re-implementation, so the
    thing under test is the code the browser ships.
    """
    program = textwrap.dedent(
        f"""
        import {{ createApiClient }} from "./lib/api/client.ts";

        const client = createApiClient({{ baseUrl: {json.dumps(base_url)} }});
        const out = {{}};
        {script}
        process.stdout.write("RESULT_JSON:" + JSON.stringify(out) + "\\n");
        """
    )
    script_path = WEB_ROOT / ".web-contract-probe.mts"
    script_path.write_text(program, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", "--import", "tsx", str(script_path)],
            cwd=WEB_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        script_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise AssertionError(
            f"the web client failed (exit {proc.returncode}).\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        )

    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:") :])
    raise AssertionError(f"no result from the web client.\nstdout:\n{proc.stdout}")


# ---------------------------------------------------------------------------
# The full path, through the browser's own client
# ---------------------------------------------------------------------------


def test_the_web_api_client_drives_a_real_change_to_a_real_preview(live_stack):
    """apps/web's client -> FastAPI -> worker -> Blender -> PNG the browser can show.

    The worker's inbound loop is pumped from this thread, which is what the
    standalone `python -m blender_worker` process does in real use.
    """
    http = live_stack["http"]
    worker = live_stack["worker"]

    # The browser's own client submits and polls, exactly as the UI does.
    result = run_web_client(
        """
        out.health = await client.getHealth();
        out.latestBefore = await client.getLatestPreview("proj_seed");
        out.submission = await client.submitChat({
          request_id: "req_web_001",
          project_id: "proj_seed",
          session_id: "sess_web",
          message: "Move Cube 50 cm to the right.",
        });
        """,
        live_stack["server"].base_url,
    )

    # Shapes the UI depends on are present and correctly typed.
    assert result["health"]["api"] == "healthy"
    assert result["health"]["blender_capable_workers"] == 1
    assert result["latestBefore"] is None, "no preview exists before any change"

    submission = result["submission"]
    assert submission["job_status"] == "queued"
    assert submission["duplicate"] is False
    job_id = submission["job_id"]
    assert submission["status_url"] == f"/api/projects/{PROJECT_ID}/jobs/{job_id}"

    # Let the worker do the real work.
    assert _handle_until(worker, protocol.JOB_OFFER), "the offer never arrived"
    _await_status(http, job_id, "succeeded")

    # Now the client reads the terminal state and the preview, as the UI does.
    followup = run_web_client(
        f"""
        out.status = await client.getJobStatus("proj_seed", {json.dumps(job_id)});
        out.latestAfter = await client.getLatestPreview("proj_seed");
        out.artifactUrl = client.getArtifactUrl(out.status.preview.url);
        const image = await fetch(out.artifactUrl);
        out.imageStatus = image.status;
        out.imageType = image.headers.get("content-type");
        const bytes = new Uint8Array(await image.arrayBuffer());
        out.imageBytes = bytes.length;
        out.isPng =
          bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47;
        """,
        live_stack["server"].base_url,
    )

    status = followup["status"]
    assert status["job_status"] == "succeeded"
    assert status["preview_error"] is None

    preview = status["preview"]
    assert preview["media_type"] == "image/png"
    assert (preview["width"], preview["height"]) == (320, 180)
    assert preview["url"].startswith(f"/api/projects/{PROJECT_ID}/artifacts/")

    # The URL the UI builds resolves to real PNG bytes.
    assert followup["artifactUrl"] == (
        f"{live_stack['server'].base_url}{preview['url']}"
    )
    assert followup["imageStatus"] == 200
    assert followup["imageType"] == "image/png"
    assert followup["isPng"] is True, "the browser would not receive a PNG"
    assert followup["imageBytes"] == preview["size_bytes"]

    # And the page-open path now finds that preview.
    assert followup["latestAfter"]["artifact_id"] == preview["artifact_id"]

    # Nothing the browser receives names a file on this machine.
    serialized = json.dumps(followup["status"])
    for leaked in (str(live_stack["project_path"]), ".blend", "/tmp/", "/home/"):
        assert leaked not in serialized, f"the API sent the browser {leaked!r}"


def test_a_friendly_error_reaches_the_browser_for_an_unsupported_instruction(
    live_stack,
):
    """The UI's error translation keys on codes the API really sends."""
    result = run_web_client(
        """
        try {
          await client.submitChat({
            request_id: "req_web_bad",
            project_id: "proj_seed",
            session_id: "sess_web",
            message: "Make the kitchen look nicer.",
          });
          out.threw = false;
        } catch (error) {
          out.threw = true;
          out.kind = error.kind;
          out.code = error.code;
          out.message = error.message;
        }
        """,
        live_stack["server"].base_url,
    )

    assert result["threw"] is True
    assert result["code"] == "UNSUPPORTED_INSTRUCTION"
    assert result["kind"] == "unsupported"
    # What the user would actually read.
    assert "isn't supported yet" in result["message"]


def test_an_unknown_project_is_reported_as_not_found(live_stack):
    result = run_web_client(
        """
        try {
          await client.getJobStatus("proj_not_registered", "job_whatever");
          out.threw = false;
        } catch (error) {
          out.threw = true;
          out.kind = error.kind;
          out.message = error.message;
        }
        """,
        live_stack["server"].base_url,
    )

    assert result["threw"] is True
    assert result["kind"] == "not_found"
    assert "could not be found" in result["message"]


def test_the_browser_origin_is_allowed_by_cors(live_stack):
    """A cross-origin browser request must be permitted, and only from listed origins."""
    http = live_stack["http"]

    allowed = http.get("/health", headers={"Origin": BROWSER_ORIGIN})
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == BROWSER_ORIGIN

    # The preflight a browser sends before POSTing JSON.
    preflight = http.request(
        "OPTIONS",
        "/api/chat",
        headers={
            "Origin": BROWSER_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code in (200, 204)
    assert preflight.headers.get("access-control-allow-origin") == BROWSER_ORIGIN

    # An unlisted origin is not granted access.
    denied = http.get("/health", headers={"Origin": "https://evil.example.com"})
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_no_response_the_browser_receives_contains_a_secret(live_stack):
    http = live_stack["http"]

    for path in ("/health", "/api/workers", "/openapi.json"):
        text = http.get(path, headers={"Origin": BROWSER_ORIGIN}).text
        for leaked in (TOKEN, "STUDIO_WORKER_TOKEN", ".blend", "/snap/bin/blender"):
            assert leaked not in text, f"{path} would send the browser {leaked!r}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _handle_until(client, expected: str, deadline_seconds: float = 300.0) -> bool:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if client.handle_next(timeout=1.0) == expected:
            return True
    return False


def _await_status(http, job_id: str, expected: str, deadline_seconds: float = 300.0):
    end = time.monotonic() + deadline_seconds
    last = None
    while time.monotonic() < end:
        response = http.get(f"/api/projects/{PROJECT_ID}/jobs/{job_id}")
        if response.status_code == 200:
            last = response.json()
            if last["job_status"] == expected:
                return last
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {expected!r}; last: {last}")

"""Cross-language parity for the AUTHORITATIVE scene-version digest.

Spec 002, Task 1.

``scene_version`` is an ENFORCED execution precondition from Task 5 onward: a
mutation is refused when the scene it was reasoned against no longer matches. That
only works if every producer and every consumer computes the same digest, so this
test executes the TypeScript implementation in Node, the Python implementation
in-process, and asserts the two agree on:

  - the digest version constant,
  - the fixed-decimal formatting of every numeric vector,
  - which numeric values are refused,
  - the canonical JSON BYTES of every scene projection,
  - the resulting hex digest,
  - the object sort keys,
  - which scenes are refused as ambiguous or malformed.

Byte-level agreement on the canonical JSON is asserted deliberately rather than
only agreement on the digest: if the two ever diverge, the JSON tells you exactly
where (a quantum, a key order, a non-ASCII escape), whereas two different hashes
tell you nothing.

The test skips only if Node is unavailable, so the Python suite stays runnable in
a Blender-only or Python-only environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from studio_contracts.scene import (
    SCENE_DIGEST_VERSION,
    SceneDigestError,
    canonical_digest_json,
    compute_scene_version,
    format_digest_number,
    object_sort_key,
    scene_digest_projection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "packages" / "contracts"
EMIT_SCRIPT = CONTRACTS / "scripts" / "emit-scene-verdicts.ts"
CASES_PATH = CONTRACTS / "scene-cases.json"

CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _python_verdicts() -> list[dict]:
    verdicts: list[dict] = [
        {
            "group": "digest_version",
            "case": "constant",
            "value": SCENE_DIGEST_VERSION,
        }
    ]

    for case in CASES["numbers"]["cases"]:
        verdicts.append(
            {
                "group": "number",
                "case": case["name"],
                "formatted": format_digest_number(case["value"]),
            }
        )

    for case in CASES["numbers"]["rejected"]:
        threw = False
        try:
            format_digest_number(case["value"])
        except SceneDigestError:
            threw = True
        verdicts.append(
            {"group": "number_rejected", "case": case["name"], "threw": threw}
        )

    for case in CASES["scenes"]:
        scene = case["scene"]
        verdicts.append(
            {
                "group": "scene",
                "case": case["name"],
                "canonical_json": canonical_digest_json(
                    scene_digest_projection(scene)
                ),
                "scene_version": compute_scene_version(scene),
                # Emitted as lists so the JSON shape matches the TypeScript side;
                # Python tuples would serialise the same but compare unequal.
                "sort_keys": [list(object_sort_key(o)) for o in scene["objects"]],
            }
        )

    for case in CASES["rejected_scenes"]:
        threw = False
        try:
            compute_scene_version(case["scene"])
        except SceneDigestError:
            threw = True
        verdicts.append(
            {"group": "scene_rejected", "case": case["name"], "threw": threw}
        )

    verdicts.sort(key=lambda v: f"{v['group']}::{v['case']}")
    return verdicts


def _typescript_verdicts() -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; skipping cross-language parity check")
    proc = subprocess.run(
        [node, str(EMIT_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"node scene verdict script failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_typescript_and_python_compute_identical_scene_digests():
    ts = _typescript_verdicts()
    py = _python_verdicts()

    assert len(ts) == len(py), (
        f"verdict count differs: TypeScript {len(ts)} vs Python {len(py)}"
    )

    mismatches = [{"typescript": t, "python": p} for t, p in zip(ts, py) if t != p]
    assert not mismatches, (
        "TypeScript and Python disagree about the scene-version digest:\n"
        + json.dumps(mismatches, indent=2)
    )


def test_canonical_json_bytes_are_identical_for_every_scene():
    """Stated separately so a divergence names the scene and shows both strings."""
    ts = {v["case"]: v for v in _typescript_verdicts() if v["group"] == "scene"}
    py = {v["case"]: v for v in _python_verdicts() if v["group"] == "scene"}
    assert set(ts) == set(py)
    for name in sorted(ts):
        assert ts[name]["canonical_json"] == py[name]["canonical_json"], (
            f"canonical JSON differs for {name}:\n"
            f"  typescript: {ts[name]['canonical_json']}\n"
            f"  python:     {py[name]['canonical_json']}"
        )


def test_every_rejected_vector_is_rejected_in_both_languages():
    for verdict in _python_verdicts():
        if verdict["group"] in ("number_rejected", "scene_rejected"):
            assert verdict["threw"] is True, (
                f"{verdict['group']}::{verdict['case']} was accepted by Python"
            )
    for verdict in _typescript_verdicts():
        if verdict["group"] in ("number_rejected", "scene_rejected"):
            assert verdict["threw"] is True, (
                f"{verdict['group']}::{verdict['case']} was accepted by TypeScript"
            )

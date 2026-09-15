"""Cross-language contract parity.

This is the strongest drift guard in the repo: it executes the TypeScript
representation in Node, executes the Python representation in-process, and
asserts both reach IDENTICAL verdicts for every case in the shared canonical
corpus (packages/contracts/schemas/conformance-cases.json).

If a contract is changed in only one language, this test fails.

The test skips (rather than fails) only if Node is unavailable, so the Python
suite remains runnable in a Blender-only or Python-only environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from studio_contracts import SCHEMA_DIR, SCHEMA_FILES, validate_against_schema
from studio_validation import validate_chat_request

REPO_ROOT = Path(__file__).resolve().parents[2]
EMIT_SCRIPT = REPO_ROOT / "packages" / "contracts" / "scripts" / "emit-verdicts.ts"


def _python_verdicts() -> list[dict]:
    with (SCHEMA_DIR / "conformance-cases.json").open("r", encoding="utf-8") as fh:
        corpus = json.load(fh)

    verdicts: list[dict] = []
    for suite in corpus["suites"]:
        for expected in ("valid", "invalid"):
            for case in suite[expected]:
                data = case["data"]
                verdicts.append(
                    {
                        "schema": suite["schema"],
                        "case": case["name"],
                        "expected": expected,
                        "schema_valid": validate_against_schema(
                            suite["schema"], data
                        ).valid,
                        "validation_valid": (
                            validate_chat_request(data).valid
                            if suite["schema"] == SCHEMA_FILES["ChatRequest"]
                            else None
                        ),
                    }
                )
    verdicts.sort(key=lambda v: f"{v['schema']}::{v['case']}")
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
        pytest.fail(f"node verdict script failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_typescript_and_python_reach_identical_verdicts():
    ts = _typescript_verdicts()
    py = _python_verdicts()

    assert len(ts) == len(py), (
        f"case count differs: TypeScript {len(ts)} vs Python {len(py)}"
    )

    mismatches = [
        {"typescript": t, "python": p} for t, p in zip(ts, py) if t != p
    ]
    assert not mismatches, (
        "TypeScript and Python disagree about the canonical contract:\n"
        + json.dumps(mismatches, indent=2)
    )


def test_every_corpus_case_matches_its_declared_expectation():
    """The corpus itself must be self-consistent against the canonical schemas."""
    for verdict in _python_verdicts():
        expected_valid = verdict["expected"] == "valid"
        assert verdict["schema_valid"] is expected_valid, (
            f"{verdict['schema']}::{verdict['case']} expected "
            f"{verdict['expected']} but schema said "
            f"{'valid' if verdict['schema_valid'] else 'invalid'}"
        )

"""Cross-language parity for the job layer (Spec 001, Task 3).

Executes the TypeScript job layer in Node, executes the Python job layer
in-process, and asserts both agree on:

  - canonical encodings (byte-identical strings)
  - derived idempotency keys (identical SHA-256 digests)
  - lifecycle transition rules
  - job validation verdicts and the error codes produced

If the job contract is changed in only one language, this test fails.

The test skips (rather than fails) only if Node is unavailable, so the Python
suite remains runnable in a Blender-only or Python-only environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from studio_contracts import SCHEMA_DIR, SCHEMA_FILES
from studio_contracts.jobs import (
    can_transition,
    canonicalize,
    classify_delivery,
    derive_content_fingerprint,
    derive_idempotency_key,
    validate_job,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EMIT_SCRIPT = (
    REPO_ROOT / "packages" / "contracts" / "scripts" / "emit-job-verdicts.ts"
)

JOB_CASES = json.loads(
    (REPO_ROOT / "packages" / "contracts" / "job-cases.json").read_text("utf-8")
)
CONFORMANCE = json.loads(
    (SCHEMA_DIR / "conformance-cases.json").read_text("utf-8")
)


def _derive(base, **overrides):
    merged = {**base, **overrides}
    return derive_idempotency_key(
        project_id=merged["project_id"],
        request_id=merged["request_id"],
        operation_index=merged["operation_index"],
    )


def _python_verdicts() -> list[dict]:
    verdicts: list[dict] = []

    for case in JOB_CASES["canonicalize"]["cases"]:
        verdicts.append(
            {
                "group": "canonicalize",
                "case": case["name"],
                "encoded": canonicalize(case["value"]),
            }
        )

    for case in JOB_CASES["canonicalize"]["rejected"]:
        value = {
            "__NAN__": float("nan"),
            "__INF__": float("inf"),
            "__NEG_INF__": float("-inf"),
        }[case["value"]]
        threw = False
        try:
            canonicalize(value)
        except ValueError:
            threw = True
        verdicts.append(
            {"group": "canonicalize-rejected", "case": case["name"], "threw": threw}
        )

    base = JOB_CASES["idempotency"]["base"]
    verdicts.append({"group": "idempotency", "case": "base", "key": _derive(base)})
    for variant in JOB_CASES["idempotency"]["different_key_variants"]:
        verdicts.append(
            {
                "group": "idempotency",
                "case": variant["name"],
                "key": _derive(base, **{variant["field"]: variant["value"]}),
            }
        )
    for variant in JOB_CASES["idempotency"]["rejected"]:
        threw = False
        try:
            _derive(base, **{variant["field"]: variant["value"]})
        except ValueError:
            threw = True
        verdicts.append(
            {"group": "idempotency-rejected", "case": variant["name"], "threw": threw}
        )

    fp = JOB_CASES["content_fingerprint"]
    verdicts.append(
        {
            "group": "fingerprint",
            "case": "base",
            "fingerprint": derive_content_fingerprint(fp["job_type"], fp["payload"]),
        }
    )
    for variant in fp["different_payloads"] + fp["equivalent_payloads"]:
        verdicts.append(
            {
                "group": "fingerprint",
                "case": variant["name"],
                "fingerprint": derive_content_fingerprint(
                    fp["job_type"], variant["payload"]
                ),
            }
        )

    for case in JOB_CASES["delivery"]["cases"]:
        verdicts.append(
            {
                "group": "delivery",
                "case": case["name"],
                "decision": classify_delivery(case["recorded_status"]),
            }
        )

    for transition in (
        JOB_CASES["transitions"]["allowed"] + JOB_CASES["transitions"]["rejected"]
    ):
        verdicts.append(
            {
                "group": "transition",
                "case": f"{transition['from']}->{transition['to']}",
                "allowed": can_transition(transition["from"], transition["to"]),
            }
        )

    job_suite = next(
        s for s in CONFORMANCE["suites"] if s["schema"] == SCHEMA_FILES["Job"]
    )
    for kind in ("valid", "invalid"):
        for case in job_suite[kind]:
            result = validate_job(case["data"])
            verdicts.append(
                {
                    "group": f"validate-{kind}",
                    "case": case["name"],
                    "valid": result.valid,
                    "codes": sorted({e.code for e in result.errors}),
                }
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
        pytest.fail(f"node verdict script failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_typescript_and_python_agree_on_the_job_contract():
    ts = _typescript_verdicts()
    py = _python_verdicts()

    assert len(ts) == len(py), (
        f"case count differs: TypeScript {len(ts)} vs Python {len(py)}"
    )

    mismatches = [{"typescript": t, "python": p} for t, p in zip(ts, py) if t != p]
    assert not mismatches, (
        "TypeScript and Python disagree about the job contract:\n"
        + json.dumps(mismatches, indent=2)
    )


def test_idempotency_keys_are_stable_values_not_just_equal():
    """Guards against both languages drifting together to a new key."""
    base = JOB_CASES["idempotency"]["base"]
    assert _derive(base) == JOB_CASES["idempotency"]["expected_key"]


def test_content_fingerprint_is_a_stable_value():
    fp = JOB_CASES["content_fingerprint"]
    assert (
        derive_content_fingerprint(fp["job_type"], fp["payload"])
        == fp["expected_fingerprint"]
    )


def test_content_fingerprint_is_not_the_mutation_identity():
    """Identical content from two requests must not collapse into one mutation."""
    fp = JOB_CASES["content_fingerprint"]
    same_content = derive_content_fingerprint(fp["job_type"], fp["payload"])
    assert same_content == derive_content_fingerprint(fp["job_type"], fp["payload"])
    assert derive_idempotency_key("proj_1", "req_A", 0) != derive_idempotency_key(
        "proj_1", "req_B", 0
    )


def test_job_conformance_corpus_is_self_consistent():
    """Every declared job expectation must hold in the Python validator."""
    job_suite = next(
        s for s in CONFORMANCE["suites"] if s["schema"] == SCHEMA_FILES["Job"]
    )
    for case in job_suite["valid"]:
        assert validate_job(case["data"]).valid is True, case["name"]
    for case in job_suite["invalid"]:
        assert validate_job(case["data"]).valid is False, case["name"]

"""Cross-language parity for the spatial utilities (Spec 001, Task 2).

Executes the TypeScript representation in Node, executes the Python
representation in-process, and asserts both produce IDENTICAL results for every
case in the shared corpus (packages/spatial/spatial-cases.json) — including exact
float values, error codes, and error messages.

If unit conversion or direction mapping is changed in only one language, this
test fails.

Floats are compared via their exact 17-significant-digit encoding, so a
one-bit difference is caught rather than hidden by decimal rounding.

The test skips (rather than fails) only if Node is unavailable, so the Python
suite remains runnable in a Blender-only or Python-only environment.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest
from spatial_test_support import decode_value, load_spatial_cases
from studio_spatial import direction_delta_meters, resolve_direction, to_meters

REPO_ROOT = Path(__file__).resolve().parents[2]
EMIT_SCRIPT = REPO_ROOT / "packages" / "spatial" / "scripts" / "emit-verdicts.ts"

CASES = load_spatial_cases()


def _normalize_number(value: float) -> str:
    """Normalize a float to a canonical exact form for cross-language compare.

    Both sides are reduced to Python's float repr of the same bits: the TS side
    emits toPrecision(17), which round-trips exactly, and here it is parsed back
    to a float. Comparing the repr of parsed floats therefore compares the
    underlying bits without reimplementing JavaScript's formatting rules.
    """
    if math.isnan(value):
        return "NaN"
    if value == math.inf:
        return "Infinity"
    if value == -math.inf:
        return "-Infinity"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    return repr(float(value))


def _normalize_encoded(encoded: str) -> str:
    """Normalize a TS-emitted number string the same way."""
    if encoded in ("NaN", "Infinity", "-Infinity", "-0"):
        return encoded
    return _normalize_number(float(encoded))


def _python_verdicts() -> list[dict]:
    verdicts: list[dict] = []

    for case in CASES["conversion"]["valid"] + CASES["conversion"]["invalid"]:
        result = to_meters(
            {"value": decode_value(case.get("value")), "unit": case["unit"]}
        )
        entry = {"group": "conversion", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["meters"] = _normalize_number(result.meters)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in CASES["conversion"]["malformed"]:
        result = to_meters(decode_value(case["measurement"]))
        entry = {
            "group": "conversion-malformed",
            "case": case["name"],
            "ok": result.ok,
        }
        if result.ok:
            entry["meters"] = _normalize_number(result.meters)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in CASES["directions"]["valid"] + CASES["directions"]["invalid"]:
        result = resolve_direction(decode_value(case["direction"]))
        entry = {"group": "direction", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["axis"] = result.axis_direction.axis
            entry["sign"] = result.axis_direction.sign
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in CASES["deltas"]["valid"] + CASES["deltas"]["invalid"]:
        result = direction_delta_meters(
            case["direction"],
            {"value": decode_value(case.get("value")), "unit": case["unit"]},
        )
        entry = {"group": "delta", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["axis"] = result.axis
            entry["meters"] = _normalize_number(result.meters)
            entry["delta"] = {
                "x": _normalize_number(result.delta.x),
                "y": _normalize_number(result.delta.y),
                "z": _normalize_number(result.delta.z),
            }
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

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

    raw = json.loads(proc.stdout)
    # Normalize the TS float encoding into the same canonical form.
    for entry in raw:
        if "meters" in entry:
            entry["meters"] = _normalize_encoded(entry["meters"])
        if "delta" in entry:
            entry["delta"] = {
                axis: _normalize_encoded(v) for axis, v in entry["delta"].items()
            }
    return raw


def test_typescript_and_python_produce_identical_spatial_results():
    ts = _typescript_verdicts()
    py = _python_verdicts()

    assert len(ts) == len(py), (
        f"case count differs: TypeScript {len(ts)} vs Python {len(py)}"
    )

    mismatches = [
        {"typescript": t, "python": p} for t, p in zip(ts, py) if t != p
    ]
    assert not mismatches, (
        "TypeScript and Python disagree about spatial behaviour:\n"
        + json.dumps(mismatches, indent=2)
    )


def test_corpus_expectations_are_self_consistent():
    """Every declared expectation in the corpus must hold in Python."""
    for case in CASES["conversion"]["valid"]:
        result = to_meters(
            {"value": decode_value(case.get("value")), "unit": case["unit"]}
        )
        assert result.ok, f"{case['name']}: expected success"
        assert result.meters == case["meters"], case["name"]

    for case in CASES["directions"]["valid"]:
        result = resolve_direction(case["direction"])
        assert result.ok, case["name"]
        assert result.axis_direction.axis == case["axis"], case["name"]
        assert result.axis_direction.sign == case["sign"], case["name"]

    for case in CASES["deltas"]["valid"]:
        result = direction_delta_meters(
            case["direction"],
            {"value": decode_value(case.get("value")), "unit": case["unit"]},
        )
        assert result.ok, case["name"]
        assert result.meters == case["meters"], case["name"]

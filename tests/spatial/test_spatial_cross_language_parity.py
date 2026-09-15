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
from spatial_test_support import decode_color, decode_value, load_spatial_cases
from studio_spatial import (
    direction_delta_meters,
    resize_factor,
    resolve_direction,
    resolve_named_color,
    srgb_encoded_channel_to_linear,
    to_meters,
    to_radians,
    validate_material_color,
    validate_size_factor,
)

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

    # --- Spec 002 Task 2: angles ------------------------------------------
    #
    # Angle conversion is one multiplication by a single constant, so parity is
    # asserted on the EXACT bits, exactly like length conversion.
    for case in CASES["angles"]["valid"] + CASES["angles"]["invalid"]:
        result = to_radians(
            {"value": decode_value(case.get("value")), "unit": case["unit"]}
        )
        entry = {"group": "angle", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["radians"] = _normalize_number(result.radians)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in CASES["angles"]["malformed"]:
        result = to_radians(decode_value(case["measurement"]))
        entry = {"group": "angle-malformed", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["radians"] = _normalize_number(result.radians)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    # --- Spec 002 Task 2: resize ------------------------------------------
    for case in CASES["resize"]["valid"] + CASES["resize"]["invalid"]:
        result = resize_factor(decode_value(case.get("percent")), case["direction"])
        entry = {"group": "resize", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["factor"] = _normalize_number(result.factor)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in (
        CASES["resize"]["factors"]["valid"] + CASES["resize"]["factors"]["invalid"]
    ):
        result = validate_size_factor(decode_value(case["value"]))
        entry = {"group": "size-factor", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["factor"] = _normalize_number(result.factor)
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    # --- Spec 002 Task 2: colour ------------------------------------------
    #
    # Validation verdicts and PALETTE values are compared exactly: the palette is
    # authored in canonical linear literals, so no `pow` is involved.
    for case in CASES["colors"]["valid"] + CASES["colors"]["invalid"]:
        result = validate_material_color(decode_color(case["color"]))
        entry = {"group": "color", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["color"] = {
                channel: _normalize_number(getattr(result.color, channel))
                for channel in ("r", "g", "b", "a")
            }
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    for case in CASES["colors"]["named"] + CASES["colors"]["unnamed"]:
        result = resolve_named_color(decode_value(case["input"]))
        entry = {"group": "named-color", "case": case["name"], "ok": result.ok}
        if result.ok:
            entry["color"] = {
                channel: _normalize_number(getattr(result.color, channel))
                for channel in ("r", "g", "b", "a")
            }
        else:
            entry["error_code"] = result.error.code
            entry["error_message"] = result.error.message
        verdicts.append(entry)

    # Transfer-function output goes through `pow`, which is not guaranteed
    # bit-identical across libm implementations, so it is compared within the
    # corpus tolerance by test_srgb_transfer_agrees_within_tolerance rather than
    # bit-for-bit here. Only the accept/reject verdict is compared exactly.
    for case in CASES["colors"]["transfer"]["cases"]:
        verdicts.append(
            {
                "group": "srgb-transfer",
                "case": case["name"],
                "ok": srgb_encoded_channel_to_linear(case["encoded"]) is not None,
            }
        )

    for case in CASES["colors"]["transfer"]["rejected"]:
        verdicts.append(
            {
                "group": "srgb-transfer-rejected",
                "case": case["name"],
                "ok": srgb_encoded_channel_to_linear(decode_value(case["encoded"]))
                is not None,
            }
        )

    verdicts.sort(key=lambda v: f"{v['group']}::{v['case']}")
    return verdicts


def _raw_typescript_verdicts() -> list[dict]:
    """Run the TypeScript emitter and return its output unmodified."""
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


def _typescript_verdicts() -> list[dict]:
    raw = _raw_typescript_verdicts()
    # Normalize the TS float encoding into the same canonical form.
    for entry in raw:
        for scalar in ("meters", "radians", "factor"):
            if scalar in entry:
                entry[scalar] = _normalize_encoded(entry[scalar])
        for vector in ("delta", "color"):
            if vector in entry:
                entry[vector] = {
                    key: _normalize_encoded(value)
                    for key, value in entry[vector].items()
                }
        # The transfer-function group carries only a verdict on this side; the
        # numeric agreement is checked within tolerance in its own test.
        if entry.get("group") == "srgb-transfer":
            entry.pop("linear", None)
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

    for case in CASES["angles"]["valid"]:
        result = to_radians({"value": decode_value(case["value"]), "unit": case["unit"]})
        assert result.ok, case["name"]
        assert result.radians == case["radians"], case["name"]

    for case in CASES["resize"]["valid"]:
        result = resize_factor(decode_value(case["percent"]), case["direction"])
        assert result.ok, case["name"]
        assert result.factor == case["factor"], case["name"]

    for case in CASES["colors"]["named"]:
        result = resolve_named_color(case["input"])
        assert result.ok, case["name"]
        for channel, expected in case["color"].items():
            assert abs(getattr(result.color, channel) - expected) <= 1e-12, case["name"]


def test_srgb_transfer_agrees_within_tolerance():
    """The transfer function is compared within the corpus tolerance, not bit for
    bit.

    The power segment uses `pow`, which is correctly rounded in glibc but not
    guaranteed so in V8, so demanding the same final bit would be a contract the
    platform cannot keep. Observed bit-identical on the development machine; the
    tolerance is what is actually promised.

    The PALETTE is unaffected: it is authored in canonical linear literals, so a
    named lookup is exactly equal in both languages and is compared exactly above.
    """
    tolerance = CASES["colors"]["transfer"]["tolerance"]
    ts = {
        verdict["case"]: verdict
        for verdict in _raw_typescript_verdicts()
        if verdict["group"] == "srgb-transfer"
    }
    assert ts, "expected transfer verdicts from the TypeScript side"
    for case in CASES["colors"]["transfer"]["cases"]:
        python_value = srgb_encoded_channel_to_linear(case["encoded"])
        assert python_value is not None, case["name"]
        assert abs(python_value - case["linear"]) <= tolerance, case["name"]
        typescript_value = float(ts[case["name"]]["linear"])
        assert abs(python_value - typescript_value) <= tolerance, (
            f"{case['name']}: python {python_value!r} vs typescript "
            f"{typescript_value!r}"
        )

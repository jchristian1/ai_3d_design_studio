"""Scene description contracts and the AUTHORITATIVE scene-version digest.

Spec 002, Task 1.

This module owns exactly one definition of "scene state digest" for the whole
repository. Nothing else may hash scene state: a second, subtly different
definition would mean two answers to "did the scene change?", which is the one
question the Spec 002 execution precondition depends on.

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL
             |
             +--> TypeScript representation (packages/contracts/src/scene.ts)
             +--> Python representation      (this module)

Deliberate non-goals for this task: no Blender, no ``bpy``, no worker, no MCP
tool, no context building, no provider, and no HTTP. This layer only describes
and validates.

Why the digest is a PROJECTION and not a hash of the snapshot
------------------------------------------------------------

Hashing a ``SceneSnapshot`` document mechanically would fail twice over:

1. ``captured_at`` changes on every read, so two reads of an unchanged scene
   would produce different versions and every plan would be refused.
2. Worse, a deny-list ("hash everything except captured_at") makes concurrency
   semantics change SILENTLY: adding a future informational field — a thumbnail
   hint, a UI label, a statistics block — would change every project's
   ``scene_version`` and invalidate in-flight plans for no design reason.

So the digest input is an explicit allow-list keyed by a digest version. A field
that is not in the projection cannot affect concurrency detection, whether it
exists today or is added later. Adding a PLANNING-RELEVANT field is a deliberate
act that requires a new ``digest_version``; adding an informational one is free.
``test_scene.py`` enforces the choice mechanically (see
``SCENE_SNAPSHOT_INFORMATIONAL_FIELDS``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from studio_types import UNIT_SYSTEMS

from . import SCHEMA_FILES, ChatError, to_wire
from .schema import validate_against_schema

# ---------------------------------------------------------------------------
# Digest version and projection membership
# ---------------------------------------------------------------------------

#: The digest projection identity. It is hashed INSIDE the payload, so it is real
#: domain separation rather than a comment: two projections can never collide, and
#: a version change is visible in the input.
#:
#: Bumping this invalidates every recorded scene_version exactly once, and refuses
#: every in-flight plan. That is why it is only bumped deliberately.
SCENE_DIGEST_VERSION = "studio-scene-v1"

#: Snapshot fields that ARE part of the digest.
SCENE_SNAPSHOT_DIGEST_FIELDS: tuple[str, ...] = ("units", "objects")

#: Snapshot fields deliberately EXCLUDED from the digest, each with a reason.
#:
#: ``captured_at``  — capture metadata; changes on every read.
#: ``scene_version`` — the digest itself; cannot be its own input.
#: ``project_id``   — the digest answers "is this the same scene state?", not
#:                    "whose scene is this?". Authorization and cache keying are
#:                    already project-scoped elsewhere, and excluding it keeps the
#:                    useful property that an identical scene digests identically
#:                    regardless of which project holds it. Domain separation
#:                    comes from ``digest_version``, which is what it is for.
SCENE_SNAPSHOT_INFORMATIONAL_FIELDS: tuple[str, ...] = (
    "captured_at",
    "project_id",
    "scene_version",
)

#: Object fields that ARE part of the digest.
SCENE_OBJECT_DIGEST_FIELDS: tuple[str, ...] = (
    "studio_object_id",
    "name",
    "object_type",
    "world_position_meters",
    "dimensions_meters",
    "rotation_euler_radians",
    "scale",
    "visible",
    "material",
)

#: Object fields deliberately excluded from the digest. Empty today: every field
#: the snapshot exposes about an object is something the model may plan against.
SCENE_OBJECT_INFORMATIONAL_FIELDS: tuple[str, ...] = ()

#: Unit fields that ARE part of the digest. All of them: a metre value can only be
#: read in light of the unit configuration it was measured in.
SCENE_UNITS_DIGEST_FIELDS: tuple[str, ...] = (
    "unit_system",
    "length_unit",
    "scale_length",
)

#: Names that must never become representable in a scene contract. Asserted
#: against the canonical schemas by the security contract tests, so this list is
#: documentation of an enforced property rather than a hopeful convention.
FORBIDDEN_SCENE_FIELDS: tuple[str, ...] = (
    "blend_path",
    "blendfile",
    "code",
    "command",
    "directory",
    "env",
    "environment",
    "expression",
    "filename",
    "filepath",
    "host",
    "hostname",
    "metadata",
    "path",
    "pointer",
    "python",
    "script",
    "secret",
    "session_id",
    "shell",
    "token",
    "url",
    "worker_id",
    "worker_token",
)

# ---------------------------------------------------------------------------
# Deterministic numeric representation
# ---------------------------------------------------------------------------

#: Decimal places every quantised value is formatted with. One quantum for all
#: quantities, chosen so the rule is impossible to misapply: 1e-6 is 1 micrometre
#: for a length, ~0.2 arcseconds for an angle, and one part in a million for a
#: unitless factor or a colour channel.
#:
#: Quantisation exists to remove REPRESENTATION noise, not change. Blender stores
#: these values as float32, so a digest computed twice from the same bytes is
#: identical regardless of the quantum; what the shared fixed-decimal form buys is
#: that Python and TypeScript agree exactly, with no dependence on either
#: language's float repr (Python renders 1.0 as "1.0", JavaScript as "1", and
#: exponent formatting differs too — which is precisely why quantised values are
#: serialised as fixed-decimal STRINGS below rather than as JSON numbers).
#:
#: 1e-6 is three orders of magnitude below the smallest design-meaningful change
#: and well below the calibrated Spec 001 position tolerance, so no change large
#: enough to be APPLIED can vanish into the quantum.
DIGEST_DECIMALS = 6

#: The quantum implied by DIGEST_DECIMALS, stated explicitly for documentation.
DIGEST_QUANTUM = 1e-6


class SceneDigestError(ValueError):
    """Raised when scene state cannot be digested deterministically.

    A hard error rather than a serialised token: an unreadable scene must be
    refused, never hashed into a value that looks authoritative.
    """


def format_digest_number(value: Any) -> str:
    """Quantise a number and format it as a fixed-decimal string.

    Rules, all of which exist to make two implementations agree byte for byte:
      - round half to even at the quantum (Python's and JS's default rounding
        differ, so the rounding mode is chosen explicitly, not inherited);
      - always exactly ``DIGEST_DECIMALS`` decimals, never exponent notation;
      - negative zero normalises to ``0.000000``, so ``-0.0`` and ``0.0`` cannot
        produce different scene versions (Blender reports unset Euler components
        as ``-0.0``);
      - non-finite values raise.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneDigestError(
            f"scene state contains a non-numeric value where a number is required: "
            f"{type(value).__name__}"
        )
    if not math.isfinite(value):
        raise SceneDigestError(
            "scene state contains a non-finite number (NaN or Infinity); an "
            "unreadable scene is refused rather than digested"
        )

    quantised = _round_half_even(float(value), DIGEST_DECIMALS)
    text = f"{quantised:.{DIGEST_DECIMALS}f}"
    # Normalise every representation of zero, including the "-0.000000" that a
    # tiny negative value produces once it is quantised.
    if float(text) == 0:
        return "0." + "0" * DIGEST_DECIMALS
    return text


def _round_half_even(value: float, decimals: int) -> float:
    """Round half to even at ``decimals`` places.

    Python's built-in ``round`` is already half-to-even on floats, but it is
    spelled out here so the TypeScript implementation has an unambiguous rule to
    mirror (JavaScript's ``toFixed`` rounds half away from zero, and its behaviour
    on ties is implementation-influenced by binary representation).
    """
    scale = 10**decimals
    scaled = value * scale
    floor = math.floor(scaled)
    diff = scaled - floor
    if diff > 0.5:
        rounded = floor + 1
    elif diff < 0.5:
        rounded = floor
    else:
        rounded = floor if floor % 2 == 0 else floor + 1
    return rounded / scale


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_digest_json(value: Any) -> str:
    """Serialize the digest projection deterministically.

    UTF-8 text, object keys sorted lexicographically, no insignificant
    whitespace, JSON literals for booleans and null, and no non-ASCII escaping
    (Python's default ``ensure_ascii=True`` would escape a name like "Küche" while
    ``JSON.stringify`` would not, so the two languages would disagree).

    All numbers in the projection are already fixed-decimal STRINGS produced by
    :func:`format_digest_number`, so this function never has to render a float.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def _as_wire(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_wire(value)
    return value


def _triple(source: Any, field: str, keys: Sequence[str] = ("x", "y", "z")) -> list[str]:
    node = _as_wire(source)
    if not isinstance(node, Mapping):
        raise SceneDigestError(f"{field} must be an object with {list(keys)}")
    out: list[str] = []
    for key in keys:
        if key not in node:
            raise SceneDigestError(f"{field} is missing component {key!r}")
        out.append(format_digest_number(node[key]))
    return out


def object_sort_key(obj: Any) -> tuple[int, str]:
    """The documented TOTAL order objects are sorted by before digesting.

    ``(0, studio_object_id)`` when a stable id is present, else ``(1, name)``.
    Objects with a stable id sort first, then by id; the rest sort by name.

    Blender guarantees object names are unique within a file, so the fallback is a
    genuine total order rather than a tie-break. The caller still verifies keys are
    strictly increasing: a duplicate key means an ambiguous snapshot, which is
    refused rather than digested into an arbitrary order.
    """
    wire = _as_wire(obj)
    if not isinstance(wire, Mapping):
        raise SceneDigestError("scene object must be an object")
    object_id = wire.get("studio_object_id")
    if isinstance(object_id, str) and object_id.strip():
        return (0, object_id)
    name = wire.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SceneDigestError(
            "scene object must have a non-blank name to be ordered deterministically"
        )
    return (1, name)


def _material_projection(material: Any) -> Optional[dict[str, Any]]:
    if material is None:
        return None
    wire = _as_wire(material)
    if not isinstance(wire, Mapping):
        raise SceneDigestError("material must be an object or absent")
    base_color = wire.get("base_color")
    return {
        "name": wire.get("name"),
        "base_color": (
            None
            if base_color is None
            else _triple(base_color, "material.base_color", ("r", "g", "b", "a"))
        ),
    }


def _object_projection(obj: Any) -> dict[str, Any]:
    wire = _as_wire(obj)
    if not isinstance(wire, Mapping):
        raise SceneDigestError("scene object must be an object")

    name = wire.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SceneDigestError("scene object requires a non-blank name")
    object_type = wire.get("object_type")
    if not isinstance(object_type, str) or not object_type.strip():
        raise SceneDigestError(f"{name}: object_type is required")
    visible = wire.get("visible")
    if not isinstance(visible, bool):
        raise SceneDigestError(f"{name}: visible must be a boolean")

    studio_object_id = wire.get("studio_object_id")
    if studio_object_id is not None and not isinstance(studio_object_id, str):
        raise SceneDigestError(f"{name}: studio_object_id must be a string or absent")

    return {
        # Absent optionals become explicit null so "no material" and "material
        # removed" cannot alias into different projection SHAPES.
        "studio_object_id": studio_object_id,
        "name": name,
        "object_type": object_type,
        "world_position_meters": _triple(
            wire.get("world_position_meters"), f"{name}.world_position_meters"
        ),
        "dimensions_meters": _triple(
            wire.get("dimensions_meters"), f"{name}.dimensions_meters"
        ),
        "rotation_euler_radians": _triple(
            wire.get("rotation_euler_radians"), f"{name}.rotation_euler_radians"
        ),
        "scale": _triple(wire.get("scale"), f"{name}.scale"),
        "visible": visible,
        "material": _material_projection(wire.get("material")),
    }


def _units_projection(units: Any) -> dict[str, Any]:
    wire = _as_wire(units)
    if not isinstance(wire, Mapping):
        raise SceneDigestError("units must be an object")
    unit_system = wire.get("unit_system")
    length_unit = wire.get("length_unit")
    if not isinstance(unit_system, str) or not unit_system:
        raise SceneDigestError("units.unit_system is required")
    if not isinstance(length_unit, str) or not length_unit:
        raise SceneDigestError("units.length_unit is required")
    return {
        "unit_system": unit_system,
        "length_unit": length_unit,
        "scale_length": format_digest_number(wire.get("scale_length")),
    }


def scene_digest_projection(scene: Any) -> dict[str, Any]:
    """Build the exact ``studio-scene-v1`` digest input.

    Accepts a :class:`SceneSnapshot`, a snapshot wire mapping, or any mapping that
    carries ``units`` and ``objects``. The last form matters: a snapshot cannot be
    constructed before its ``scene_version`` is known, so the producer digests
    ``{"units": ..., "objects": [...]}`` first. That the projection is happy
    without ``project_id``, ``captured_at`` or ``scene_version`` is the clearest
    demonstration that it is not "the snapshot".
    """
    wire = _as_wire(scene)
    if not isinstance(wire, Mapping):
        raise SceneDigestError("scene must be an object")
    if "units" not in wire or "objects" not in wire:
        raise SceneDigestError("scene must carry 'units' and 'objects'")

    raw_objects = wire.get("objects")
    if isinstance(raw_objects, (str, bytes)) or not isinstance(
        raw_objects, (list, tuple)
    ):
        raise SceneDigestError("scene.objects must be an array")

    ordered = sorted(raw_objects, key=object_sort_key)
    keys = [object_sort_key(obj) for obj in ordered]
    for previous, current in zip(keys, keys[1:]):
        if previous == current:
            raise SceneDigestError(
                f"ambiguous scene: two objects share the sort key {current!r}; "
                "refusing to digest an arbitrary order"
            )

    return {
        "digest_version": SCENE_DIGEST_VERSION,
        "units": _units_projection(wire.get("units")),
        "objects": [_object_projection(obj) for obj in ordered],
    }


def compute_scene_version(scene: Any) -> str:
    """The authoritative ``scene_version`` of a semantic scene state.

    ``sha256:<64 hex chars>``, prefixed with its algorithm so a future migration is
    unambiguous. Deterministic for unchanged semantic state and insensitive to
    ``captured_at``, ``project_id``, object enumeration order, and any field
    outside the projection.
    """
    canonical = canonical_digest_json(scene_digest_projection(scene))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

SCENE_MESSAGES: dict[str, str] = {
    "NON_FINITE": (
        "scene state contains a non-finite number; JSON cannot encode NaN or "
        "Infinity, so the finite rule is enforced here"
    ),
    "SCALE_LENGTH": "units.scale_length must be a finite number greater than zero",
    "UNKNOWN_UNIT_SYSTEM": (
        "units.unit_system must be one of: " + ", ".join(UNIT_SYSTEMS)
    ),
    "DEGENERATE_SCALE": (
        "scale components must be non-zero; a zero scale collapses the object and "
        "is not a recoverable state"
    ),
    "AMBIGUOUS_ORDER": (
        "two objects share the same digest sort key, so the scene cannot be "
        "digested deterministically"
    ),
    "VERSION_MISMATCH": (
        "scene_version does not match the scene state it accompanies"
    ),
}


@dataclass
class SceneValidationResult:
    valid: bool
    errors: list[ChatError]


def _finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def validate_scene_snapshot(
    snapshot: Any, *, verify_scene_version: bool = False
) -> SceneValidationResult:
    """Validate a snapshot against the canonical schema plus the rules JSON
    Schema cannot express.

    The schema covers structure, closure and ranges. This adds:
      - finite numbers everywhere (JSON has no NaN/Infinity literal),
      - ``scale_length`` strictly positive (the supported keyword subset has no
        ``exclusiveMinimum``),
      - non-zero scale components,
      - deterministic ordering (no two objects sharing a digest sort key),
      - optionally, that ``scene_version`` actually matches the state it
        accompanies. Off by default because a producer computes the version and
        then builds the snapshot; on for consumers that must not trust a label.
    """
    errors: list[ChatError] = []
    wire = _as_wire(snapshot)
    if not isinstance(wire, Mapping):
        return SceneValidationResult(
            valid=False,
            errors=[
                ChatError(
                    code="VALIDATION_ERROR", message="scene snapshot must be an object"
                )
            ],
        )

    schema_result = validate_against_schema(SCHEMA_FILES["SceneSnapshot"], wire)
    for violation in schema_result.violations:
        errors.append(
            ChatError(
                code="VALIDATION_ERROR",
                message=f"{violation.path or 'scene_snapshot'}: {violation.message}",
            )
        )

    units = wire.get("units")
    if isinstance(units, Mapping):
        scale_length = units.get("scale_length")
        if not _finite(scale_length) or scale_length <= 0:
            errors.append(
                ChatError(
                    code="INVALID_UNITS", message=SCENE_MESSAGES["SCALE_LENGTH"]
                )
            )
        if units.get("unit_system") not in UNIT_SYSTEMS:
            errors.append(
                ChatError(
                    code="INVALID_UNITS",
                    message=SCENE_MESSAGES["UNKNOWN_UNIT_SYSTEM"],
                )
            )

    objects = wire.get("objects")
    if isinstance(objects, (list, tuple)):
        for entry in objects:
            if not isinstance(entry, Mapping):
                continue
            label = entry.get("name") or "<unnamed>"
            for field in (
                "world_position_meters",
                "dimensions_meters",
                "rotation_euler_radians",
                "scale",
            ):
                vector = entry.get(field)
                if not isinstance(vector, Mapping):
                    continue
                for axis in ("x", "y", "z"):
                    if not _finite(vector.get(axis)):
                        errors.append(
                            ChatError(
                                code="INVALID_UNITS",
                                message=(
                                    f"{label}.{field}.{axis}: "
                                    f"{SCENE_MESSAGES['NON_FINITE']}"
                                ),
                            )
                        )
            scale = entry.get("scale")
            if isinstance(scale, Mapping):
                for axis in ("x", "y", "z"):
                    if _finite(scale.get(axis)) and scale[axis] == 0:
                        errors.append(
                            ChatError(
                                code="VALIDATION_ERROR",
                                message=(
                                    f"{label}.scale.{axis}: "
                                    f"{SCENE_MESSAGES['DEGENERATE_SCALE']}"
                                ),
                            )
                        )
            material = entry.get("material")
            if isinstance(material, Mapping) and isinstance(
                material.get("base_color"), Mapping
            ):
                for channel in ("r", "g", "b", "a"):
                    value = material["base_color"].get(channel)
                    if not _finite(value):
                        errors.append(
                            ChatError(
                                code="VALIDATION_ERROR",
                                message=(
                                    f"{label}.material.base_color.{channel}: "
                                    f"{SCENE_MESSAGES['NON_FINITE']}"
                                ),
                            )
                        )

        try:
            keys = sorted(object_sort_key(obj) for obj in objects)
        except SceneDigestError:
            keys = []
        for previous, current in zip(keys, keys[1:]):
            if previous == current:
                errors.append(
                    ChatError(
                        code="VALIDATION_ERROR",
                        message=SCENE_MESSAGES["AMBIGUOUS_ORDER"],
                    )
                )
                break

    if verify_scene_version and not errors:
        try:
            expected = compute_scene_version(wire)
        except SceneDigestError as exc:
            errors.append(ChatError(code="VALIDATION_ERROR", message=str(exc)))
        else:
            if wire.get("scene_version") != expected:
                errors.append(
                    ChatError(
                        code="SCENE_VERSION_MISMATCH",
                        message=SCENE_MESSAGES["VERSION_MISMATCH"],
                    )
                )

    return SceneValidationResult(valid=not errors, errors=errors)


def scene_versions_match(left: str, right: str) -> bool:
    """Compare two scene versions.

    A named function rather than ``==`` at call sites, so the comparison has one
    place to live if the digest ever gains a migration path, and so a caller can
    never accidentally compare a version against a truncated or prefixed form.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left == right


__all__ = [
    "DIGEST_DECIMALS",
    "DIGEST_QUANTUM",
    "FORBIDDEN_SCENE_FIELDS",
    "SCENE_DIGEST_VERSION",
    "SCENE_MESSAGES",
    "SCENE_OBJECT_DIGEST_FIELDS",
    "SCENE_OBJECT_INFORMATIONAL_FIELDS",
    "SCENE_SNAPSHOT_DIGEST_FIELDS",
    "SCENE_SNAPSHOT_INFORMATIONAL_FIELDS",
    "SCENE_UNITS_DIGEST_FIELDS",
    "SceneDigestError",
    "SceneValidationResult",
    "canonical_digest_json",
    "compute_scene_version",
    "format_digest_number",
    "object_sort_key",
    "scene_digest_projection",
    "scene_versions_match",
    "validate_scene_snapshot",
]

"""Single-conversion-site guard — Python runtime code (Spec 002, Task 2).

Spec 001 established one rule and audited it once by hand: cm->m conversion exists
in exactly one place, ``packages/spatial``. Spec 002 adds three more quantities that
can be converted wrongly — degrees to radians, a percentage to a size factor, and
encoded sRGB to linear light — so the rule becomes a permanent, executable guard
rather than an audit note.

Why it matters, concretely. Every one of these has a plausible-looking wrong form
that produces a number rather than an error:

  * ``degrees * 3.14 / 180`` is close enough to look right and wrong enough to
    misplace geometry;
  * ``percent / 100`` written twice eventually disagrees about whether 100% smaller
    is legal;
  * ``value / 255`` looks like a colour conversion but is off by a factor of 2.3 in
    the midtones, and the result is still a plausible colour.

A second definition of any of them would not fail loudly. It would quietly disagree.

Implementation notes, both deliberate:

1. This is an AST guard, not a text scan. Text scanning was rejected because the
   repository legitimately contains the literal ``2.4`` (a measured Blender
   tolerance), the literal ``100`` (retry bounds, prose), and the words "warm beige"
   (tests, documentation). A string search would flag those and be switched off
   within a week. The AST check instead asks what a number is being USED for: is 100
   the right operand of a division, is 2.4 an exponent, is 180 paired with pi.

2. Every detector is proved to actually detect. ``test_detector_*`` runs the same
   functions against synthetic violating source, so the suite cannot pass because
   the scan is looking in the wrong place — the exact failure mode Spec 001's Task 13
   audit found in the "workstation never listens" guard.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable, Iterable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The approved home of every conversion. Nothing here is scanned.
APPROVED_PACKAGE = REPO_ROOT / "packages" / "spatial" / "python" / "studio_spatial"

#: Runtime source roots. Tests are excluded: a test may legitimately compute an
#: expected value by hand, and forbidding that would make the guard adversarial to
#: the very assertions that pin the conversions.
RUNTIME_ROOTS = (
    REPO_ROOT / "services" / "agent" / "studio_agent",
    REPO_ROOT / "services" / "api" / "studio_api",
    REPO_ROOT / "services" / "blender-mcp" / "blender_mcp",
    REPO_ROOT / "services" / "blender-worker" / "blender_worker",
    REPO_ROOT / "services" / "preview" / "studio_preview",
    REPO_ROOT / "packages" / "contracts" / "python" / "studio_contracts",
    REPO_ROOT / "packages" / "types" / "python" / "studio_types",
    REPO_ROOT / "packages" / "validation" / "python" / "studio_validation",
)

#: Palette names that must exist in exactly one table.
PALETTE_NAMES = ("warm beige", "beige", "grey", "gray")


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in RUNTIME_ROOTS:
        assert root.is_dir(), f"runtime root missing: {root}"
        for path in sorted(root.rglob("*.py")):
            if APPROVED_PACKAGE in path.parents:
                continue
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            files.append(path)
    return files


RUNTIME_FILES = _runtime_files()
TREES: list[tuple[Path, ast.AST]] = [
    (path, ast.parse(path.read_text("utf-8"))) for path in RUNTIME_FILES
]


def _describe(path: Path, node: ast.AST) -> str:
    label = path.name if path.is_absolute() else str(path)
    return f"{label}:{getattr(node, 'lineno', '?')}"


def _is_constant(node: ast.AST, value: float) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value == value
    )


def _is_pi(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "pi":
        return True
    return isinstance(node, ast.Name) and node.id == "pi"


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


# ---------------------------------------------------------------------------
# Detectors. Each takes (path, tree) and returns human-readable offenders.
# ---------------------------------------------------------------------------


def find_division_by(divisor: float) -> Callable[[Path, ast.AST], list[str]]:
    def detector(path: Path, tree: ast.AST) -> list[str]:
        return [
            _describe(path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _is_constant(node.right, divisor)
        ]

    return detector


def find_multiplication_by_hundredth(path: Path, tree: ast.AST) -> list[str]:
    return [
        _describe(path, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and (_is_constant(node.right, 0.01) or _is_constant(node.left, 0.01))
    ]


def find_conversion_constant_assignment(path: Path, tree: ast.AST) -> list[str]:
    return [
        _describe(path, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and any(
                token in target.id
                for token in ("CM_PER", "PERCENT_WHOLE", "RADIANS_PER", "SRGB_")
            )
            for target in node.targets
        )
    ]


def _arithmetic_operands(node: ast.BinOp) -> list[ast.AST]:
    """Operands of a BinOp, flattened one level through nested BinOps.

    Enough to recognise every spelling of the conversion that appears in practice —
    ``x * pi / 180``, ``x * (pi / 180)``, ``x / 180 * pi`` — without walking the
    whole subtree, which would start flagging unrelated arithmetic that happens to
    contain a 180 somewhere far below.
    """
    out: list[ast.AST] = []
    for operand in (node.left, node.right):
        out.append(operand)
        if isinstance(operand, ast.BinOp):
            out.extend((operand.left, operand.right))
    return out


def find_degree_radian_arithmetic(path: Path, tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        operands = _arithmetic_operands(node)
        has_180 = any(_is_constant(operand, 180) for operand in operands)
        has_pi = any(_is_pi(operand) for operand in operands)
        if has_180 and has_pi:
            offenders.append(_describe(path, node))
    return offenders


def find_stdlib_angle_calls(path: Path, tree: ast.AST) -> list[str]:
    return [
        _describe(path, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in ("radians", "degrees")
    ]


def find_pi_reference(path: Path, tree: ast.AST) -> list[str]:
    return [_describe(path, node) for node in ast.walk(tree) if _is_pi(node)]


def find_srgb_transfer_arithmetic(path: Path, tree: ast.AST) -> list[str]:
    magic = (1.055, 12.92, 0.04045, 0.0031308)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            for operand in (node.left, node.right):
                if any(_is_constant(operand, value) for value in magic):
                    offenders.append(_describe(path, node))
            # `x ** 2.4` — the exponent, not the bare number.
            if isinstance(node.op, ast.Pow) and _is_constant(node.right, 2.4):
                offenders.append(_describe(path, node))
        # The piecewise branch: `if e <= 0.04045`. A comparison against one of the
        # published constants is as much a transfer implementation as the arithmetic.
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if any(_is_constant(operand, value) for value in magic):
                    offenders.append(_describe(path, node))
        if (
            isinstance(node, ast.Call)
            and _called_name(node) == "pow"
            and len(node.args) == 2
            and _is_constant(node.args[1], 2.4)
        ):
            offenders.append(_describe(path, node))
    return offenders


def find_palette_name(name: str) -> Callable[[Path, ast.AST], list[str]]:
    def detector(path: Path, tree: ast.AST) -> list[str]:
        return [
            _describe(path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.strip().lower() == name
        ]

    return detector


def _scan(detector: Callable[[Path, ast.AST], list[str]]) -> list[str]:
    offenders: list[str] = []
    for path, tree in TREES:
        offenders.extend(detector(path, tree))
    return offenders


def _offenders_in(source: str, detector: Callable[[Path, ast.AST], list[str]]) -> list[str]:
    return detector(Path("synthetic.py"), ast.parse(source))


# ---------------------------------------------------------------------------
# The scan itself must be looking at real code
# ---------------------------------------------------------------------------


def test_the_scan_covers_a_plausible_number_of_files():
    """A guard that silently scans nothing passes forever.

    Spec 001's Task 13 audit found this exact failure mode in the "never listens"
    guard, which had been scoped to one subdirectory and so never saw the module that
    broke the rule.
    """
    assert len(RUNTIME_FILES) >= 40, f"only scanned {len(RUNTIME_FILES)} files"
    names = {path.name for path in RUNTIME_FILES}
    for expected in ("executor.py", "chat_service.py", "move_object.py", "jobs.py"):
        assert expected in names, f"scan missed {expected}"


def test_the_approved_package_is_excluded_from_the_scan():
    assert not any(APPROVED_PACKAGE in path.parents for path in RUNTIME_FILES)


# ---------------------------------------------------------------------------
# cm <-> m and percentage arithmetic
# ---------------------------------------------------------------------------


def test_no_runtime_division_by_one_hundred():
    """`x / 100` is either a cm->m conversion or a percentage conversion, and both
    belong to packages/spatial. The literal 100 elsewhere (a retry bound, a prose
    string) is untouched: only its use as a DIVISOR is flagged."""
    offenders = _scan(find_division_by(100))
    assert not offenders, (
        "division by 100 outside packages/spatial — route unit and percentage "
        f"conversion through studio_spatial: {offenders}"
    )


def test_no_runtime_multiplication_by_the_inexact_reciprocal():
    """`x * 0.01` is the double-rounding form Spec 001 rejected for cm->m."""
    offenders = _scan(find_multiplication_by_hundredth)
    assert not offenders, f"multiplication by 0.01 outside packages/spatial: {offenders}"


def test_no_runtime_conversion_constant_is_redefined():
    offenders = _scan(find_conversion_constant_assignment)
    assert not offenders, f"conversion constant redefined: {offenders}"


# ---------------------------------------------------------------------------
# degrees <-> radians
# ---------------------------------------------------------------------------


def test_no_runtime_degree_radian_arithmetic():
    """Any arithmetic that pairs 180 with pi is an angle conversion."""
    offenders = _scan(find_degree_radian_arithmetic)
    assert not offenders, (
        "degree/radian arithmetic outside packages/spatial — use "
        f"studio_spatial.degrees_to_radians: {offenders}"
    )


def test_no_runtime_calls_to_the_stdlib_angle_helpers():
    """`math.radians` / `math.degrees` are a second conversion site by another name,
    and they are also why the shared implementation spells the formula out:
    JavaScript has no counterpart, so parity could not rest on them."""
    offenders = _scan(find_stdlib_angle_calls)
    assert not offenders, f"stdlib angle conversion used directly: {offenders}"


def test_no_runtime_pi_constant_outside_the_approved_package():
    """There is no legitimate reason for runtime code outside packages/spatial to
    reference pi today. If one appears — a camera framing calculation, say — it
    should be reviewed by editing this guard rather than added silently."""
    offenders = _scan(find_pi_reference)
    assert not offenders, f"pi referenced outside packages/spatial: {offenders}"


# ---------------------------------------------------------------------------
# sRGB transfer function
# ---------------------------------------------------------------------------


def test_no_runtime_srgb_transfer_arithmetic():
    """The transfer function is recognised by its published constants USED IN
    ARITHMETIC. The bare literal 2.4 is deliberately not flagged: it appears as a
    measured Blender tolerance in blender-mcp, and flagging it would be the kind of
    false positive that gets a guard deleted."""
    offenders = _scan(find_srgb_transfer_arithmetic)
    assert not offenders, (
        "sRGB transfer arithmetic outside packages/spatial — use "
        f"studio_spatial.srgb_encoded_channel_to_linear: {offenders}"
    )


def test_no_runtime_division_by_255():
    """`value / 255` is the naive colour conversion that looks like a transfer
    function and is not one."""
    offenders = _scan(find_division_by(255))
    assert not offenders, f"division by 255 outside packages/spatial: {offenders}"


# ---------------------------------------------------------------------------
# Named-colour table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PALETTE_NAMES)
def test_no_duplicate_named_colour_table(name):
    """A second palette would let two parts of the platform disagree about what
    "warm beige" is. Only string CONSTANTS in code are examined, so a docstring
    mentioning the colour is fine."""
    offenders = _scan(find_palette_name(name))
    assert not offenders, (
        f"colour name {name!r} hard-coded outside the palette: {offenders}"
    )


# ---------------------------------------------------------------------------
# Every detector must actually detect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,detector",
    [
        ("meters = value / 100", find_division_by(100)),
        ("meters = value / 100.0", find_division_by(100)),
        ("factor = (100 - percent) / 100", find_division_by(100)),
        ("meters = value * 0.01", find_multiplication_by_hundredth),
        ("meters = 0.01 * value", find_multiplication_by_hundredth),
        ("CM_PER_METER = 100", find_conversion_constant_assignment),
        ("RADIANS_PER_DEGREE = 0.0174", find_conversion_constant_assignment),
        ("SRGB_EXPONENT = 2.4", find_conversion_constant_assignment),
        ("radians = degrees * math.pi / 180", find_degree_radian_arithmetic),
        ("radians = degrees * (math.pi / 180)", find_degree_radian_arithmetic),
        ("radians = degrees / 180 * math.pi", find_degree_radian_arithmetic),
        ("radians = 180 / pi", find_degree_radian_arithmetic),
        ("radians = math.radians(45)", find_stdlib_angle_calls),
        ("degrees = math.degrees(x)", find_stdlib_angle_calls),
        ("from math import radians\nr = radians(45)", find_stdlib_angle_calls),
        ("circumference = 2 * math.pi * r", find_pi_reference),
        ("linear = ((e + 0.055) / 1.055) ** 2.4", find_srgb_transfer_arithmetic),
        ("linear = e / 12.92", find_srgb_transfer_arithmetic),
        ("if e <= 0.04045:\n    pass", find_srgb_transfer_arithmetic),
        ("linear = pow(x, 2.4)", find_srgb_transfer_arithmetic),
        ("linear = x ** 2.4", find_srgb_transfer_arithmetic),
        ("channel = value / 255", find_division_by(255)),
        ('PALETTE = {"warm beige": (0.79, 0.64, 0.46)}', find_palette_name("warm beige")),
        ('if name == "Warm Beige":\n    pass', find_palette_name("warm beige")),
    ],
)
def test_detector_flags_a_real_violation(source, detector):
    """Proves the guard is not passing vacuously."""
    assert _offenders_in(source, detector), f"detector missed: {source!r}"


@pytest.mark.parametrize(
    "source,detector",
    [
        # The false positives the guard must NOT produce, each taken from real code.
        ("TOLERANCE_NOTE = 'set 2.4 -> read 2.4000000953674316'", find_srgb_transfer_arithmetic),
        ("expected = Vec3(0.0, 0.0, 2.4)", find_srgb_transfer_arithmetic),
        ("MAX_ATTEMPTS = 100", find_division_by(100)),
        ("percent_label = f'{done} of 100'", find_division_by(100)),
        ("timeout = budget / 2", find_division_by(100)),
        ("width = 100", find_conversion_constant_assignment),
        ("angle_note = 'rotate 180 degrees'", find_degree_radian_arithmetic),
        ("HEIGHT = 180", find_degree_radian_arithmetic),
        ('"""Chooses a warm beige by name."""', find_palette_name("warm beige")),
    ],
)
def test_detector_does_not_flag_a_legitimate_use(source, detector):
    """The other half of the contract: a brittle guard is worse than none, because it
    gets disabled."""
    assert not _offenders_in(source, detector), f"false positive on: {source!r}"


# ---------------------------------------------------------------------------
# The approved package really is the one place
# ---------------------------------------------------------------------------


def test_the_approved_package_contains_every_conversion():
    """The mirror image of the tests above: if a conversion is not here, the scans
    are passing for the wrong reason."""
    source = "\n".join(
        path.read_text("utf-8") for path in sorted(APPROVED_PACKAGE.glob("*.py"))
    )
    for expected in (
        "CM_PER_METER",
        "RADIANS_PER_DEGREE",
        "PERCENT_WHOLE",
        "SRGB_LINEAR_THRESHOLD",
        "NAMED_COLORS",
    ):
        assert expected in source, f"{expected} is not defined in packages/spatial"


def test_conversions_that_do_happen_import_the_shared_package():
    """Modules that DO convert must import the shared utilities rather than
    reimplementing them. Today the rule-based provider is the only such module;
    naming it here means a new converter has to be added consciously."""
    importers = {
        path.name
        for path, tree in TREES
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "studio_spatial"
            for node in ast.walk(tree)
        )
    }
    assert "rule_based.py" in importers, (
        "the rule-based provider must delegate conversion to packages/spatial"
    )


def test_detector_helpers_are_all_exercised():
    """Keeps the detector list and the self-test list from drifting apart."""
    detectors: Iterable[str] = [
        name
        for name, value in globals().items()
        if name.startswith("find_") and callable(value)
    ]
    assert sorted(detectors) == [
        "find_conversion_constant_assignment",
        "find_degree_radian_arithmetic",
        "find_division_by",
        "find_multiplication_by_hundredth",
        "find_palette_name",
        "find_pi_reference",
        "find_srgb_transfer_arithmetic",
        "find_stdlib_angle_calls",
    ]

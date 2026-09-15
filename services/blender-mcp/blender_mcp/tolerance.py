"""The single position-comparison rule for Blender coordinates.

Spec 001, Task 4.

ONE tolerance is defined here and reused for all three comparisons in
move_object — the expected-before check, the already-applied check, and the
post-mutation verification. Epsilons must not be scattered through the code:
if the three comparisons could disagree, a move could be judged "not yet
applied" by one check and "already applied" by another.

Why the tolerance is magnitude-aware
------------------------------------
Blender stores object coordinates as 32-bit floats. Measured on Blender 5.2.1
by writing a value and reading it back:

    set 0.5      -> read 0.5                    error 0.0
    set 2.4      -> read 2.4000000953674316     error 9.5e-08
    set 100.4    -> read 100.4000015258789      error 1.5e-06
    set 1000.4   -> read 1000.4000244140625     error 2.4e-05

The round-trip error grows with magnitude, so a fixed absolute epsilon would
fail verification for objects far from the origin. The rule is therefore an
absolute floor near the origin plus a relative term that tracks float32
precision:

    equal(a, b)  <=>  |a - b| <= max(ABS, REL * max(|a|, |b|))

Tightness
---------
REL = 4e-7 is roughly 3.4x float32 epsilon (1.19e-07), leaving headroom for
matrix round-tripping while staying far below any meaningful architectural
displacement. Worst case within a 1 km scene the tolerance is 0.4 mm, so the
smallest displacement this project cares about (1 mm) is never hidden. Near the
origin the tolerance is 1 micrometre.
"""

from __future__ import annotations

import math
from typing import Any

#: Absolute floor, in meters (1 micrometre). Dominates near the origin.
POSITION_ABSOLUTE_TOLERANCE_M = 1e-6

#: Relative term tracking float32 storage precision (~3.4x float32 epsilon).
POSITION_RELATIVE_TOLERANCE = 4e-7

#: The largest displacement the tolerance could ever hide, within a 1 km scene.
#: Documented so the guarantee is explicit and testable.
MAX_HIDDEN_DISPLACEMENT_M_AT_1KM = POSITION_RELATIVE_TOLERANCE * 1000.0


def position_tolerance(a: float, b: float) -> float:
    """The tolerance that applies when comparing two coordinates."""
    return max(
        POSITION_ABSOLUTE_TOLERANCE_M,
        POSITION_RELATIVE_TOLERANCE * max(abs(a), abs(b)),
    )


def coordinates_equal(a: float, b: float) -> bool:
    """Compare two world-space coordinates in meters.

    Non-finite values are never equal to anything, including themselves: a NaN
    coordinate means the scene state is unusable, not that it matches.
    """
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= position_tolerance(a, b)


def positions_equal(a: Any, b: Any) -> bool:
    """Compare two Vec3 world-space positions in meters, component-wise."""
    return (
        coordinates_equal(a.x, b.x)
        and coordinates_equal(a.y, b.y)
        and coordinates_equal(a.z, b.z)
    )


def is_finite_position(position: Any) -> bool:
    """True when every component of a position is a finite number of meters."""
    for component in (position.x, position.y, position.z):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            return False
        if not math.isfinite(component):
            return False
    return True

"""Grid snapping and coordinate math for KiCad 9 schematics."""

import math

COARSE_GRID = 2.54  # mm — standard KiCad grid
FINE_GRID = 1.27    # mm — half grid for rotated pin positions


def snap_coarse(value: float) -> float:
    """Snap a value to the 2.54mm coarse grid."""
    return round(value / COARSE_GRID) * COARSE_GRID


def snap_fine(value: float) -> float:
    """Snap a value to the 1.27mm fine grid."""
    return round(value / FINE_GRID) * FINE_GRID


def snap_point_coarse(x: float, y: float) -> tuple[float, float]:
    """Snap an (x, y) point to the coarse grid."""
    return snap_coarse(x), snap_coarse(y)


def snap_point_fine(x: float, y: float) -> tuple[float, float]:
    """Snap an (x, y) point to the fine grid."""
    return snap_fine(x), snap_fine(y)


def fmt(value: float) -> str:
    """Format a float for KiCad S-expression output.

    Removes trailing zeros but keeps at least one decimal place
    for values that aren't integers. Integers get no decimal.
    """
    if value == int(value):
        return str(int(value))
    # Up to 4 decimal places, strip trailing zeros
    s = f"{value:.4f}".rstrip("0").rstrip(".")
    return s


def outward_direction(kicad_skip_rotation: float) -> int:
    """Convert kicad-skip pin rotation to outward wire direction.

    Returns: 0=RIGHT, 90=DOWN, 180=LEFT, 270=UP
    (in schematic Y-down coordinates)
    """
    return int((540 - kicad_skip_rotation) % 360)


def direction_dx_dy(direction: int) -> tuple[float, float]:
    """Get (dx, dy) unit vector for a direction (0/90/180/270)."""
    if direction == 0:
        return 1.0, 0.0
    elif direction == 90:
        return 0.0, 1.0
    elif direction == 180:
        return -1.0, 0.0
    elif direction == 270:
        return 0.0, -1.0
    else:
        rad = math.radians(direction)
        return math.cos(rad), math.sin(rad)


def wire_stub(px: float, py: float, direction: int, length: float = 7.62) -> tuple[float, float]:
    """Calculate wire stub endpoint from a pin position and outward direction.

    Args:
        px, py: Pin position
        direction: Outward direction (0=RIGHT, 90=DOWN, 180=LEFT, 270=UP)
        length: Stub length in mm (default 7.62 = 3 coarse grid steps)

    Returns:
        (end_x, end_y) — the far end of the stub
    """
    dx, dy = direction_dx_dy(direction)
    return px + dx * length, py + dy * length

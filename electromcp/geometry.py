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


def pin_world_position(
    comp_x: float, comp_y: float, comp_rotation_deg: float,
    pin_local_x: float, pin_local_y: float,
    mirror: str = "",
) -> tuple[float, float]:
    """Convert pin local coordinates to schematic world coordinates.

    KiCad symbol pin coordinates use Y-up convention while the schematic
    uses Y-down.  The empirically verified transform is::

        world_x = comp_x + rotated_local_x
        world_y = comp_y - rotated_local_y   (Y-flip)

    where ``rotated_local`` applies the standard 2D rotation matrix.

    Mirror is applied before rotation:
    - ``"x"``: flip horizontally (negate local_x)
    - ``"y"``: flip vertically (negate local_y)
    """
    lx, ly = pin_local_x, pin_local_y
    if mirror == "x":
        lx = -lx
    elif mirror == "y":
        ly = -ly

    theta = math.radians(comp_rotation_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    rotated_x = lx * cos_t - ly * sin_t
    rotated_y = lx * sin_t + ly * cos_t
    return comp_x + rotated_x, comp_y - rotated_y


def extract_pins_from_lib_text(
    lib_text: str,
) -> list[tuple[str, str, float, float]]:
    """Extract pin info from a lib_symbol S-expression block.

    Returns a deduplicated list of ``(pin_number, pin_name, local_x, local_y)``
    tuples.  Only the first occurrence of each pin number is kept (handles
    multi-unit symbols that duplicate graphics).
    """
    import re

    pins: list[tuple[str, str, float, float]] = []
    seen: set[str] = set()

    # Iterate over each (pin ...) block
    for m in re.finditer(r"\(pin\s+\w+\s+\w+", lib_text):
        start = m.start()
        # Find the matching close paren
        depth = 0
        i = start
        while i < len(lib_text):
            if lib_text[i] == "(":
                depth += 1
            elif lib_text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        pin_block = lib_text[start : i + 1]

        at_m = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pin_block)
        num_m = re.search(r'\(number "([^"]*)"', pin_block)
        name_m = re.search(r'\(name "([^"]*)"', pin_block)
        if at_m and num_m:
            pin_num = num_m.group(1)
            if pin_num not in seen:
                seen.add(pin_num)
                pin_name = name_m.group(1) if name_m else ""
                pins.append((
                    pin_num,
                    pin_name,
                    float(at_m.group(1)),
                    float(at_m.group(2)),
                ))
    return pins


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

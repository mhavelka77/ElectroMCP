"""ElectroMCP — FastMCP server for KiCad 9 schematic design.

Provides MCP tools that let LLMs design KiCad schematics through a
place-wire-render-iterate workflow.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .geometry import snap_point_coarse, snap_point_fine
from .library import LibraryManager
from .reader import get_circuit_state, render_schematic, run_erc
from .writer import (
    SchematicModel,
    Component,
    Wire,
    NetLabel,
    Junction,
    PinEntry,
    PropertyEntry,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "ElectroMCP",
    instructions=(
        "KiCad 9 schematic design server. Place components, wire them, "
        "render to PNG, run ERC, and iterate until professional quality."
    ),
)

lib_manager = LibraryManager()

# Cache of open schematics -- keyed by absolute path
_models: dict[str, SchematicModel] = {}


def _get_model(schematic_path: str) -> SchematicModel:
    """Get or create a SchematicModel for the given path."""
    p = str(Path(schematic_path).resolve())
    if p not in _models:
        _models[p] = SchematicModel()
    return _models[p]


def _save(schematic_path: str, model: SchematicModel) -> None:
    """Write the model to disk."""
    model.write(schematic_path)


def _next_auto_ref(model: SchematicModel, prefix: str) -> str:
    """Generate the next available reference with the given prefix.

    Scans existing components in the model for references starting with
    *prefix* (e.g. ``"#PWR"`` or ``"#FLG"``), finds the highest numeric
    suffix, and returns the next one formatted with a zero-padded two-digit
    number (e.g. ``"#PWR01"``, ``"#FLG03"``).
    """
    existing: set[int] = set()
    prefix_len = len(prefix)
    for comp in model.components:
        ref = comp.reference
        if ref.startswith(prefix):
            try:
                existing.add(int(ref[prefix_len:]))
            except ValueError:
                pass
    n = 1
    while n in existing:
        n += 1
    return f"{prefix}{n:02d}"


def _count_pins_from_lib_symbol(lib_text: str) -> list[str]:
    """Extract pin numbers from a lib_symbol text block.

    Returns:
        Deduplicated list of pin number strings in the order they appear.
    """
    pin_numbers: list[str] = []
    for m in re.finditer(r'\(number "([^"]+)"', lib_text):
        num = m.group(1)
        if num not in pin_numbers:
            pin_numbers.append(num)
    return pin_numbers


# ---------------------------------------------------------------------------
# Startup — load system libraries
# ---------------------------------------------------------------------------

@mcp.resource("electromcp://status")
def server_status() -> str:
    """Server status and loaded library count."""
    return json.dumps({
        "status": "running",
        "libraries_loaded": len(lib_manager.libraries),
        "open_schematics": len(_models),
    })


# ---------------------------------------------------------------------------
# Core Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_circuit_state_tool(schematic_path: str) -> str:
    """Get the full state of a schematic as JSON.

    Returns all components with their exact pin positions (x, y, direction),
    all wires, labels, junctions, and power symbols. If the file doesn't
    exist yet, creates a blank schematic and returns empty state.

    **Pin positions are EXACT** — use them directly for wire endpoints.
    Pin direction indicates which way wires should extend FROM the pin:
    0=RIGHT, 90=DOWN, 180=LEFT, 270=UP.

    Coordinate system: millimeters, Y-axis points DOWN, origin at top-left.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        JSON with keys: root_uuid, components, wires, labels, junctions, power_symbols.
        Each component has: reference, value, lib_id, x, y, rotation, pins[].
        Each pin has: number, name, x, y, direction.
    """
    model = _get_model(schematic_path)
    p = Path(schematic_path)

    if not p.exists():
        _save(schematic_path, model)

    state = get_circuit_state(schematic_path)
    return json.dumps(state, indent=2)


@mcp.tool()
def render_schematic_view(schematic_path: str) -> str:
    """Render the schematic as a high-resolution PNG image.

    Returns a base64 data URI that can be displayed inline. The image is
    2400px wide — enough to see component labels, pin connections, and
    wire routing clearly.

    **USE THIS AFTER EVERY SIGNIFICANT CHANGE** to visually verify:
    - Labels aren't overlapping component bodies
    - Wires reach their intended pin endpoints (no small circles = unconnected)
    - Power symbols point the correct direction (VCC up, GND down)
    - Overall layout is clean and professional

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        Base64 data URI string: "data:image/png;base64,..."
    """
    png_bytes = render_schematic(schematic_path)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


@mcp.tool()
def run_erc_check(schematic_path: str) -> str:
    """Run KiCad's Electrical Rules Check (ERC) on the schematic.

    Returns violations as JSON. **Run this after wiring** to catch:
    - Unconnected pins (ERROR — wires don't reach pin endpoints)
    - Unconnected wire endpoints (ERROR — dangling wires)
    - Power pin not driven (WARNING — normal without PWR_FLAG, can ignore)
    - Duplicate references (ERROR)

    Goal: ZERO errors. Warnings about "power pin not driven" are acceptable.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        JSON with: violations[], error_count, warning_count, total.
    """
    result = run_erc(schematic_path)
    return json.dumps(result, indent=2)


@mcp.tool()
def search_symbols(query: str, limit: int = 20) -> str:
    """Search KiCad symbol libraries by name, description, or keywords.

    Use this to find the correct lib_id before placing a component.

    Examples:
        search_symbols("NE555") → finds Timer:NE555P, Timer:NE555D, etc.
        search_symbols("resistor") → finds Device:R, Device:R_Small, etc.
        search_symbols("LED") → finds Device:LED, Device:LED_Small, etc.
        search_symbols("capacitor") → finds Device:C, Device:C_Polarized, etc.

    Args:
        query: Search terms (name, description, or keywords).
        limit: Maximum results to return (default 20).

    Returns:
        JSON array of matches with: lib_name, symbol_name, full_id, description.
    """
    results = lib_manager.search(query, limit)
    return json.dumps([
        {
            "lib_name": r.lib_name,
            "symbol_name": r.symbol_name,
            "full_id": r.full_id,
            "description": r.description,
            "extends": r.extends,
        }
        for r in results
    ], indent=2)


@mcp.tool()
def register_library(name: str, path: str) -> str:
    """Register an external .kicad_sym symbol library file.

    After registering, symbols from this library can be found via
    search_symbols and placed with add_component.

    Args:
        name: Library name (used as prefix, e.g., "MyLib" → "MyLib:MySymbol").
        path: Absolute path to the .kicad_sym file.

    Returns:
        Confirmation message.
    """
    lib_manager.register_library(name, path)
    return json.dumps({"status": "ok", "library": name, "path": path})


# ---------------------------------------------------------------------------
# Placement Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def add_component(
    schematic_path: str,
    lib_id: str,
    x: float,
    y: float,
    reference: str,
    value: str,
    rotation: float = 0,
) -> str:
    """Place a component on the schematic.

    The component is snapped to the 2.54mm coarse grid. After placement,
    call get_circuit_state to see the exact pin positions for wiring.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        lib_id: Full symbol ID from search_symbols (e.g., "Device:R", "Timer:NE555P").
        x: X position in mm. Will be snapped to 2.54mm grid.
        y: Y position in mm. Will be snapped to 2.54mm grid.
        reference: Reference designator (e.g., "R1", "U1", "C1", "D1").
        value: Component value (e.g., "10k", "100nF", "NE555P").
        rotation: Rotation in degrees (0, 90, 180, 270). Default 0.

    Returns:
        JSON with placed position and pin info from the written file.

    Example:
        add_component("circuit.kicad_sch", "Device:R", 120, 50, "R1", "10k")
    """
    model = _get_model(schematic_path)

    # Snap to coarse grid
    sx, sy = snap_point_coarse(x, y)

    # Get lib_symbol text and inject
    lib_text = lib_manager.get_lib_symbol_text(lib_id)
    model.add_lib_symbol(lib_id, lib_text)

    # Determine pin numbers from the lib_symbol
    pin_numbers = _count_pins_from_lib_symbol(lib_text)

    comp = Component(
        lib_id=lib_id,
        x=sx,
        y=sy,
        rotation=rotation,
        reference=reference,
        value=value,
        pins=[PinEntry(number=pn) for pn in pin_numbers],
    )
    model.components.append(comp)

    _save(schematic_path, model)

    # Read back to get resolved pin positions
    state = get_circuit_state(schematic_path)

    # Find this component in state
    comp_state = None
    for c in state.get("components", []):
        if c["reference"] == reference:
            comp_state = c
            break

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "position": {"x": sx, "y": sy},
        "rotation": rotation,
        "component": comp_state,
    }, indent=2)


@mcp.tool()
def add_power_symbol(
    schematic_path: str,
    power_net: str,
    x: float,
    y: float,
    rotation: float = 0,
) -> str:
    """Place a power symbol (VCC, GND, +5V, +3V3, etc.) on the schematic.

    Power symbols are snapped to the 1.27mm FINE grid (not coarse!) because
    they often connect to pins on rotated components.

    **CRITICAL CONVENTIONS:**
    - VCC/+5V/+3V3: rotation=0 (arrow points UP) — place ABOVE the wire
    - GND: rotation=0 (triangle points DOWN) — place BELOW the wire

    The power symbol's pin is at its placement point (x, y). Connect a wire
    from that point to the target component pin.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        power_net: Power net name exactly as in library: "VCC", "GND", "+5V", "+3V3".
        x: X position in mm. Snapped to 1.27mm fine grid.
        y: Y position in mm. Snapped to 1.27mm fine grid.
        rotation: Rotation in degrees. Default 0.
            For VCC/+5V/+3V3: 0=up (normal), 180=down (inverted — rare).
            For GND: 0=down (normal), 180=up (inverted — rare).

    Returns:
        JSON with placed position and pin location.
    """
    model = _get_model(schematic_path)

    # Snap to fine grid
    sx, sy = snap_point_fine(x, y)

    # Get power symbol lib text
    lib_text = lib_manager.get_power_symbol_text(power_net)
    lib_id = f"power:{power_net}"
    model.add_lib_symbol(lib_id, lib_text)

    # Determine if this is a PWR_FLAG symbol (uses #FLG prefix)
    if power_net == "PWR_FLAG":
        pwr_ref = _next_auto_ref(model, "#FLG")
    else:
        pwr_ref = _next_auto_ref(model, "#PWR")

    comp = Component(
        lib_id=lib_id,
        x=sx,
        y=sy,
        rotation=rotation,
        reference=pwr_ref,
        value=power_net,
        is_power=True,
        pins=[PinEntry(number="1")],
    )

    # Power symbols need hidden Reference
    comp.properties = [
        PropertyEntry(
            name="Reference", value=pwr_ref,
            x=sx, y=sy - 3.81, hidden=True,
        ),
        PropertyEntry(
            name="Value", value=power_net,
            x=sx, y=sy + 3.556,
        ),
        PropertyEntry(
            name="Footprint", value="",
            x=sx, y=sy, hidden=True,
        ),
        PropertyEntry(
            name="Datasheet", value="",
            x=sx, y=sy, hidden=True,
        ),
        PropertyEntry(
            name="Description", value=f'Power symbol creates a global label with name \\"{power_net}\\"',
            x=sx, y=sy, hidden=True,
        ),
    ]

    model.components.append(comp)
    _save(schematic_path, model)

    state = get_circuit_state(schematic_path)
    pwr_state = None
    for ps in state.get("power_symbols", []):
        if ps["reference"] == pwr_ref:
            pwr_state = ps
            break

    return json.dumps({
        "status": "ok",
        "reference": pwr_ref,
        "power_net": power_net,
        "position": {"x": sx, "y": sy},
        "pin": pwr_state["pins"][0] if pwr_state and pwr_state.get("pins") else {"x": sx, "y": sy},
    }, indent=2)


@mcp.tool()
def add_wire(
    schematic_path: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> str:
    """Add a wire segment between two points.

    Wires are snapped to the 1.27mm fine grid. For best results, use EXACT
    pin coordinates from get_circuit_state — this ensures connections.

    **IMPORTANT:** Wires must be Manhattan (horizontal OR vertical). Diagonal
    wires are technically valid but look unprofessional. If you need to go
    from point A to point B that aren't aligned, use TWO wire segments
    (one horizontal, one vertical) meeting at a corner.

    **JUNCTION RULE:** If a wire T-intersects another wire (3-way connection),
    you MUST add a junction at the intersection point with add_junction.
    Without a junction, KiCad doesn't know the wires are connected.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        x1: Start X in mm. Use exact pin coordinate from get_circuit_state.
        y1: Start Y in mm. Use exact pin coordinate from get_circuit_state.
        x2: End X in mm.
        y2: End Y in mm.

    Returns:
        JSON confirmation with snapped coordinates.
    """
    model = _get_model(schematic_path)

    # Snap to fine grid
    sx1, sy1 = snap_point_fine(x1, y1)
    sx2, sy2 = snap_point_fine(x2, y2)

    wire = Wire(x1=sx1, y1=sy1, x2=sx2, y2=sy2)
    model.wires.append(wire)
    _save(schematic_path, model)

    return json.dumps({
        "status": "ok",
        "from": {"x": sx1, "y": sy1},
        "to": {"x": sx2, "y": sy2},
    }, indent=2)


@mcp.tool()
def add_net_label(
    schematic_path: str,
    net_name: str,
    x: float,
    y: float,
    rotation: float = 0,
) -> str:
    """Add a net label at a position.

    Net labels connect all wires/pins that touch the label's position
    and share the same net name. Use them for named signals like "OUT",
    "TRIG", etc. — especially for connections that would create messy
    long wires across the schematic.

    The label is placed at the exact (x, y) position, which must be on
    a wire or pin endpoint.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        net_name: The net name (e.g., "OUT", "CLK", "RESET").
        x: X position in mm.
        y: Y position in mm.
        rotation: Text rotation (0=horizontal pointing right, 180=left, 90=down, 270=up).

    Returns:
        JSON confirmation.
    """
    model = _get_model(schematic_path)

    sx, sy = snap_point_fine(x, y)
    label = NetLabel(name=net_name, x=sx, y=sy, rotation=rotation)
    model.labels.append(label)
    _save(schematic_path, model)

    return json.dumps({
        "status": "ok",
        "net_name": net_name,
        "position": {"x": sx, "y": sy},
    }, indent=2)


@mcp.tool()
def add_junction(schematic_path: str, x: float, y: float) -> str:
    """Add a junction dot at a wire intersection point.

    **REQUIRED** at every T-intersection where a wire branches off another
    wire. Without junctions, KiCad treats crossing wires as NOT connected.

    Place the junction at the exact intersection point — must be on the
    1.27mm fine grid.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        x: X position in mm.
        y: Y position in mm.

    Returns:
        JSON confirmation.
    """
    model = _get_model(schematic_path)

    sx, sy = snap_point_fine(x, y)
    junc = Junction(x=sx, y=sy)
    model.junctions.append(junc)
    _save(schematic_path, model)

    return json.dumps({
        "status": "ok",
        "position": {"x": sx, "y": sy},
    }, indent=2)


# ---------------------------------------------------------------------------
# Refinement Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def move_component(
    schematic_path: str,
    reference: str,
    x: float,
    y: float,
    rotation: float | None = None,
) -> str:
    """Move a component to a new position.

    **WARNING:** This does NOT move connected wires. After moving, you must
    delete old wires and re-wire using the new pin positions from
    get_circuit_state.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        reference: Component reference (e.g., "R1", "U1").
        x: New X position in mm. Snapped to 2.54mm coarse grid.
        y: New Y position in mm. Snapped to 2.54mm coarse grid.
        rotation: New rotation in degrees. If None, keeps current rotation.

    Returns:
        JSON with new position and updated pin positions.
    """
    model = _get_model(schematic_path)
    comp = model.find_component(reference)
    if comp is None:
        return json.dumps({"status": "error", "message": f"Component '{reference}' not found"})

    sx, sy = snap_point_coarse(x, y)
    comp.x = sx
    comp.y = sy
    if rotation is not None:
        comp.rotation = rotation

    # Clear custom properties so they regenerate at new position
    comp.properties = []

    _save(schematic_path, model)

    state = get_circuit_state(schematic_path)
    comp_state = None
    for c in state.get("components", []) + state.get("power_symbols", []):
        if c["reference"] == reference:
            comp_state = c
            break

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "position": {"x": sx, "y": sy},
        "component": comp_state,
    }, indent=2)


@mcp.tool()
def delete_wire(
    schematic_path: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> str:
    """Delete a specific wire segment.

    Uses fuzzy matching (1mm tolerance) to find the wire. The wire endpoints
    can be specified in either order.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        x1: Start X of wire to delete.
        y1: Start Y of wire to delete.
        x2: End X of wire to delete.
        y2: End Y of wire to delete.

    Returns:
        JSON with status ("ok" or "not_found").
    """
    model = _get_model(schematic_path)
    removed = model.remove_wire(x1, y1, x2, y2)
    if removed:
        _save(schematic_path, model)
        return json.dumps({"status": "ok", "message": "Wire deleted"})
    else:
        return json.dumps({"status": "not_found", "message": "No wire found matching those coordinates (1mm tolerance)"})


@mcp.tool()
def delete_all_wires(schematic_path: str) -> str:
    """Delete ALL wires from the schematic.

    Nuclear option for complete rewiring. Use when the wiring is too messy
    to fix incrementally. After calling this, use get_circuit_state to see
    pin positions and rewire everything.

    Also clears all junctions (they're meaningless without wires).

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        JSON with count of wires removed.
    """
    model = _get_model(schematic_path)
    wire_count = model.clear_wires()
    junc_count = len(model.junctions)
    model.junctions.clear()
    _save(schematic_path, model)

    return json.dumps({
        "status": "ok",
        "wires_removed": wire_count,
        "junctions_removed": junc_count,
    })


@mcp.tool()
def delete_component(schematic_path: str, reference: str) -> str:
    """Remove a component from the schematic.

    Does NOT remove connected wires — you may want to delete those too.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        reference: Component reference (e.g., "R1", "#PWR01").

    Returns:
        JSON with status.
    """
    model = _get_model(schematic_path)
    removed = model.remove_component(reference)
    if removed:
        _save(schematic_path, model)
        return json.dumps({"status": "ok", "message": f"Component '{reference}' deleted"})
    else:
        return json.dumps({"status": "not_found", "message": f"Component '{reference}' not found"})


@mcp.tool()
def move_property_label(
    schematic_path: str,
    reference: str,
    property_name: str,
    dx: float = 0,
    dy: float = 0,
    x: float | None = None,
    y: float | None = None,
) -> str:
    """Move a component's property label (Reference or Value) to a new position.

    Two modes:
    - **Offset mode** (dx/dy): Nudge the label from its current position by the
      given offsets. Example: dx=5 moves the label 5mm to the right.
    - **Absolute mode** (x/y): Set the label to an exact position on the schematic.
      Use get_circuit_state to read current property positions first.

    If both x/y and dx/dy are provided, absolute mode (x/y) takes precedence.

    Use this to fix overlapping labels. Check with render_schematic_view
    after adjusting.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        reference: Component reference (e.g., "R1").
        property_name: "Reference" or "Value".
        dx: Horizontal offset in mm (positive = right). Used in offset mode.
        dy: Vertical offset in mm (positive = down). Used in offset mode.
        x: Absolute X position in mm. If provided with y, uses absolute mode.
        y: Absolute Y position in mm. If provided with x, uses absolute mode.

    Returns:
        JSON confirmation with new label position.
    """
    model = _get_model(schematic_path)
    comp = model.find_component(reference)
    if comp is None:
        return json.dumps({"status": "error", "message": f"Component '{reference}' not found"})

    # Ensure properties are built
    if not comp.properties:
        comp.properties = comp.build_properties()

    for prop in comp.properties:
        if prop.name == property_name:
            if x is not None and y is not None:
                # Absolute mode
                prop.x = x
                prop.y = y
            else:
                # Offset mode
                prop.x += dx
                prop.y += dy
            _save(schematic_path, model)
            return json.dumps({
                "status": "ok",
                "property": property_name,
                "new_position": {"x": prop.x, "y": prop.y},
            })

    return json.dumps({"status": "error", "message": f"Property '{property_name}' not found on '{reference}'"})


@mcp.tool()
def rotate_component(
    schematic_path: str,
    reference: str,
    rotation: float,
) -> str:
    """Change a component's rotation without moving it.

    Essential for flipping power symbols, LEDs, and other directional components.
    After rotating, wires will be disconnected — re-wire using new pin
    positions from get_circuit_state.

    Common rotations:
    - 0: Default orientation
    - 90: Rotated 90° clockwise
    - 180: Flipped (upside down)
    - 270: Rotated 90° counter-clockwise

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        reference: Component reference (e.g., "D1", "#PWR03").
        rotation: New rotation in degrees (0, 90, 180, 270).

    Returns:
        JSON with updated pin positions.
    """
    model = _get_model(schematic_path)
    comp = model.find_component(reference)
    if comp is None:
        return json.dumps({"status": "error", "message": f"Component '{reference}' not found"})

    comp.rotation = rotation
    comp.properties = []  # Reset so they regenerate

    _save(schematic_path, model)

    state = get_circuit_state(schematic_path)
    comp_state = None
    for c in state.get("components", []) + state.get("power_symbols", []):
        if c["reference"] == reference:
            comp_state = c
            break

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "rotation": rotation,
        "component": comp_state,
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
    )
    count = lib_manager.load_system_libraries()
    logger.info("Loaded %d symbol libraries", count)
    mcp.run()


if __name__ == "__main__":
    main()

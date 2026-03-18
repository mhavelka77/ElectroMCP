"""ElectroMCP — FastMCP server for KiCad 9 schematic design.

Provides MCP tools that let LLMs design KiCad schematics through a
place-wire-render-iterate workflow.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage

from .geometry import (
    extract_pins_from_lib_text,
    pin_world_position,
    snap_fine,
    snap_point_coarse,
    snap_point_fine,
)
from .library import LibraryManager
from .reader import (
    _extract_lib_symbols_from_file,
    _extract_no_connects_from_file,
    _extract_passthrough_blocks,
    get_circuit_state,
    junction_to_model,
    label_to_model,
    render_schematic,
    run_erc,
    symbol_to_component,
    wire_to_model,
)
from .writer import (
    Component,
    Junction,
    NetLabel,
    NoConnect,
    PinEntry,
    PropertyEntry,
    SchematicModel,
    Wire,
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


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

def _load_model_from_file(schematic_path: str) -> SchematicModel:
    """Load a SchematicModel from an existing .kicad_sch file.

    Parses all known element types into the model and stores unrecognised
    top-level S-expression blocks as passthrough so they survive rewrites.
    """
    import skip

    path = Path(schematic_path)
    raw_text = path.read_text(encoding="utf-8")

    model = SchematicModel()

    # Root UUID
    m = re.search(r'\(uuid "([^"]+)"\)', raw_text[:1000])
    if m:
        model.root_uuid = m.group(1)

    # Paper size
    m = re.search(r'\(paper "([^"]+)"\)', raw_text[:1000])
    if m:
        model.paper = m.group(1)

    # Lib symbols — raw text blocks
    model.lib_symbol_texts = _extract_lib_symbols_from_file(raw_text)

    # Use kicad-skip for structured elements
    sch = skip.Schematic(str(path))

    # Components (including power symbols)
    if sch.symbol is not None:
        for sym in sch.symbol:
            try:
                comp = symbol_to_component(sym)
                model.components.append(comp)
            except Exception:
                logger.debug("Skipping unparseable symbol", exc_info=True)

    # Wires
    if sch.wire is not None:
        for w in sch.wire:
            wire = wire_to_model(w)
            if wire:
                model.wires.append(wire)

    # Labels
    if sch.label is not None:
        for lb in sch.label:
            label = label_to_model(lb)
            if label:
                model.labels.append(label)

    # Junctions
    if sch.junction is not None:
        for j in sch.junction:
            junc = junction_to_model(j)
            if junc:
                model.junctions.append(junc)

    # No-connects (regex from raw text — reliable regardless of kicad-skip)
    for x, y, nc_uuid in _extract_no_connects_from_file(raw_text):
        model.no_connects.append(NoConnect(x=x, y=y, uuid=nc_uuid))

    # Passthrough blocks (text, polyline, global_label, bus, etc.)
    model._passthrough_blocks = _extract_passthrough_blocks(raw_text)

    return model


def _get_model(schematic_path: str) -> SchematicModel:
    """Get or create a SchematicModel for the given path.

    If the file already exists but has no cached model, the file is parsed
    into a new model so that existing elements are preserved.
    """
    p = str(Path(schematic_path).resolve())
    if p not in _models:
        if Path(p).exists():
            try:
                _models[p] = _load_model_from_file(p)
                logger.info("Loaded existing schematic: %s", p)
            except Exception:
                logger.warning(
                    "Failed to load existing schematic, starting fresh: %s",
                    p, exc_info=True,
                )
                _models[p] = SchematicModel()
        else:
            _models[p] = SchematicModel()
    return _models[p]


def _save(schematic_path: str, model: SchematicModel) -> None:
    """Write the model to disk."""
    model.write(schematic_path)


def _next_auto_ref(model: SchematicModel, prefix: str) -> str:
    """Generate the next available reference with the given prefix."""
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
    """Extract pin numbers from a lib_symbol text block."""
    pin_numbers: list[str] = []
    for m in re.finditer(r'\(number "([^"]+)"', lib_text):
        num = m.group(1)
        if num not in pin_numbers:
            pin_numbers.append(num)
    return pin_numbers


def _compute_component_pins(
    lib_text: str, comp_x: float, comp_y: float, comp_rotation: float,
) -> list[dict]:
    """Compute world-space pin positions for a component from its lib_symbol."""
    raw_pins = extract_pins_from_lib_text(lib_text)
    pins: list[dict] = []
    for pin_num, pin_name, local_x, local_y in raw_pins:
        wx, wy = pin_world_position(comp_x, comp_y, comp_rotation, local_x, local_y)
        wx = snap_fine(wx)
        wy = snap_fine(wy)
        pins.append({
            "number": pin_num,
            "name": pin_name,
            "x": round(wx, 4),
            "y": round(wy, 4),
        })
    return pins


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
    all wires, labels, junctions, power symbols, and no-connect flags.
    If the file doesn't exist yet, creates a blank schematic and returns
    empty state.

    **Pin positions are EXACT** — use them directly for wire endpoints.
    Pin direction indicates which way wires should extend FROM the pin:
    0=RIGHT, 90=DOWN, 180=LEFT, 270=UP.

    Coordinate system: millimeters, Y-axis points DOWN, origin at top-left.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        JSON with keys: root_uuid, components, wires, labels, junctions,
        power_symbols, no_connects.
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
def render_schematic_view(
    schematic_path: str,
    output_path: str | None = None,
    width: int = 2400,
) -> list:
    """Render the schematic as a PNG image for visual verification.

    Returns the image as a native MCP image content block that the AI agent
    can see directly, plus a text block with metadata (file path, dimensions).

    **USE THIS AFTER EVERY SIGNIFICANT CHANGE** to visually verify:
    - Labels aren't overlapping component bodies
    - Wires reach their intended pin endpoints (no small circles = unconnected)
    - Power symbols point the correct direction (VCC up, GND down)
    - Overall layout is clean and professional

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        output_path: Optional path to save the PNG file. If not provided,
            a temporary file is created. The file persists for manual inspection.
        width: Output image width in pixels (default 2400). Use 800-1200 for
            quick checks, 2400 for detailed review.

    Returns:
        List containing an MCP Image content block and a text metadata block.
    """
    png_bytes = render_schematic(schematic_path, output_width=width)

    # Save to file (temp or user-specified)
    if output_path:
        save_path = Path(output_path)
        save_path.write_bytes(png_bytes)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="schematic_render_")
        import os
        os.close(fd)
        save_path = Path(tmp)
        save_path.write_bytes(png_bytes)

    size_kb = len(png_bytes) / 1024
    metadata = json.dumps({
        "status": "ok",
        "image_path": str(save_path),
        "size_kb": round(size_kb, 1),
        "width": width,
    })

    return [MCPImage(data=png_bytes, format="png"), metadata]


@mcp.tool()
def run_erc_check(schematic_path: str) -> str:
    """Run KiCad's Electrical Rules Check (ERC) on the schematic.

    Returns violations as JSON. **Run this after wiring** to catch:
    - Unconnected pins (ERROR — wires don't reach pin endpoints)
    - Unconnected wire endpoints (ERROR — dangling wires)
    - Power pin not driven (WARNING — normal without PWR_FLAG, can ignore)
    - Duplicate references (ERROR)

    Goal: ZERO errors. Warnings about "power pin not driven" are acceptable.

    Violation positions are in mm, matching all other tool coordinates.

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
    the response includes exact pin positions for wiring (computed from
    the symbol definition, correct even for rotated components).

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        lib_id: Full symbol ID from search_symbols (e.g., "Device:R", "Timer:NE555P").
        x: X position in mm. Will be snapped to 2.54mm grid.
        y: Y position in mm. Will be snapped to 2.54mm grid.
        reference: Reference designator (e.g., "R1", "U1", "C1", "D1").
        value: Component value (e.g., "10k", "100nF", "NE555P").
        rotation: Rotation in degrees (0, 90, 180, 270). Default 0.

    Returns:
        JSON with placed position, pin info, and reference.

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

    # Compute pin positions from lib data (correct for rotated components)
    pins = _compute_component_pins(lib_text, sx, sy, rotation)

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "position": {"x": sx, "y": sy},
        "rotation": rotation,
        "pins": pins,
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

    Returns:
        JSON with placed position, assigned reference, and pin location.
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

    return json.dumps({
        "status": "ok",
        "reference": pwr_ref,
        "power_net": power_net,
        "position": {"x": sx, "y": sy},
        "pin": {"x": sx, "y": sy},
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


@mcp.tool()
def add_no_connect(schematic_path: str, x: float, y: float) -> str:
    """Place a no-connect (X) flag on an unused pin.

    Essential for MCU-based schematics where many pins are unused.
    The no-connect flag tells KiCad's ERC that the pin is intentionally
    unconnected.

    Snapped to the 1.27mm fine grid. Place at the EXACT pin position
    from get_circuit_state.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        x: X position in mm (pin position).
        y: Y position in mm (pin position).

    Returns:
        JSON confirmation with snapped position.
    """
    model = _get_model(schematic_path)

    sx, sy = snap_point_fine(x, y)
    nc = NoConnect(x=sx, y=sy)
    model.no_connects.append(nc)
    _save(schematic_path, model)

    return json.dumps({
        "status": "ok",
        "position": {"x": sx, "y": sy},
    }, indent=2)


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------

@mcp.tool()
def add_multiple(schematic_path: str, items: str) -> str:
    """Add multiple elements in a single operation (one file write).

    Much more efficient than individual tool calls when adding many elements.
    Accepts a JSON array of items, each with a "type" key and type-specific
    fields. All items are processed and the file is written ONCE at the end.

    Supported item types:

    - ``{"type": "wire", "x1": ..., "y1": ..., "x2": ..., "y2": ...}``
    - ``{"type": "no_connect", "x": ..., "y": ...}``
    - ``{"type": "label", "name": "SIG", "x": ..., "y": ..., "rotation": 0}``
    - ``{"type": "junction", "x": ..., "y": ...}``
    - ``{"type": "power_symbol", "net": "VCC", "x": ..., "y": ..., "rotation": 0}``

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        items: JSON string — array of item objects.

    Returns:
        JSON summary with counts of items added and any errors.
    """
    model = _get_model(schematic_path)

    try:
        item_list = json.loads(items)
    except json.JSONDecodeError as e:
        return json.dumps({"status": "error", "message": f"Invalid JSON: {e}"})

    if not isinstance(item_list, list):
        return json.dumps({"status": "error", "message": "items must be a JSON array"})

    counts: dict[str, int] = {}
    errors: list[str] = []

    for i, item in enumerate(item_list):
        item_type = item.get("type", "")
        try:
            if item_type == "wire":
                sx1, sy1 = snap_point_fine(item["x1"], item["y1"])
                sx2, sy2 = snap_point_fine(item["x2"], item["y2"])
                model.wires.append(Wire(x1=sx1, y1=sy1, x2=sx2, y2=sy2))
                counts["wire"] = counts.get("wire", 0) + 1

            elif item_type == "no_connect":
                sx, sy = snap_point_fine(item["x"], item["y"])
                model.no_connects.append(NoConnect(x=sx, y=sy))
                counts["no_connect"] = counts.get("no_connect", 0) + 1

            elif item_type == "label":
                sx, sy = snap_point_fine(item["x"], item["y"])
                rotation = item.get("rotation", 0)
                model.labels.append(NetLabel(name=item["name"], x=sx, y=sy, rotation=rotation))
                counts["label"] = counts.get("label", 0) + 1

            elif item_type == "junction":
                sx, sy = snap_point_fine(item["x"], item["y"])
                model.junctions.append(Junction(x=sx, y=sy))
                counts["junction"] = counts.get("junction", 0) + 1

            elif item_type == "power_symbol":
                sx, sy = snap_point_fine(item["x"], item["y"])
                power_net = item["net"]
                rotation = item.get("rotation", 0)

                lib_text = lib_manager.get_power_symbol_text(power_net)
                lib_id = f"power:{power_net}"
                model.add_lib_symbol(lib_id, lib_text)

                if power_net == "PWR_FLAG":
                    pwr_ref = _next_auto_ref(model, "#FLG")
                else:
                    pwr_ref = _next_auto_ref(model, "#PWR")

                pwr_comp = Component(
                    lib_id=lib_id, x=sx, y=sy, rotation=rotation,
                    reference=pwr_ref, value=power_net, is_power=True,
                    pins=[PinEntry(number="1")],
                )
                pwr_comp.properties = [
                    PropertyEntry(name="Reference", value=pwr_ref, x=sx, y=sy - 3.81, hidden=True),
                    PropertyEntry(name="Value", value=power_net, x=sx, y=sy + 3.556),
                    PropertyEntry(name="Footprint", value="", x=sx, y=sy, hidden=True),
                    PropertyEntry(name="Datasheet", value="", x=sx, y=sy, hidden=True),
                    PropertyEntry(
                        name="Description",
                        value=f'Power symbol creates a global label with name \\"{power_net}\\"',
                        x=sx, y=sy, hidden=True,
                    ),
                ]
                model.components.append(pwr_comp)
                counts["power_symbol"] = counts.get("power_symbol", 0) + 1

            else:
                errors.append(f"Item {i}: unknown type '{item_type}'")

        except KeyError as e:
            errors.append(f"Item {i} ({item_type}): missing field {e}")
        except Exception as e:
            errors.append(f"Item {i} ({item_type}): {e}")

    _save(schematic_path, model)

    result: dict = {"status": "ok", "added": counts}
    if errors:
        result["errors"] = errors
    return json.dumps(result, indent=2)


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

    # Compute pins from lib data
    lib_text = model.lib_symbol_texts.get(comp.lib_id)
    pins = _compute_component_pins(lib_text, sx, sy, comp.rotation) if lib_text else []

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "position": {"x": sx, "y": sy},
        "pins": pins,
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

    Works for regular components AND power symbols (e.g., "#PWR01", "#FLG01").
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
def delete_no_connect(schematic_path: str, x: float, y: float) -> str:
    """Remove a no-connect flag near the given position.

    Uses 1mm tolerance for fuzzy matching.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        x: X position in mm (near the no-connect to remove).
        y: Y position in mm.

    Returns:
        JSON with status ("ok" or "not_found").
    """
    model = _get_model(schematic_path)
    removed = model.remove_no_connect(x, y)
    if removed:
        _save(schematic_path, model)
        return json.dumps({"status": "ok", "message": "No-connect removed"})
    else:
        return json.dumps({"status": "not_found", "message": "No no-connect found near that position (1mm tolerance)"})


@mcp.tool()
def delete_label(
    schematic_path: str,
    net_name: str,
    x: float | None = None,
    y: float | None = None,
) -> str:
    """Remove a net label by name and optional position.

    If x and y are provided, only removes the label at that position.
    If only net_name is provided, removes the first label with that name.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        net_name: Name of the net label to remove.
        x: Optional X position for precise matching (1mm tolerance).
        y: Optional Y position for precise matching (1mm tolerance).

    Returns:
        JSON with status.
    """
    model = _get_model(schematic_path)
    removed = model.remove_label(net_name, x, y)
    if removed:
        _save(schematic_path, model)
        return json.dumps({"status": "ok", "message": f"Label '{net_name}' removed"})
    else:
        return json.dumps({"status": "not_found", "message": f"Label '{net_name}' not found"})


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

    # Compute pins from lib data
    lib_text = model.lib_symbol_texts.get(comp.lib_id)
    pins = _compute_component_pins(lib_text, comp.x, comp.y, rotation) if lib_text else []

    return json.dumps({
        "status": "ok",
        "reference": reference,
        "rotation": rotation,
        "pins": pins,
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

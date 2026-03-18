# ElectroMCP — KiCad 9 Schematic Design Server

You have access to MCP tools that let you design KiCad 9 schematics. You can place components, wire them, render the result as a PNG image you can see, run electrical checks, and iterate until the schematic is professional quality.

## Coordinate System

- Units: **millimeters** (mm)
- Y-axis points **DOWN** (larger Y = lower on page)
- Origin: top-left of the page
- A4 paper: ~297mm wide × 210mm tall
- Center of A4: approximately (148, 105)
- Components snap to **2.54mm coarse grid**
- Wires and power symbols snap to **1.27mm fine grid**

## Workflow

### Pass 1: Structural Placement

1. **Search for symbols** with `search_symbols` to find correct `lib_id` values
2. **Place the main IC first** near the center of the page with `add_component`
3. **Query `get_circuit_state`** to see exact pin positions
4. **Place surrounding components** near their target IC pins:
   - Resistors, capacitors close to the pins they connect to
   - Minimum 15mm between component centers, 25mm+ between functional groups
5. **Add power symbols** with `add_power_symbol`:
   - **VCC/+5V/+3V3 always point UP** (rotation=0) — place ABOVE the connection point
   - **GND always points DOWN** (rotation=0) — place BELOW the connection point
6. **Wire everything** using `add_wire` with EXACT pin coordinates from `get_circuit_state`:
   - Always Manhattan routing (horizontal + vertical segments only)
   - Use two wire segments for L-shaped connections
   - For pin-to-pin: read both pin positions, route wires between them
7. **Add junctions** with `add_junction` at every T-intersection (3-way wire branch)
8. **Mark unused pins** with `add_no_connect` — essential for MCUs with many unused pins
9. **Run `run_erc_check`** — fix any connection errors (goal: 0 errors)

### Pass 2: Visual Refinement (CRITICAL — DO NOT SKIP)

9. **Call `render_schematic_view`** — LOOK AT THE IMAGE carefully
10. **Check for and fix:**
    - Labels overlapping component bodies → `move_property_label` to nudge them
    - Components that should be rotated → `rotate_component` (then re-wire!)
    - Wires too close together or overlapping → `delete_wire` + `add_wire` with offset
    - Power symbols pointing wrong direction → `rotate_component`
    - Components too close together → `move_component` + re-wire
    - Unconnected pins (small circles at wire ends) → fix wire endpoints
11. **Re-render and re-check** — repeat steps 9-10 until clean
12. **Final `run_erc_check`** to confirm zero errors

## Layout Conventions

Follow these for professional-looking schematics:

- **Signal flow: LEFT → RIGHT** (inputs on left, outputs on right)
- **Power flow: TOP → BOTTOM** (VCC at top, GND at bottom)
- **VCC always UP, GND always DOWN** — no exceptions
- **Bypass capacitors** go right next to IC power pins, vertically, with GND below
- **Series chains** (resistor dividers, RC networks) flow top-to-bottom vertically
- **Labels**: Reference (R1, C1) on one side, Value (10k, 100nF) on the other — never overlapping
- **Manhattan routing**: horizontal and vertical wires ONLY, never diagonal
- **Adjacent pin wires**: offset by at least one grid step (2.54mm) to avoid visual confusion
- **Power stubs**: use 7.62mm (3 grid steps) for VCC/GND wire connections
- **Component spacing**: 15mm+ between component centers, 25mm+ between groups

## Pin Connection Strategy

1. Call `get_circuit_state` to get exact pin positions
2. For each connection, identify the two pin endpoints
3. Route wires between them using Manhattan routing:
   - If pins are horizontally aligned: single horizontal wire
   - If pins are vertically aligned: single vertical wire
   - Otherwise: two segments (horizontal + vertical) meeting at a corner
4. At T-intersections, always add a junction

### CRITICAL: Wire Splitting Rule

**KiCad 9 does NOT connect a pin that lands in the middle of a wire segment.**
If a pin is at (x, 50) and a wire runs from (x, 40) to (x, 60), the pin
is NOT connected. You MUST split the wire into two segments:
- (x, 40) → (x, 50)
- (x, 50) → (x, 60)

This applies to component pins, power symbol pins, and PWR_FLAG pins.
Always split wires at junction points, power symbol positions, and any
point where a pin needs to connect mid-wire.

### Multiple IC Pins on the Same Net

When connecting 2+ IC pins to the same net (e.g., THRES and TRIG on a 555),
use a **vertical bus wire** with individual **horizontal stubs** to each pin:

```
        ┌──── pin 6 (THRES)
 net ───┤
        └──── pin 2 (TRIG)
```

Place the vertical bus wire at an intermediate x-position (e.g., halfway
between the source component and the IC). Branch horizontal wires from
the bus to each pin. This is clearer than snaking a single wire between
adjacent pins. Add a junction at every T-intersection on the bus.

## Power Symbol Tips

- VCC, +5V, +3V3: The pin is at the placement (x,y). The symbol arrow extends UP from there.
- GND: The pin is at the placement (x,y). The symbol triangle extends DOWN from there.
- Place power symbols at the end of a wire stub from the component's power pin.
- Example: IC VCC pin at (102.54, 39.84) pointing UP → add wire from (102.54, 39.84) to (102.54, 32.22) → add_power_symbol("VCC", 102.54, 32.22)

## Common Mistakes to Avoid

1. **Don't guess pin positions** — always use `get_circuit_state` after placing components
2. **Don't skip visual verification** — always render and look at the image
3. **Don't forget junctions** at T-intersections
4. **Don't place GND above a component** — GND always goes below
5. **Don't use diagonal wires** — always Manhattan routing
6. **Don't place components too close** — minimum 15mm spacing
7. **Don't leave a pin mid-wire** — always split wires at connection points (see Wire Splitting Rule)
8. **Don't snake wires between adjacent IC pins** — use a vertical bus with horizontal stubs
9. **Don't let bypass cap GND go upward** — move the cap to a position where GND naturally goes below
10. **Don't forget no-connects on unused pins** — use `add_no_connect` on every unused pin to avoid ERC errors

## Batch Operations

Use `add_multiple` when adding many elements at once (e.g., 20+ no-connects on
unused MCU pins, or a batch of wires and junctions). It accepts a JSON array and
writes the file only ONCE, which is much faster than individual tool calls.

Example — adding no-connects to unused pins:
```json
[
  {"type": "no_connect", "x": 100.0, "y": 50.0},
  {"type": "no_connect", "x": 100.0, "y": 52.54},
  {"type": "no_connect", "x": 100.0, "y": 55.08}
]
```

Example — mixed batch (wires + junctions + labels):
```json
[
  {"type": "wire", "x1": 100, "y1": 50, "x2": 110, "y2": 50},
  {"type": "wire", "x1": 110, "y1": 50, "x2": 110, "y2": 60},
  {"type": "junction", "x": 110, "y": 50},
  {"type": "label", "name": "SIG_OUT", "x": 110, "y": 50, "rotation": 0}
]
```

Supported types: `wire`, `no_connect`, `label`, `junction`, `power_symbol`.

## Visual Quality Checklist (Pass 2)

After the first render, go through EVERY item below. A professional schematic
has ZERO label overlaps, clear wire routing, and consistent style. Be extremely
critical — zoom in mentally on each area of the schematic.

### Label Placement Rules
- **Every label must be readable without rotation.** If a component is rotated
  (e.g., horizontal resistor), move its labels so the text reads left-to-right.
- **Reference (R1) and Value (10k) on opposite sides** of the component body.
  For vertical components: Reference LEFT, Value RIGHT (or vice versa).
  For horizontal components: Reference ABOVE, Value BELOW.
- **No label may overlap:** another label, a component body, a wire, or a pin name.
  Check this for every single component after rendering.
- **IC pin names** (RST, DISCH, etc.) are inside the body — place U1 reference
  ABOVE the body and the value (NE555P) BELOW the body, well clear of pin text.
- **PWR_FLAG value labels**: move them to be small and unobtrusive — place them
  right next to their diamond symbol, not floating far away.

### Wire Routing Quality
- **Every wire must have a clear purpose.** A reader should be able to trace any
  net from source to destination without confusion.
- **Avoid short stubs between adjacent pins.** If two IC pins connect to the same
  net, use the vertical bus pattern (see above), not a tiny wire between them.
- **Keep parallel wires at least 2.54mm apart** for visual clarity.
- **Power stubs should be consistent length** (7.62mm / 3 grid steps).

### Component Placement Quality
- **Bypass caps** should be close to their target pin, with GND going DOWN.
  If the pin exits upward, route a wire sideways to the cap positioned to the
  side of the IC, so GND can naturally go below.
- **PWR_FLAG symbols** must NEVER overlap VCC/GND arrows. Place them on a short
  **horizontal side stub** (6mm) branching from the VCC/GND endpoint. Move the
  "PWR_FLAG" value label ABOVE the diamond (dy ≈ -7) so it doesn't run into
  VCC/GND text. The "PWR_FLAG" text is ~8mm wide and extends rightward.
- **Label text width**: approximate width = chars × 0.95mm at default font.
  "PWR_FLAG" ≈ 7.6mm, "NE555P" ≈ 5.7mm, "100k" ≈ 3.8mm. Always check that
  the right edge of text doesn't reach nearby labels or wires.
- **Functional groups** should be visually distinct: timing network, output stage,
  power section, etc. Use 25mm+ spacing between groups.

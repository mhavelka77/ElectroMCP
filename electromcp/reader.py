"""Read-only schematic analysis via kicad-skip.

Uses kicad-skip ONLY for reading — never for writing.
Resolves pin positions, extracts component state, etc.
Also provides helpers for loading an existing .kicad_sch file into a
SchematicModel so that tool mutations preserve all existing elements.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .geometry import (
    extract_pins_from_lib_text,
    outward_direction,
    pin_world_position,
    snap_fine,
)

logger = logging.getLogger(__name__)


def _find_kicad_cli() -> str:
    """Find the kicad-cli binary, checking common locations.

    Checks the KICAD_CLI_PATH environment variable first, then PATH,
    then platform-specific default install locations.

    Returns:
        Absolute path to the kicad-cli executable.

    Raises:
        FileNotFoundError: If kicad-cli cannot be found anywhere.
    """
    # Environment variable override
    env_path = os.environ.get("KICAD_CLI_PATH")
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        raise FileNotFoundError(
            f"KICAD_CLI_PATH is set to '{env_path}' but the file does not exist."
        )

    # Check PATH
    found = shutil.which("kicad-cli")
    if found:
        return found

    # Platform-specific defaults
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS
        "/usr/bin/kicad-cli",  # Linux
        "/usr/local/bin/kicad-cli",  # Linux alt
        r"C:\Program Files\KiCad\bin\kicad-cli.exe",  # Windows
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    raise FileNotFoundError(
        "kicad-cli not found. Install KiCad 9 or add kicad-cli to PATH. "
        "You can also set the KICAD_CLI_PATH environment variable."
    )


KICAD_CLI = _find_kicad_cli()


# ---------------------------------------------------------------------------
# Pin position computation from lib_symbol data
# ---------------------------------------------------------------------------

def _compute_pin_positions(
    lib_symbol_text: str | None,
    comp_x: float,
    comp_y: float,
    comp_rotation: float,
    mirror: str = "",
) -> list[dict]:
    """Compute world-space pin positions from lib_symbol text.

    This replaces kicad-skip's pin location data which is incorrect
    for rotated components.

    Returns:
        List of dicts with keys: number, name, x, y, direction.
    """
    if not lib_symbol_text:
        return []

    raw_pins = extract_pins_from_lib_text(lib_symbol_text)
    pins: list[dict] = []
    for pin_num, pin_name, local_x, local_y in raw_pins:
        wx, wy = pin_world_position(
            comp_x, comp_y, comp_rotation, local_x, local_y, mirror,
        )
        # Snap to fine grid so positions match wire endpoints
        wx = snap_fine(wx)
        wy = snap_fine(wy)
        pins.append({
            "number": pin_num,
            "name": pin_name,
            "x": round(wx, 4),
            "y": round(wy, 4),
            "direction": 0,  # simplified — direction depends on pin rotation + comp rotation
        })
    return pins


# ---------------------------------------------------------------------------
# kicad-skip-based state reading
# ---------------------------------------------------------------------------

def _parse_symbol(sym: object, lib_texts: dict[str, str] | None = None) -> dict:
    """Extract component data from a kicad-skip symbol object.

    If *lib_texts* is provided (mapping lib_id → raw lib_symbol text),
    pin positions are computed from the lib data instead of relying on
    kicad-skip (which gets rotated components wrong).
    """
    try:
        ref = str(sym.property.Reference.value)
    except Exception:
        ref = "?"

    try:
        val = str(sym.property.Value.value)
    except Exception:
        val = "?"

    try:
        lib_id = str(sym.lib_id.value)
    except Exception:
        lib_id = "?"

    try:
        at_val = sym.at.value
        sx = float(at_val[0])
        sy = float(at_val[1])
        sr = float(at_val[2]) if len(at_val) > 2 else 0.0
    except Exception:
        sx, sy, sr = 0.0, 0.0, 0.0

    # Prefer our own pin computation over kicad-skip
    if lib_texts and lib_id in lib_texts:
        pins = _compute_pin_positions(lib_texts[lib_id], sx, sy, sr)
    else:
        pins = _parse_pins_from_skip(sym, sx, sy)

    properties = _parse_properties(sym)

    return {
        "reference": ref,
        "value": val,
        "lib_id": lib_id,
        "x": round(sx, 4),
        "y": round(sy, 4),
        "rotation": sr,
        "pins": pins,
        "properties": properties,
    }


def _parse_pins_from_skip(sym: object, default_x: float, default_y: float) -> list[dict]:
    """Fallback: extract pin data from a kicad-skip symbol object."""
    pins: list[dict] = []
    if sym.pin is None:
        return pins

    for p in sym.pin:
        pin_num = str(p.number) if hasattr(p, 'number') else "?"
        try:
            pin_name = str(p.name) if p.name else ""
        except Exception:
            pin_name = ""

        px, py = default_x, default_y
        direction = 0

        if hasattr(p, 'location') and p.location is not None:
            loc = p.location
            px = float(loc.x)
            py = float(loc.y)
            raw_rot = float(loc.rotation) if hasattr(loc, 'rotation') else 0.0
            direction = outward_direction(raw_rot)

        pins.append({
            "number": pin_num,
            "name": pin_name,
            "x": round(px, 4),
            "y": round(py, 4),
            "direction": direction,
        })

    return pins


def _parse_properties(sym: object) -> list[dict]:
    """Extract visible property positions (Reference, Value) from a symbol."""
    properties: list[dict] = []
    for prop_name in ("Reference", "Value"):
        try:
            prop = getattr(sym.property, prop_name, None)
            if prop is not None and hasattr(prop, 'at') and prop.at is not None:
                prop_at = prop.at.value
                prop_val = str(prop.value) if hasattr(prop, 'value') else ""
                properties.append({
                    "name": prop_name,
                    "value": prop_val,
                    "x": round(float(prop_at[0]), 4),
                    "y": round(float(prop_at[1]), 4),
                    "rotation": float(prop_at[2]) if len(prop_at) > 2 else 0,
                })
        except Exception:
            pass
    return properties


def get_circuit_state(schematic_path: str) -> dict:
    """Read the schematic and return full circuit state as a dict.

    Returns:
        Dict with keys: root_uuid, components, wires, labels, junctions,
        power_symbols, no_connects.
    """
    import skip

    path = Path(schematic_path)
    if not path.exists():
        return {
            "components": [],
            "wires": [],
            "labels": [],
            "junctions": [],
            "power_symbols": [],
            "no_connects": [],
        }

    # Extract lib_symbol texts for pin position computation
    raw_text = path.read_text(encoding="utf-8")
    lib_texts = _extract_lib_symbols_from_file(raw_text)

    sch = skip.Schematic(str(path))
    root_uuid = str(sch.uuid.value) if hasattr(sch.uuid, 'value') else str(sch.uuid)

    components: list[dict] = []
    power_symbols: list[dict] = []

    if sch.symbol is not None:
        for sym in sch.symbol:
            entry = _parse_symbol(sym, lib_texts)
            if entry["reference"].startswith("#PWR") or entry["reference"].startswith("#FLG"):
                power_symbols.append(entry)
            else:
                components.append(entry)

    wires: list[dict] = []
    if sch.wire is not None:
        for w in sch.wire:
            try:
                pts = w.pts.xy
                if len(pts) >= 2:
                    p0 = pts[0].value
                    p1 = pts[1].value
                    wires.append({
                        "x1": round(float(p0[0]), 4),
                        "y1": round(float(p0[1]), 4),
                        "x2": round(float(p1[0]), 4),
                        "y2": round(float(p1[1]), 4),
                    })
            except Exception:
                pass

    labels: list[dict] = []
    if sch.label is not None:
        for lb in sch.label:
            try:
                name = str(lb.value) if hasattr(lb, 'value') else "?"
                at_val = lb.at.value
                labels.append({
                    "name": name,
                    "x": round(float(at_val[0]), 4),
                    "y": round(float(at_val[1]), 4),
                    "rotation": float(at_val[2]) if len(at_val) > 2 else 0.0,
                })
            except Exception:
                pass

    junctions: list[dict] = []
    if sch.junction is not None:
        for j in sch.junction:
            try:
                at_val = j.at.value
                junctions.append({
                    "x": round(float(at_val[0]), 4),
                    "y": round(float(at_val[1]), 4),
                })
            except Exception:
                pass

    # Parse no_connects
    no_connects: list[dict] = []
    if hasattr(sch, 'no_connect') and sch.no_connect is not None:
        for nc in sch.no_connect:
            try:
                at_val = nc.at.value
                no_connects.append({
                    "x": round(float(at_val[0]), 4),
                    "y": round(float(at_val[1]), 4),
                })
            except Exception:
                pass

    # Fallback: parse no_connects from raw text if kicad-skip didn't find them
    if not no_connects:
        for m in re.finditer(
            r'\(no_connect\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)', raw_text,
        ):
            no_connects.append({
                "x": round(float(m.group(1)), 4),
                "y": round(float(m.group(2)), 4),
            })

    return {
        "root_uuid": root_uuid,
        "components": components,
        "wires": wires,
        "labels": labels,
        "junctions": junctions,
        "power_symbols": power_symbols,
        "no_connects": no_connects,
    }


# ---------------------------------------------------------------------------
# File loading helpers — used by server._load_model_from_file
# ---------------------------------------------------------------------------

def _extract_lib_symbols_from_file(raw_text: str) -> dict[str, str]:
    """Extract lib_symbol blocks from raw .kicad_sch text.

    Returns a dict mapping lib_id (e.g. "Device:R") to the raw S-expression
    text of that symbol definition (WITHOUT leading/trailing whitespace,
    at the indent level used inside the lib_symbols section).
    """
    result: dict[str, str] = {}

    # Find the (lib_symbols ...) section
    ls_match = re.search(r'\(lib_symbols\b', raw_text)
    if not ls_match:
        return result

    # Find each top-level (symbol "LIB:NAME" ...) inside lib_symbols
    start = ls_match.start()
    # Find the end of lib_symbols by counting parens
    depth = 0
    i = start
    while i < len(raw_text):
        if raw_text[i] == '(':
            depth += 1
        elif raw_text[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    lib_section = raw_text[start:i + 1]

    # Extract each symbol block within the lib_symbols section
    for m in re.finditer(r'\n\t\t\(symbol "([^"]+)"', lib_section):
        sym_name = m.group(1)
        # Skip sub-symbols (name_N_N pattern)
        if re.search(r'_\d+_\d+$', sym_name):
            continue

        block_start = m.start() + 1  # skip the leading newline
        # Count parens to find end of this symbol block
        d = 0
        j = block_start
        while j < len(lib_section):
            if lib_section[j] == '(':
                d += 1
            elif lib_section[j] == ')':
                d -= 1
                if d == 0:
                    break
            j += 1
        block = lib_section[block_start:j + 1].strip()
        result[sym_name] = block

    return result


def _extract_no_connects_from_file(
    raw_text: str,
) -> list[tuple[float, float, str]]:
    """Extract (x, y, uuid) tuples for no_connect elements from raw text."""
    results: list[tuple[float, float, str]] = []
    for m in re.finditer(
        r'\(no_connect\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)\s+\(uuid "([^"]+)"\)',
        raw_text,
    ):
        results.append((float(m.group(1)), float(m.group(2)), m.group(3)))
    return results


_KNOWN_TOP_LEVEL = frozenset({
    "version", "generator", "generator_version", "uuid", "paper",
    "lib_symbols", "symbol", "wire", "label", "junction", "no_connect",
    "text", "sheet_instances",
})


def _extract_text_notes_from_file(
    raw_text: str,
) -> list[tuple[str, float, float, float, str]]:
    """Extract (text, x, y, font_size, uuid) for text notes from raw file."""
    results: list[tuple[str, float, float, float, str]] = []
    for m in re.finditer(
        r'\(text "([^"]*(?:\\.[^"]*)*)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)',
        raw_text,
    ):
        text_val = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
        x = float(m.group(2))
        y = float(m.group(3))
        # Try to find font size and uuid nearby
        rest = raw_text[m.start():m.start() + 500]
        size_m = re.search(r'\(size\s+([-\d.]+)', rest)
        font_size = float(size_m.group(1)) if size_m else 1.27
        uuid_m = re.search(r'\(uuid "([^"]+)"\)', rest)
        uid = uuid_m.group(1) if uuid_m else ""
        results.append((text_val, x, y, font_size, uid))
    return results


def _extract_passthrough_blocks(raw_text: str) -> list[str]:
    """Extract top-level S-expression blocks not tracked by the model.

    These are preserved verbatim across file rewrites.  Includes elements
    like ``(text ...)``, ``(polyline ...)``, ``(global_label ...)``,
    ``(bus ...)``, ``(title_block ...)``, etc.
    """
    blocks: list[str] = []

    # Find the start of the kicad_sch body
    ks_match = re.search(r'\(kicad_sch\b', raw_text)
    if not ks_match:
        return blocks

    i = ks_match.end()
    end = len(raw_text)

    while i < end:
        # Skip whitespace
        while i < end and raw_text[i] in ' \t\n\r':
            i += 1
        if i >= end or raw_text[i] == ')':
            break

        if raw_text[i] == '(':
            block_start = i
            # Read block type
            j = i + 1
            while j < end and raw_text[j] not in ' \t\n\r()':
                j += 1
            block_type = raw_text[i + 1:j]

            # Count parens to find end
            depth = 1
            k = i + 1
            while k < end and depth > 0:
                ch = raw_text[k]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                elif ch == '"':
                    k += 1
                    while k < end and raw_text[k] != '"':
                        if raw_text[k] == '\\':
                            k += 1
                        k += 1
                k += 1

            block_text = raw_text[block_start:k]

            if block_type not in _KNOWN_TOP_LEVEL:
                blocks.append(block_text)

            i = k
        else:
            i += 1

    return blocks


def symbol_to_component(skip_sym: object) -> "Component":
    """Convert a kicad-skip symbol object to a writer.Component."""
    from .writer import Component, PinEntry, PropertyEntry

    try:
        lib_id = str(skip_sym.lib_id.value)
    except Exception:
        lib_id = "?"

    try:
        at_val = skip_sym.at.value
        sx = float(at_val[0])
        sy = float(at_val[1])
        sr = float(at_val[2]) if len(at_val) > 2 else 0.0
    except Exception:
        sx, sy, sr = 0.0, 0.0, 0.0

    try:
        unit = int(skip_sym.unit.value)
    except Exception:
        unit = 1

    try:
        comp_uuid = str(skip_sym.uuid.value) if hasattr(skip_sym.uuid, 'value') else str(skip_sym.uuid)
    except Exception:
        from .writer import _uuid
        comp_uuid = _uuid()

    try:
        ref = str(skip_sym.property.Reference.value)
    except Exception:
        ref = "?"
    try:
        val = str(skip_sym.property.Value.value)
    except Exception:
        val = "?"

    is_power = ref.startswith("#PWR") or ref.startswith("#FLG")

    # Extract mirror
    mirror_val = ""
    try:
        if hasattr(skip_sym, 'mirror') and skip_sym.mirror is not None:
            mirror_val = str(skip_sym.mirror.value) if hasattr(skip_sym.mirror, 'value') else str(skip_sym.mirror)
    except Exception:
        pass

    # Extract pins
    pins: list[PinEntry] = []
    if skip_sym.pin is not None:
        for p in skip_sym.pin:
            pin_num = str(p.number) if hasattr(p, 'number') else "?"
            try:
                pin_uuid = str(p.uuid.value) if hasattr(p.uuid, 'value') else str(p.uuid)
            except Exception:
                from .writer import _uuid
                pin_uuid = _uuid()
            pins.append(PinEntry(number=pin_num, uuid=pin_uuid))

    # Extract properties
    properties: list[PropertyEntry] = []
    for prop_name in ("Reference", "Value", "Footprint", "Datasheet", "Description"):
        try:
            prop = getattr(skip_sym.property, prop_name, None)
            if prop is None:
                continue
            prop_val = str(prop.value) if hasattr(prop, 'value') else ""

            prop_x, prop_y, prop_rot = sx, sy, 0.0
            if hasattr(prop, 'at') and prop.at is not None:
                pat = prop.at.value
                prop_x = float(pat[0])
                prop_y = float(pat[1])
                prop_rot = float(pat[2]) if len(pat) > 2 else 0.0

            hidden = False
            try:
                if hasattr(prop, 'effects') and prop.effects is not None:
                    eff = prop.effects
                    if hasattr(eff, 'hide') and eff.hide is not None:
                        hidden = True
            except Exception:
                pass

            font_size = 1.27
            try:
                if hasattr(prop, 'effects') and prop.effects is not None:
                    font = prop.effects.font
                    if font is not None and hasattr(font, 'size') and font.size is not None:
                        font_size = float(font.size.value[0])
            except Exception:
                pass

            properties.append(PropertyEntry(
                name=prop_name,
                value=prop_val,
                x=prop_x,
                y=prop_y,
                rotation=prop_rot,
                font_size=font_size,
                hidden=hidden,
            ))
        except Exception:
            pass

    # Extract footprint/datasheet/description from properties if available
    footprint = ""
    datasheet = "~"
    description = ""
    for prop in properties:
        if prop.name == "Footprint":
            footprint = prop.value
        elif prop.name == "Datasheet":
            datasheet = prop.value
        elif prop.name == "Description":
            description = prop.value

    comp = Component(
        lib_id=lib_id,
        x=sx, y=sy,
        rotation=sr,
        unit=unit,
        reference=ref,
        value=val,
        footprint=footprint,
        datasheet=datasheet,
        description=description,
        uuid=comp_uuid,
        pins=pins,
        properties=properties,
        is_power=is_power,
        mirror=mirror_val,
    )
    return comp


def wire_to_model(skip_wire: object) -> "Wire | None":
    """Convert a kicad-skip wire to a writer.Wire."""
    from .writer import Wire
    try:
        pts = skip_wire.pts.xy
        if len(pts) >= 2:
            p0 = pts[0].value
            p1 = pts[1].value
            wire_uuid = ""
            try:
                wire_uuid = str(skip_wire.uuid.value) if hasattr(skip_wire.uuid, 'value') else str(skip_wire.uuid)
            except Exception:
                from .writer import _uuid
                wire_uuid = _uuid()
            return Wire(
                x1=float(p0[0]), y1=float(p0[1]),
                x2=float(p1[0]), y2=float(p1[1]),
                uuid=wire_uuid,
            )
    except Exception:
        pass
    return None


def label_to_model(skip_label: object) -> "NetLabel | None":
    """Convert a kicad-skip label to a writer.NetLabel."""
    from .writer import NetLabel
    try:
        name = str(skip_label.value) if hasattr(skip_label, 'value') else "?"
        at_val = skip_label.at.value
        label_uuid = ""
        try:
            label_uuid = str(skip_label.uuid.value) if hasattr(skip_label.uuid, 'value') else str(skip_label.uuid)
        except Exception:
            from .writer import _uuid
            label_uuid = _uuid()
        return NetLabel(
            name=name,
            x=float(at_val[0]),
            y=float(at_val[1]),
            rotation=float(at_val[2]) if len(at_val) > 2 else 0.0,
            uuid=label_uuid,
        )
    except Exception:
        pass
    return None


def junction_to_model(skip_junction: object) -> "Junction | None":
    """Convert a kicad-skip junction to a writer.Junction."""
    from .writer import Junction
    try:
        at_val = skip_junction.at.value
        junc_uuid = ""
        try:
            junc_uuid = str(skip_junction.uuid.value) if hasattr(skip_junction.uuid, 'value') else str(skip_junction.uuid)
        except Exception:
            from .writer import _uuid
            junc_uuid = _uuid()
        return Junction(
            x=float(at_val[0]),
            y=float(at_val[1]),
            uuid=junc_uuid,
        )
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Render & ERC
# ---------------------------------------------------------------------------

def render_schematic(
    schematic_path: str,
    output_width: int = 2400,
    crop: tuple[float, float, float, float] | None = None,
) -> bytes:
    """Render a schematic to PNG via kicad-cli SVG export and cairosvg.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        output_width: Width of the output PNG in pixels.
        crop: Optional (x_min_mm, y_min_mm, x_max_mm, y_max_mm) to render
            only a region of the schematic.  Coordinates are in schematic mm.

    Returns:
        Raw PNG image bytes.

    Raises:
        FileNotFoundError: If the schematic file does not exist.
        RuntimeError: If kicad-cli SVG export fails or produces no output.
    """
    import cairosvg

    path = Path(schematic_path)
    if not path.exists():
        raise FileNotFoundError(f"Schematic not found: {schematic_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                KICAD_CLI, "sch", "export", "svg",
                "-o", tmpdir,
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"kicad-cli SVG export failed: {result.stderr}"
            )

        # kicad-cli names the SVG after the input schematic
        svg_path = Path(tmpdir) / (path.stem + ".svg")

        if not svg_path.exists():
            # Fall back to any SVG in the output directory
            svgs = list(Path(tmpdir).glob("*.svg"))
            if svgs:
                svg_path = svgs[0]
            else:
                raise RuntimeError(
                    f"No SVG output found. "
                    f"kicad-cli stdout: {result.stdout}, stderr: {result.stderr}"
                )

        svg_data = svg_path.read_bytes()

        if crop is not None:
            svg_data = _crop_svg(svg_data, crop)

        return cairosvg.svg2png(
            bytestring=svg_data,
            output_width=output_width,
        )


def _crop_svg(
    svg_data: bytes,
    crop: tuple[float, float, float, float],
) -> bytes:
    """Rewrite the SVG viewBox to show only the cropped region.

    KiCad SVGs use a coordinate system where 1 SVG user-unit ≈ 1 mil
    (1/1000 inch).  Schematic mm coordinates are converted via
    ``mm * 1000 / 25.4``.  We replace the ``viewBox`` attribute so
    cairosvg only rasterises the desired rectangle.
    """
    x_min_mm, y_min_mm, x_max_mm, y_max_mm = crop
    SCALE = 1000.0 / 25.4  # mm → mils (SVG user-units)
    vb_x = x_min_mm * SCALE
    vb_y = y_min_mm * SCALE
    vb_w = (x_max_mm - x_min_mm) * SCALE
    vb_h = (y_max_mm - y_min_mm) * SCALE

    text = svg_data.decode("utf-8")
    text = re.sub(
        r'viewBox="[^"]*"',
        f'viewBox="{vb_x:.2f} {vb_y:.2f} {vb_w:.2f} {vb_h:.2f}"',
        text,
        count=1,
    )
    return text.encode("utf-8")


def run_erc(schematic_path: str) -> dict:
    """Run KiCad Electrical Rules Check and return results.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.

    Returns:
        Dict with keys: violations (list), error_count, warning_count, total.
        May also include raw_stderr if no report file was generated.

    Raises:
        FileNotFoundError: If the schematic file does not exist.
    """
    path = Path(schematic_path)
    if not path.exists():
        raise FileNotFoundError(f"Schematic not found: {schematic_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = os.path.join(tmpdir, "erc.json")
        result = subprocess.run(
            [
                KICAD_CLI, "sch", "erc",
                "--format", "json",
                "--severity-all",
                "-o", report_path,
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # ERC returns non-zero when there are violations -- that is normal
        if os.path.exists(report_path):
            with open(report_path, encoding="utf-8") as f:
                erc_data = json.load(f)
        else:
            return {
                "violations": [],
                "error_count": 0,
                "warning_count": 0,
                "total": 0,
                "raw_stderr": result.stderr,
            }

    return _summarise_erc(erc_data)


def _summarise_erc(erc_data: dict) -> dict:
    """Parse KiCad ERC JSON output into a simplified violations summary."""
    violations: list[dict] = []
    error_count = 0
    warning_count = 0

    for sheet in erc_data.get("sheets", []):
        for v in sheet.get("violations", []):
            severity = v.get("severity", "unknown")
            if severity == "error":
                error_count += 1
            elif severity == "warning":
                warning_count += 1

            items: list[dict] = []
            for item in v.get("items", []):
                item_entry: dict = {"description": item.get("description", "")}
                pos = item.get("pos")
                if pos:
                    # ERC JSON position values × 100 = mm (empirically verified).
                    # The raw values appear to be in an internal unit ≈ 0.01 mm.
                    item_entry["x"] = round(pos.get("x", 0) * 100, 4)
                    item_entry["y"] = round(pos.get("y", 0) * 100, 4)
                items.append(item_entry)

            violations.append({
                "severity": severity,
                "type": v.get("type", ""),
                "description": v.get("description", ""),
                "items": items,
            })

    return {
        "violations": violations,
        "error_count": error_count,
        "warning_count": warning_count,
        "total": len(violations),
    }

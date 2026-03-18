"""Read-only schematic analysis via kicad-skip.

Uses kicad-skip ONLY for reading — never for writing.
Resolves pin positions, extracts component state, etc.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .geometry import outward_direction

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


def _parse_symbol(sym: object) -> dict:
    """Extract component data from a kicad-skip symbol object.

    Returns a dict with keys: reference, value, lib_id, x, y, rotation,
    pins, properties.
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

    pins = _parse_pins(sym, sx, sy)
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


def _parse_pins(sym: object, default_x: float, default_y: float) -> list[dict]:
    """Extract pin data from a kicad-skip symbol object."""
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
        power_symbols. See the tool docstring in server.py for full schema.
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
        }

    sch = skip.Schematic(str(path))
    root_uuid = str(sch.uuid.value) if hasattr(sch.uuid, 'value') else str(sch.uuid)

    components: list[dict] = []
    power_symbols: list[dict] = []

    if sch.symbol is not None:
        for sym in sch.symbol:
            entry = _parse_symbol(sym)
            if entry["reference"].startswith("#PWR"):
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

    return {
        "root_uuid": root_uuid,
        "components": components,
        "wires": wires,
        "labels": labels,
        "junctions": junctions,
        "power_symbols": power_symbols,
    }


def render_schematic(schematic_path: str, output_width: int = 2400) -> bytes:
    """Render a schematic to PNG via kicad-cli SVG export and cairosvg.

    Args:
        schematic_path: Absolute path to the .kicad_sch file.
        output_width: Width of the output PNG in pixels.

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
        return cairosvg.svg2png(
            bytestring=svg_data,
            output_width=output_width,
        )


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
                    # KiCad ERC JSON reports positions in metres; convert to mm
                    item_entry["x"] = round(pos.get("x", 0) * 1000, 4)
                    item_entry["y"] = round(pos.get("y", 0) * 1000, 4)
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

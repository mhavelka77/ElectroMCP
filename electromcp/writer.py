"""Direct KiCad 9 .kicad_sch file writer.

Maintains an in-memory data model and writes the COMPLETE file in one shot.
No kicad-skip for writing — only string formatting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .geometry import fmt


def _uuid() -> str:
    """Generate a random UUID string for KiCad element identification."""
    return str(uuid.uuid4())


@dataclass
class PinEntry:
    """A pin on a placed component."""
    number: str
    uuid: str = field(default_factory=_uuid)


@dataclass
class PropertyEntry:
    """A component property (Reference, Value, etc.)."""
    name: str
    value: str
    x: float
    y: float
    rotation: float = 0
    font_size: float = 1.27
    hidden: bool = False
    justify: str = ""  # e.g. "left", "left bottom", ""

    def to_sexpr(self, indent: str = "\t\t") -> str:
        """Render this property as a KiCad S-expression string."""
        escaped_value = self.value.replace("\\", "\\\\").replace('"', '\\"')
        lines = [
            f'{indent}(property "{self.name}" "{escaped_value}"',
            f"{indent}\t(at {fmt(self.x)} {fmt(self.y)} {fmt(self.rotation)})",
            f"{indent}\t(effects",
            f"{indent}\t\t(font",
            f"{indent}\t\t\t(size {fmt(self.font_size)} {fmt(self.font_size)})",
            f"{indent}\t\t)",
        ]
        if self.justify:
            lines.append(f"{indent}\t\t(justify {self.justify})")
        if self.hidden:
            lines.append(f"{indent}\t\t(hide yes)")
        lines.append(f"{indent}\t)")
        lines.append(f"{indent})")
        return "\n".join(lines)


@dataclass
class Component:
    """A placed component instance."""
    lib_id: str          # e.g. "Device:R"
    x: float
    y: float
    rotation: float = 0
    unit: int = 1
    reference: str = ""
    value: str = ""
    footprint: str = ""
    datasheet: str = "~"
    description: str = ""
    uuid: str = field(default_factory=_uuid)
    pins: list[PinEntry] = field(default_factory=list)
    properties: list[PropertyEntry] = field(default_factory=list)
    is_power: bool = False
    mirror: str = ""  # "", "x", or "y"

    def build_properties(self) -> list[PropertyEntry]:
        """Build standard properties if custom ones aren't set."""
        if self.properties:
            return self.properties

        props = []
        ref_hidden = self.reference.startswith("#")

        props.append(PropertyEntry(
            name="Reference", value=self.reference,
            x=self.x + 2.032, y=self.y, rotation=0,
            hidden=ref_hidden,
        ))
        props.append(PropertyEntry(
            name="Value", value=self.value,
            x=self.x, y=self.y + 2.54, rotation=0,
        ))
        props.append(PropertyEntry(
            name="Footprint", value=self.footprint,
            x=self.x, y=self.y, rotation=0, hidden=True,
        ))
        props.append(PropertyEntry(
            name="Datasheet", value=self.datasheet,
            x=self.x, y=self.y, rotation=0, hidden=True,
        ))
        props.append(PropertyEntry(
            name="Description", value=self.description,
            x=self.x, y=self.y, rotation=0, hidden=True,
        ))
        return props

    def to_sexpr(self, root_uuid: str) -> str:
        """Render this component as a KiCad S-expression string."""
        props = self.build_properties()
        props_text = "\n".join(p.to_sexpr("\t\t") for p in props)
        pins_text = "\n".join(
            f'\t\t(pin "{p.number}"\n\t\t\t(uuid "{p.uuid}")\n\t\t)'
            for p in self.pins
        )
        mirror_line = f"\n\t\t(mirror {self.mirror})" if self.mirror else ""
        return f"""\t(symbol
\t\t(lib_id "{self.lib_id}")
\t\t(at {fmt(self.x)} {fmt(self.y)} {fmt(self.rotation)}){mirror_line}
\t\t(unit {self.unit})
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "{self.uuid}")
{props_text}
{pins_text}
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/{root_uuid}"
\t\t\t\t\t(reference "{self.reference}")
\t\t\t\t\t(unit {self.unit})
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""


@dataclass
class Wire:
    """A wire segment between two points."""
    x1: float
    y1: float
    x2: float
    y2: float
    uuid: str = field(default_factory=_uuid)

    def to_sexpr(self) -> str:
        """Render this wire as a KiCad S-expression string."""
        return f"""\t(wire
\t\t(pts
\t\t\t(xy {fmt(self.x1)} {fmt(self.y1)})
\t\t\t(xy {fmt(self.x2)} {fmt(self.y2)})
\t\t)
\t\t(stroke
\t\t\t(width 0)
\t\t\t(type default)
\t\t)
\t\t(uuid "{self.uuid}")
\t)"""


@dataclass
class NetLabel:
    """A named net label placed on a wire or pin endpoint."""
    name: str
    x: float
    y: float
    rotation: float = 0
    uuid: str = field(default_factory=_uuid)

    def to_sexpr(self) -> str:
        """Render this label as a KiCad S-expression string."""
        return f"""\t(label "{self.name}"
\t\t(at {fmt(self.x)} {fmt(self.y)} {fmt(self.rotation)})
\t\t(fields_autoplaced yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t\t(justify left bottom)
\t\t)
\t\t(uuid "{self.uuid}")
\t)"""


@dataclass
class Junction:
    """A junction dot at a wire intersection."""
    x: float
    y: float
    uuid: str = field(default_factory=_uuid)

    def to_sexpr(self) -> str:
        """Render this junction as a KiCad S-expression string."""
        return f"""\t(junction
\t\t(at {fmt(self.x)} {fmt(self.y)})
\t\t(diameter 0)
\t\t(color 0 0 0 0)
\t\t(uuid "{self.uuid}")
\t)"""


@dataclass
class NoConnect:
    """A no-connect (X) flag on an unused pin."""
    x: float
    y: float
    uuid: str = field(default_factory=_uuid)

    def to_sexpr(self) -> str:
        """Render this no-connect as a KiCad S-expression string."""
        return f"""\t(no_connect
\t\t(at {fmt(self.x)} {fmt(self.y)})
\t\t(uuid "{self.uuid}")
\t)"""


@dataclass
class TextNote:
    """A free text annotation on the schematic."""
    text: str
    x: float
    y: float
    font_size: float = 1.27
    uuid: str = field(default_factory=_uuid)

    def to_sexpr(self) -> str:
        """Render this text note as a KiCad S-expression string."""
        escaped = self.text.replace("\\", "\\\\").replace('"', '\\"')
        return f"""\t(text "{escaped}"
\t\t(at {fmt(self.x)} {fmt(self.y)} 0)
\t\t(effects
\t\t\t(font
\t\t\t\t(size {fmt(self.font_size)} {fmt(self.font_size)})
\t\t\t)
\t\t)
\t\t(uuid "{self.uuid}")
\t)"""


@dataclass
class SchematicModel:
    """In-memory model of a KiCad 9 schematic."""

    root_uuid: str = field(default_factory=_uuid)
    paper: str = "A4"
    lib_symbol_texts: dict[str, str] = field(default_factory=dict)  # lib_id -> raw text
    components: list[Component] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    labels: list[NetLabel] = field(default_factory=list)
    junctions: list[Junction] = field(default_factory=list)
    no_connects: list[NoConnect] = field(default_factory=list)
    text_notes: list[TextNote] = field(default_factory=list)
    _passthrough_blocks: list[str] = field(default_factory=list)

    def find_component(self, reference: str) -> Component | None:
        """Find a component by reference designator."""
        for c in self.components:
            if c.reference == reference:
                return c
        return None

    def add_lib_symbol(self, lib_id: str, text: str) -> None:
        """Add a lib_symbol definition (if not already present)."""
        if lib_id not in self.lib_symbol_texts:
            self.lib_symbol_texts[lib_id] = text

    def remove_component(self, reference: str) -> bool:
        """Remove a component by reference. Returns True if found."""
        for i, c in enumerate(self.components):
            if c.reference == reference:
                self.components.pop(i)
                return True
        return False

    def remove_wire(self, x1: float, y1: float, x2: float, y2: float,
                    tolerance: float = 1.0) -> bool:
        """Remove a wire segment with fuzzy coordinate matching."""
        for i, w in enumerate(self.wires):
            if (self._close(w.x1, x1, tolerance) and
                self._close(w.y1, y1, tolerance) and
                self._close(w.x2, x2, tolerance) and
                self._close(w.y2, y2, tolerance)):
                self.wires.pop(i)
                return True
            # Also check reversed direction
            if (self._close(w.x1, x2, tolerance) and
                self._close(w.y1, y2, tolerance) and
                self._close(w.x2, x1, tolerance) and
                self._close(w.y2, y1, tolerance)):
                self.wires.pop(i)
                return True
        return False

    @staticmethod
    def _close(a: float, b: float, tol: float) -> bool:
        """Return True if *a* and *b* are within *tol* of each other."""
        return abs(a - b) <= tol

    def clear_wires(self) -> int:
        """Remove all wires. Returns count removed."""
        n = len(self.wires)
        self.wires.clear()
        return n

    def remove_no_connect(self, x: float, y: float,
                          tolerance: float = 1.0) -> bool:
        """Remove a no-connect flag near (x, y). Returns True if found."""
        for i, nc in enumerate(self.no_connects):
            if self._close(nc.x, x, tolerance) and self._close(nc.y, y, tolerance):
                self.no_connects.pop(i)
                return True
        return False

    def remove_label(self, net_name: str, x: float | None = None,
                     y: float | None = None, tolerance: float = 1.0) -> bool:
        """Remove a label by name and optional position. Returns True if found."""
        for i, lb in enumerate(self.labels):
            if lb.name == net_name:
                if x is None or y is None or (
                    self._close(lb.x, x, tolerance) and
                    self._close(lb.y, y, tolerance)
                ):
                    self.labels.pop(i)
                    return True
        return False

    def write(self, output_path: str | Path) -> None:
        """Write the complete .kicad_sch file.

        Also creates a minimal .kicad_pro project file alongside it
        if one does not already exist (KiCad requires it).

        Args:
            output_path: Filesystem path for the .kicad_sch file.
        """
        out = Path(output_path)

        lib_symbols_inner = "\n".join(
            _indent(text, "\t\t")
            for text in self.lib_symbol_texts.values()
        )
        components_text = "\n".join(
            c.to_sexpr(self.root_uuid) for c in self.components
        )
        wires_text = "\n".join(w.to_sexpr() for w in self.wires)
        labels_text = "\n".join(lb.to_sexpr() for lb in self.labels)
        junctions_text = "\n".join(j.to_sexpr() for j in self.junctions)
        no_connects_text = "\n".join(nc.to_sexpr() for nc in self.no_connects)
        text_notes_text = "\n".join(t.to_sexpr() for t in self.text_notes)
        passthrough_text = "\n".join(self._passthrough_blocks)

        content = f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "{self.root_uuid}")
\t(paper "{self.paper}")
\t(lib_symbols
{lib_symbols_inner}
\t)
{components_text}
{wires_text}
{labels_text}
{junctions_text}
{no_connects_text}
{text_notes_text}
{passthrough_text}
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
"""
        out.write_text(content, encoding="utf-8")

        pro_path = out.with_suffix(".kicad_pro")
        if not pro_path.exists():
            _write_project_file(pro_path)


def _indent(text: str, indent: str) -> str:
    """Add indentation to each line of text."""
    lines = text.split("\n")
    return "\n".join(indent + line if line.strip() else line for line in lines)


def _write_project_file(path: Path) -> None:
    """Write a minimal .kicad_pro project file."""
    content = """{
  "board": {
    "3dviewports": [],
    "design_settings": {},
    "ipc2581": {},
    "layer_presets": [],
    "layers": []
  },
  "boards": [],
  "cvpcb": {
    "equivalence_files": []
  },
  "libraries": {
    "pinned_footprint_libs": [],
    "pinned_symbol_libs": []
  },
  "meta": {
    "filename": "%s",
    "version": 1
  },
  "net_settings": {},
  "pcbnew": {
    "last_paths": {
      "gencad": "",
      "idf": "",
      "netlist": "",
      "plot": "",
      "pos_files": "",
      "specctra_dsn": "",
      "step": "",
      "svg": "",
      "vrml": ""
    },
    "page_layout_descr_file": ""
  },
  "schematic": {
    "legacy_lib_dir": "",
    "legacy_lib_list": []
  },
  "sheets": [],
  "text_variables": {}
}
""" % path.name
    path.write_text(content, encoding="utf-8")

"""KiCad 9 symbol library parsing and search.

Reads .kicad_sym files and extracts raw symbol definitions for injection
into schematic lib_symbols sections. Handles the 'extends' pattern where
child symbols inherit graphics from parents.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_symbol_lib_dir() -> Path | None:
    """Find the KiCad system symbol library directory.

    Checks the KICAD_SYMBOL_DIR environment variable first, then
    platform-specific default install locations.

    Returns:
        Path to the symbol library directory, or None if not found.
    """
    # Environment variable override
    env_dir = os.environ.get("KICAD_SYMBOL_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
        logger.warning(
            "KICAD_SYMBOL_DIR is set to '%s' but the directory does not exist.",
            env_dir,
        )
        return None

    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",  # macOS
        "/usr/share/kicad/symbols",  # Linux
        r"C:\Program Files\KiCad\share\kicad\symbols",  # Windows
    ]
    for c in candidates:
        p = Path(c)
        if p.is_dir():
            return p

    return None


KICAD_SYMBOLS_DIR = _find_symbol_lib_dir()


@dataclass
class SymbolInfo:
    """Metadata about a symbol in a library."""
    lib_name: str        # e.g. "Device"
    symbol_name: str     # e.g. "R"
    full_id: str         # e.g. "Device:R"
    description: str
    keywords: str
    extends: str | None  # parent symbol name if this extends another
    lib_path: Path       # path to the .kicad_sym file


@dataclass
class LibraryManager:
    """Manages KiCad symbol libraries for search and extraction."""

    libraries: dict[str, Path] = field(default_factory=dict)
    _symbol_index: dict[str, SymbolInfo] = field(default_factory=dict)
    _indexed: bool = False

    def load_system_libraries(self) -> int:
        """Load all system KiCad symbol libraries.

        Returns:
            Number of library files loaded.
        """
        if KICAD_SYMBOLS_DIR is None or not KICAD_SYMBOLS_DIR.exists():
            logger.warning(
                "KiCad symbol library directory not found. "
                "Install KiCad 9 or set the KICAD_SYMBOL_DIR environment variable."
            )
            return 0
        count = 0
        for f in sorted(KICAD_SYMBOLS_DIR.glob("*.kicad_sym")):
            lib_name = f.stem
            self.libraries[lib_name] = f
            count += 1
        self._indexed = False
        return count

    def register_library(self, name: str, path: str) -> None:
        """Register an external .kicad_sym library file.

        Args:
            name: Library name used as the prefix in full_id references.
            path: Absolute path to the .kicad_sym file.

        Raises:
            FileNotFoundError: If the library file does not exist.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Library file not found: {path}")
        self.libraries[name] = p
        self._indexed = False

    def _ensure_index(self) -> None:
        """Build the symbol index if not already done."""
        if self._indexed:
            return
        self._symbol_index.clear()
        for lib_name, lib_path in self.libraries.items():
            self._index_library(lib_name, lib_path)
        self._indexed = True

    def _index_library(self, lib_name: str, lib_path: Path) -> None:
        """Index all top-level symbols in a library file."""
        try:
            text = lib_path.read_text(encoding="utf-8")
        except Exception:
            return

        # Match top-level symbol declarations (not sub-symbols like R_0_1)
        # Top-level symbols are at indent level 1 (one tab)
        pattern = re.compile(
            r'^\t\(symbol "([^"]+)"',
            re.MULTILINE,
        )
        for m in pattern.finditer(text):
            sym_name = m.group(1)
            # Skip sub-symbols (contain _N_N suffix)
            if re.search(r"_\d+_\d+$", sym_name):
                continue

            full_id = f"{lib_name}:{sym_name}"

            # Extract description
            desc = ""
            desc_m = self._find_property_after(text, m.start(), "Description")
            if desc_m:
                desc = desc_m

            # Extract keywords
            kw = ""
            kw_m = self._find_property_after(text, m.start(), "ki_keywords")
            if kw_m:
                kw = kw_m

            # Check for extends
            extends = None
            extends_m = re.search(
                r'\(extends "([^"]+)"\)',
                text[m.start():m.start() + 500],
            )
            if extends_m:
                extends = extends_m.group(1)

            self._symbol_index[full_id] = SymbolInfo(
                lib_name=lib_name,
                symbol_name=sym_name,
                full_id=full_id,
                description=desc,
                keywords=kw,
                extends=extends,
                lib_path=lib_path,
            )

    def _find_property_after(self, text: str, start: int, prop_name: str) -> str | None:
        """Find a property value in text after a given position."""
        # Look within the next 5000 chars for the property
        chunk = text[start:start + 5000]
        m = re.search(
            rf'\(property "{prop_name}" "([^"]*(?:\\.[^"]*)*)"',
            chunk,
        )
        if m:
            return m.group(1).replace('\\"', '"')
        return None

    def search(self, query: str, limit: int = 20) -> list[SymbolInfo]:
        """Search symbols by name, description, or keywords."""
        self._ensure_index()
        query_lower = query.lower()
        terms = query_lower.split()

        results: list[tuple[int, SymbolInfo]] = []
        for info in self._symbol_index.values():
            searchable = f"{info.symbol_name} {info.description} {info.keywords} {info.lib_name}".lower()

            # Score: exact match > starts_with > contains
            score = 0
            if info.symbol_name.lower() == query_lower:
                score = 100
            elif info.full_id.lower() == query_lower:
                score = 100
            else:
                for term in terms:
                    if term == info.symbol_name.lower():
                        score += 50
                    elif info.symbol_name.lower().startswith(term):
                        score += 30
                    elif term in searchable:
                        score += 10

            if score > 0:
                results.append((score, info))

        results.sort(key=lambda x: (-x[0], x[1].full_id))
        return [info for _, info in results[:limit]]

    def get_lib_symbol_text(self, full_id: str) -> str:
        """Extract the raw lib_symbol text for a component, ready for injection.

        For 'extends' symbols, this resolves the parent and builds the
        full definition with renamed sub-symbols and child properties.

        The returned text has the top-level name prefixed with the library
        (e.g., "Device:R") but sub-symbols keep original names (e.g., "R_0_1").
        """
        self._ensure_index()

        if full_id not in self._symbol_index:
            raise KeyError(f"Symbol not found: {full_id}")

        info = self._symbol_index[full_id]
        lib_text = info.lib_path.read_text(encoding="utf-8")

        if info.extends:
            return self._resolve_extends(info, lib_text)
        else:
            return self._extract_and_prefix(info, lib_text)

    def _extract_symbol_block(self, text: str, symbol_name: str) -> str:
        """Extract a complete symbol block from library text.

        Uses parenthesis depth counting to find the exact boundaries.
        """
        # Find the symbol declaration
        pattern = re.compile(
            rf'^\t\(symbol "{re.escape(symbol_name)}"',
            re.MULTILINE,
        )
        m = pattern.search(text)
        if not m:
            raise KeyError(f"Symbol '{symbol_name}' not found in library text")

        start = m.start()
        # Count parens to find the end
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            i += 1

        raise ValueError(f"Unbalanced parentheses for symbol '{symbol_name}'")

    def _extract_and_prefix(self, info: SymbolInfo, lib_text: str) -> str:
        """Extract a non-extends symbol and prefix the top-level name."""
        block = self._extract_symbol_block(lib_text, info.symbol_name)

        # Replace only the FIRST occurrence of the symbol name (top-level)
        # with the library-qualified name
        old = f'(symbol "{info.symbol_name}"'
        new = f'(symbol "{info.full_id}"'
        # Only replace the very first occurrence
        result = block.replace(old, new, 1)

        # Strip leading tab (library files indent top-level symbols with one tab)
        result = _dedent_one_tab(result)

        return result

    def _resolve_extends(self, child_info: SymbolInfo, lib_text: str) -> str:
        """Resolve an 'extends' symbol by copying parent and applying child overrides."""
        parent_name = child_info.extends
        child_name = child_info.symbol_name

        # Extract parent block
        parent_block = self._extract_symbol_block(lib_text, parent_name)

        # Extract child block (for property overrides)
        child_block = self._extract_symbol_block(lib_text, child_name)

        # Start with parent's full definition
        result = parent_block

        # Rename top-level: parent -> child with library prefix
        result = result.replace(
            f'(symbol "{parent_name}"',
            f'(symbol "{child_info.full_id}"',
            1,
        )

        # Rename sub-symbols: parent_N_N -> child_N_N (NO library prefix!)
        sub_pattern = re.compile(rf'(symbol "{re.escape(parent_name)}_(\d+_\d+)")')
        result = sub_pattern.sub(
            lambda m: f'symbol "{child_name}_{m.group(2)}"',
            result,
        )

        # Override properties from child
        child_props = self._extract_properties(child_block)
        for prop_name, prop_text in child_props.items():
            # Replace in result
            result = self._replace_property(result, prop_name, prop_text)

        # Remove (extends ...) if present in parent (shouldn't be, but safety)
        result = re.sub(r'\s*\(extends "[^"]+"\)\n?', "\n", result)

        result = _dedent_one_tab(result)
        return result

    def _extract_properties(self, block: str) -> dict[str, str]:
        """Extract all (property ...) blocks from a symbol block."""
        props = {}
        # Find each property and extract its complete block
        for m in re.finditer(r'\(property "([^"]+)"', block):
            prop_name = m.group(1)
            start = m.start()
            # Count parens
            depth = 0
            i = start
            while i < len(block):
                if block[i] == "(":
                    depth += 1
                elif block[i] == ")":
                    depth -= 1
                    if depth == 0:
                        props[prop_name] = block[start:i + 1]
                        break
                i += 1
        return props

    def _replace_property(self, block: str, prop_name: str, new_prop: str) -> str:
        """Replace a property block in a symbol definition."""
        m = re.search(rf'\(property "{re.escape(prop_name)}"', block)
        if not m:
            return block

        start = m.start()
        depth = 0
        i = start
        while i < len(block):
            if block[i] == "(":
                depth += 1
            elif block[i] == ")":
                depth -= 1
                if depth == 0:
                    # Get the indentation of the original
                    line_start = block.rfind("\n", 0, start) + 1
                    indent = block[line_start:start]
                    # Reindent the new property to match
                    new_lines = new_prop.strip().split("\n")
                    reindented = new_lines[0]
                    for line in new_lines[1:]:
                        reindented += "\n" + indent + line.lstrip("\t")
                    return block[:start] + reindented + block[i + 1:]
            i += 1
        return block

    def get_power_symbol_text(self, power_net: str) -> str:
        """Get the lib_symbol text for a power symbol (VCC, GND, +5V, etc.)."""
        full_id = f"power:{power_net}"
        return self.get_lib_symbol_text(full_id)


def _dedent_one_tab(text: str) -> str:
    """Remove one leading tab from each line."""
    lines = text.split("\n")
    result = []
    for line in lines:
        if line.startswith("\t"):
            result.append(line[1:])
        else:
            result.append(line)
    return "\n".join(result)

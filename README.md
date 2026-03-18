# ElectroMCP

An MCP server that lets AI agents design KiCad 9 schematics. Place components, wire them, render to PNG, run electrical checks, and iterate until professional quality.

## What it does

ElectroMCP exposes 16 tools via the [Model Context Protocol](https://modelcontextprotocol.io/) that give an LLM full control over KiCad schematic design:

- **Search & place** components from KiCad's symbol libraries
- **Wire** components with Manhattan routing
- **Add** power symbols (VCC, GND), net labels, and junctions
- **Render** the schematic to PNG for visual verification
- **Run ERC** (Electrical Rules Check) to catch connection errors
- **Refine** by moving components, adjusting labels, rotating parts

The AI agent follows a place → wire → check → render → refine loop to produce clean schematics.

## Prerequisites

- **Python 3.11+**
- **KiCad 9** installed ([kicad.org](https://www.kicad.org/download/))
- **Cairo** graphics library (for SVG→PNG rendering)

### Install Cairo

```bash
# macOS
brew install cairo

# Ubuntu/Debian
sudo apt install libcairo2-dev

# Fedora
sudo dnf install cairo-devel
```

## Installation

```bash
# Clone
git clone https://github.com/anthropics/electromcp.git
cd electromcp

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

## Setup with Claude Code

Add to your Claude Code MCP settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "electromcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/electromcp", "electromcp"]
    }
  }
}
```

Then copy `CLAUDE.md` into your project — it contains the workflow instructions that guide the AI through schematic design.

## Setup with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "electromcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/electromcp", "electromcp"]
    }
  }
}
```

## Setup with OpenCode

Add to your `.opencode.json` (note the leading dot):

```json
{
  "mcpServers": {
    "electromcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/electromcp", "electromcp"]
    }
  }
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `KICAD_CLI_PATH` | Path to `kicad-cli` binary | Auto-detected |
| `KICAD_SYMBOL_DIR` | Path to KiCad symbol libraries | Auto-detected |

## Tools

| Tool | Description |
|------|-------------|
| `search_symbols` | Search KiCad symbol libraries by name/description |
| `add_component` | Place a component on the schematic |
| `add_power_symbol` | Place VCC, GND, or other power symbol |
| `add_wire` | Add a wire segment between two points |
| `add_net_label` | Add a named net label |
| `add_junction` | Add junction dot at wire T-intersection |
| `get_circuit_state` | Get full schematic state as JSON |
| `render_schematic_view` | Render schematic to PNG image |
| `run_erc_check` | Run electrical rules check |
| `move_component` | Move a component to new position |
| `move_property_label` | Adjust label position (offset or absolute) |
| `rotate_component` | Change component rotation |
| `delete_component` | Remove a component |
| `delete_wire` | Remove a specific wire segment |
| `delete_all_wires` | Remove all wires (for complete rewiring) |
| `register_library` | Register an external symbol library |

## Architecture

```
electromcp/
├── server.py    # MCP tool definitions and request handling
├── reader.py    # Read-only schematic analysis (via kicad-skip)
├── writer.py    # In-memory data model and .kicad_sch file generation
├── library.py   # Symbol library indexing and search
└── geometry.py  # Grid snapping and coordinate math
```

Key design decisions:
- **kicad-skip** is used read-only for parsing existing schematics
- **writer.py** generates complete `.kicad_sch` files from an in-memory model — never modifies files in place
- Schematic models are cached in memory for fast multi-step editing
- All coordinates are in millimeters with Y-axis pointing down

## License

MIT

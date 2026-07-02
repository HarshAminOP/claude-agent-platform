#!/usr/bin/env python3
"""Diagram Server MCP — local diagram generation from text.

Renders Mermaid, D2, Graphviz/DOT, and PlantUML diagrams to SVG/PNG
using locally installed CLI tools. Zero external services.

Supported renderers (auto-detected based on what's installed):
  - d2:       brew install d2
  - graphviz: brew install graphviz (provides `dot`)
  - mermaid:  npx @mermaid-js/mermaid-cli (no global install needed)
  - plantuml: brew install plantuml

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger("cap.diagram")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

server = Server("cap-diagram")

SUPPORTED_FORMATS = ("svg", "png")
SUPPORTED_ENGINES = ("d2", "mermaid", "graphviz", "plantuml")

MAX_SOURCE_LENGTH = 50_000


def _find_tool(name: str) -> str | None:
    """Find a CLI tool in PATH."""
    return shutil.which(name)


def _available_engines() -> dict[str, str]:
    """Detect which diagram engines are available locally."""
    engines = {}
    if _find_tool("d2"):
        engines["d2"] = _find_tool("d2")
    if _find_tool("dot"):
        engines["graphviz"] = _find_tool("dot")
    if _find_tool("mmdc"):
        engines["mermaid"] = _find_tool("mmdc")
    elif _find_tool("npx"):
        engines["mermaid"] = "npx"
    if _find_tool("plantuml"):
        engines["plantuml"] = _find_tool("plantuml")
    return engines


ENGINES = _available_engines()


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="diagram_render",
            description="Render a diagram from text source (Mermaid, D2, Graphviz DOT, PlantUML) to SVG or PNG. All rendering happens locally — no external services.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Diagram source code (Mermaid, D2, DOT, or PlantUML syntax)",
                    },
                    "engine": {
                        "type": "string",
                        "enum": list(SUPPORTED_ENGINES),
                        "description": "Rendering engine. Auto-detected from source if omitted.",
                    },
                    "format": {
                        "type": "string",
                        "enum": list(SUPPORTED_FORMATS),
                        "default": "svg",
                        "description": "Output format (svg or png)",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the rendered file. If omitted, saves to a temp file and returns the path.",
                    },
                    "theme": {
                        "type": "string",
                        "enum": ["default", "dark", "neutral"],
                        "default": "default",
                    },
                },
                "required": ["source"],
            },
        ),
        Tool(
            name="diagram_engines",
            description="List available diagram rendering engines installed locally.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="diagram_mermaid_to_md",
            description="Wrap diagram source in a Mermaid code block for embedding in Markdown. GitHub/GitLab render these natively.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Mermaid diagram source code",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional caption above the diagram",
                    },
                },
                "required": ["source"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "diagram_render":
            return await _handle_render(arguments)
        elif name == "diagram_engines":
            return await _handle_engines(arguments)
        elif name == "diagram_mermaid_to_md":
            return await _handle_mermaid_to_md(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_render(args: dict):
    source = args["source"]
    if len(source) > MAX_SOURCE_LENGTH:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Source too large ({len(source)} chars, max {MAX_SOURCE_LENGTH})"
        }))]

    engine = args.get("engine") or _detect_engine(source)
    fmt = args.get("format", "svg")
    output_path = args.get("output_path")
    theme = args.get("theme", "default")

    if engine not in ENGINES:
        available = list(ENGINES.keys())
        return [TextContent(type="text", text=json.dumps({
            "error": f"Engine '{engine}' not available. Installed: {available}. Install with: brew install {engine}",
            "available_engines": available,
        }))]

    with tempfile.TemporaryDirectory(prefix="cap-diagram-") as tmpdir:
        input_file, output_file = _prepare_files(tmpdir, engine, fmt, source)

        cmd = _build_command(engine, input_file, output_file, fmt, theme)
        if cmd is None:
            return [TextContent(type="text", text=json.dumps({
                "error": f"Failed to build command for engine '{engine}'"
            }))]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text=json.dumps({
                "error": "Diagram rendering timed out (30s)"
            }))]

        if result.returncode != 0:
            return [TextContent(type="text", text=json.dumps({
                "error": "Rendering failed",
                "stderr": result.stderr[:500],
                "engine": engine,
            }))]

        if not Path(output_file).exists():
            return [TextContent(type="text", text=json.dumps({
                "error": "Output file not generated",
                "stderr": result.stderr[:500],
            }))]

        if output_path:
            dest = Path(output_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).rename(dest) if dest.parent == Path(tmpdir) else _copy_file(output_file, str(dest))
            final_path = str(dest)
        else:
            final_path = str(Path(output_file))
            persistent = Path(tmpdir).parent / f"cap-diagram-{os.urandom(4).hex()}.{fmt}"
            _copy_file(output_file, str(persistent))
            final_path = str(persistent)

        file_size = Path(final_path).stat().st_size

        return [TextContent(type="text", text=json.dumps({
            "status": "rendered",
            "path": final_path,
            "engine": engine,
            "format": fmt,
            "size_bytes": file_size,
        }))]


async def _handle_engines(args: dict):
    engines_info = {}
    for name, path in ENGINES.items():
        version = _get_version(name, path)
        engines_info[name] = {
            "available": True,
            "path": path,
            "version": version,
        }

    for name in SUPPORTED_ENGINES:
        if name not in engines_info:
            engines_info[name] = {
                "available": False,
                "install": _install_hint(name),
            }

    return [TextContent(type="text", text=json.dumps({
        "engines": engines_info,
        "supported_formats": list(SUPPORTED_FORMATS),
    }))]


async def _handle_mermaid_to_md(args: dict):
    source = args["source"].strip()
    title = args.get("title")

    md_parts = []
    if title:
        md_parts.append(f"**{title}**\n")
    md_parts.append("```mermaid")
    md_parts.append(source)
    md_parts.append("```")

    return [TextContent(type="text", text=json.dumps({
        "markdown": "\n".join(md_parts),
    }))]


def _detect_engine(source: str) -> str:
    """Auto-detect diagram engine from source syntax."""
    source_lower = source.strip().lower()

    if source_lower.startswith("graph ") or source_lower.startswith("flowchart ") or \
       source_lower.startswith("sequencediagram") or source_lower.startswith("sequence") or \
       source_lower.startswith("classDiagram") or source_lower.startswith("statediagram") or \
       source_lower.startswith("gantt") or source_lower.startswith("pie") or \
       source_lower.startswith("erdiagram") or source_lower.startswith("gitgraph") or \
       "---" in source[:50] and "title:" in source[:100]:
        return "mermaid"

    if source_lower.startswith("digraph") or source_lower.startswith("graph") and "{" in source[:50]:
        if "->" in source or "--" in source:
            return "graphviz"

    if source_lower.startswith("@startuml") or source_lower.startswith("@startmindmap"):
        return "plantuml"

    if ": {" in source or "-> " in source[:200] or source.strip().split("\n")[0].endswith("{"):
        return "d2"

    if "d2" in ENGINES:
        return "d2"
    if "mermaid" in ENGINES:
        return "mermaid"
    if "graphviz" in ENGINES:
        return "graphviz"

    return "d2"


def _prepare_files(tmpdir: str, engine: str, fmt: str, source: str) -> tuple[str, str]:
    """Write source to input file and determine output path."""
    ext_map = {
        "d2": ".d2",
        "mermaid": ".mmd",
        "graphviz": ".dot",
        "plantuml": ".puml",
    }
    input_ext = ext_map.get(engine, ".txt")
    input_file = os.path.join(tmpdir, f"input{input_ext}")
    output_file = os.path.join(tmpdir, f"output.{fmt}")

    with open(input_file, "w") as f:
        f.write(source)

    return input_file, output_file


def _build_command(engine: str, input_file: str, output_file: str, fmt: str, theme: str) -> list[str] | None:
    """Build the CLI command for the given engine."""
    if engine == "d2":
        cmd = [ENGINES["d2"]]
        if theme == "dark":
            cmd.extend(["--theme", "200"])
        elif theme == "neutral":
            cmd.extend(["--theme", "300"])
        cmd.extend([input_file, output_file])
        return cmd

    elif engine == "graphviz":
        output_fmt = "svg" if fmt == "svg" else "png"
        return [ENGINES["graphviz"], f"-T{output_fmt}", "-o", output_file, input_file]

    elif engine == "mermaid":
        tool_path = ENGINES["mermaid"]
        if tool_path == "npx":
            cmd = ["npx", "-y", "@mermaid-js/mermaid-cli", "mmdc"]
        else:
            cmd = [tool_path]
        cmd.extend(["-i", input_file, "-o", output_file])
        if fmt == "png":
            cmd.extend(["-e", "png"])
        if theme == "dark":
            cmd.extend(["--theme", "dark"])
        elif theme == "neutral":
            cmd.extend(["--theme", "neutral"])
        return cmd

    elif engine == "plantuml":
        fmt_flag = "-tsvg" if fmt == "svg" else "-tpng"
        return [ENGINES["plantuml"], fmt_flag, "-o", os.path.dirname(output_file), input_file]

    return None


def _get_version(engine: str, path: str) -> str:
    """Get version string for a diagram engine."""
    try:
        if engine == "d2":
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        elif engine == "graphviz":
            r = subprocess.run([path, "-V"], capture_output=True, text=True, timeout=5)
            return r.stderr.strip().split("version")[-1].strip().split(" ")[0] if r.stderr else ""
        elif engine == "mermaid":
            if path == "npx":
                return "via npx"
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        elif engine == "plantuml":
            r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
            return r.stdout.split("\n")[0] if r.stdout else ""
    except Exception:
        pass
    return "unknown"


def _install_hint(engine: str) -> str:
    hints = {
        "d2": "brew install d2",
        "graphviz": "brew install graphviz",
        "mermaid": "npm install -g @mermaid-js/mermaid-cli (or use npx)",
        "plantuml": "brew install plantuml",
    }
    return hints.get(engine, f"install {engine}")


def _copy_file(src: str, dst: str) -> None:
    """Copy file content."""
    with open(src, "rb") as f_in:
        with open(dst, "wb") as f_out:
            f_out.write(f_in.read())


async def _async_main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for the cap-diagram-server console script."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

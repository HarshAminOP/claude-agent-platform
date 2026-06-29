#!/usr/bin/env python3
"""AST Server MCP — structural code search using ast-grep.

Provides pattern-based AST search, single-file matching, and dry-run refactoring
via the `sg` (ast-grep) CLI tool as a subprocess.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger("cap.ast")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# --- Constants ---
SUBPROCESS_TIMEOUT = 30  # seconds
MAX_PATTERN_LENGTH = 10_000
SUPPORTED_LANGUAGES = {"python", "typescript", "javascript", "go", "rust", "hcl", "yaml"}
# ast-grep uses these lang identifiers
LANG_MAP = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "go": "go",
    "rust": "rust",
    "hcl": "hcl",
    "terraform": "hcl",
    "yaml": "yaml",
}
SG_BIN = "sg"

server = Server("cap-ast")


def _validate_pattern(pattern: str) -> str | None:
    """Return error message if pattern is invalid, else None."""
    if not pattern or not pattern.strip():
        return "Pattern must not be empty."
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"Pattern exceeds maximum length of {MAX_PATTERN_LENGTH} characters."
    return None


def _resolve_lang(lang: str) -> str | None:
    """Resolve language alias to ast-grep language identifier."""
    return LANG_MAP.get(lang.lower())


def _validate_path_exists(path: str) -> str | None:
    """Return error message if path does not exist, else None."""
    if not os.path.exists(path):
        return f"Path does not exist: {path}"
    return None


def _run_sg(args: list[str], cwd: str | None = None) -> dict:
    """Run ast-grep subprocess and return parsed result or error dict."""
    cmd = [SG_BIN] + args
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            cwd=cwd,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"ast-grep timed out after {SUBPROCESS_TIMEOUT}s"}
    except FileNotFoundError:
        return {"error": "ast-grep (sg) binary not found. Install via: brew install ast-grep"}
    except Exception as e:
        return {"error": f"subprocess error: {str(e)}"}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="ast_search",
            description=(
                "Search for code patterns across a directory using ast-grep. "
                "Supports AST patterns with metavariables (e.g., `$X.unwrap()` in Rust, "
                "`console.log($$$)` in JS) or YAML rule definitions. "
                "Returns matches with file paths and line numbers as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "AST pattern to match. Use $X for single node, $$$ for multiple nodes. "
                            "Examples: '$X.unwrap()', 'console.log($$$)', 'if $COND { $$$BODY }'"
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "typescript", "javascript", "go", "rust", "terraform", "hcl", "yaml"],
                        "description": "Language of the code to search.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Directories or files to search. Defaults to current working directory.",
                    },
                    "rule_yaml": {
                        "type": "string",
                        "description": (
                            "Optional: full YAML rule definition for advanced matching. "
                            "When provided, 'pattern' is ignored and this rule is used via --inline-rules."
                        ),
                    },
                },
                "required": ["language"],
            },
        ),
        Tool(
            name="ast_match",
            description=(
                "Match a pattern in a single file and return all matches with line numbers. "
                "More targeted than ast_search — use when you know exactly which file to inspect."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "AST pattern to match.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to match against.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "typescript", "javascript", "go", "rust", "terraform", "hcl", "yaml"],
                        "description": "Language of the file.",
                    },
                },
                "required": ["pattern", "file_path", "language"],
            },
        ),
        Tool(
            name="ast_refactor",
            description=(
                "Dry-run: show what a pattern replacement would produce. "
                "Does NOT modify any files — only returns the preview of replacements. "
                "Use to preview refactoring before applying changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "AST pattern to match (the source pattern).",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Replacement pattern using captured metavariables (e.g., '$X.expect(\"msg\")').",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "typescript", "javascript", "go", "rust", "terraform", "hcl", "yaml"],
                        "description": "Language of the code.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Directories or files to scan for matches.",
                    },
                },
                "required": ["pattern", "replacement", "language"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "ast_search":
            return await _handle_search(arguments)
        elif name == "ast_match":
            return await _handle_match(arguments)
        elif name == "ast_refactor":
            return await _handle_refactor(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_search(args: dict):
    language = args["language"]
    pattern = args.get("pattern")
    rule_yaml = args.get("rule_yaml")
    paths = args.get("paths", ["."])

    if not pattern and not rule_yaml:
        return [TextContent(type="text", text=json.dumps({"error": "Either 'pattern' or 'rule_yaml' must be provided."}))]

    lang = _resolve_lang(language)
    if not lang:
        return [TextContent(type="text", text=json.dumps({"error": f"Unsupported language: {language}. Supported: {sorted(SUPPORTED_LANGUAGES)}"}))]

    # Validate paths exist
    for p in paths:
        err = _validate_path_exists(p)
        if err:
            return [TextContent(type="text", text=json.dumps({"error": err}))]

    if rule_yaml:
        # Validate rule_yaml length
        err = _validate_pattern(rule_yaml)
        if err:
            return [TextContent(type="text", text=json.dumps({"error": err}))]

        # Use scan --inline-rules
        cmd_args = ["scan", "--inline-rules", rule_yaml, "--json=compact"] + paths
    else:
        # Validate pattern
        err = _validate_pattern(pattern)
        if err:
            return [TextContent(type="text", text=json.dumps({"error": err}))]

        cmd_args = ["run", "--pattern", pattern, "--lang", lang, "--json=compact"] + paths

    result = _run_sg(cmd_args)

    if "error" in result:
        return [TextContent(type="text", text=json.dumps({"error": result["error"]}))]

    # Parse JSON output
    matches = _parse_json_output(result["stdout"])

    return [TextContent(type="text", text=json.dumps({
        "matches": matches,
        "count": len(matches),
        "language": lang,
        "pattern": pattern or "(inline rule)",
    }))]


async def _handle_match(args: dict):
    pattern = args["pattern"]
    file_path = args["file_path"]
    language = args["language"]

    # Validate inputs
    err = _validate_pattern(pattern)
    if err:
        return [TextContent(type="text", text=json.dumps({"error": err}))]

    err = _validate_path_exists(file_path)
    if err:
        return [TextContent(type="text", text=json.dumps({"error": err}))]

    lang = _resolve_lang(language)
    if not lang:
        return [TextContent(type="text", text=json.dumps({"error": f"Unsupported language: {language}. Supported: {sorted(SUPPORTED_LANGUAGES)}"}))]

    cmd_args = ["run", "--pattern", pattern, "--lang", lang, "--json=compact", file_path]
    result = _run_sg(cmd_args)

    if "error" in result:
        return [TextContent(type="text", text=json.dumps({"error": result["error"]}))]

    matches = _parse_json_output(result["stdout"])

    return [TextContent(type="text", text=json.dumps({
        "matches": matches,
        "count": len(matches),
        "file": file_path,
        "language": lang,
        "pattern": pattern,
    }))]


async def _handle_refactor(args: dict):
    pattern = args["pattern"]
    replacement = args["replacement"]
    language = args["language"]
    paths = args.get("paths", ["."])

    # Validate inputs
    err = _validate_pattern(pattern)
    if err:
        return [TextContent(type="text", text=json.dumps({"error": err}))]

    err = _validate_pattern(replacement)
    if err:
        return [TextContent(type="text", text=json.dumps({"error": f"Replacement: {err}"}))]

    lang = _resolve_lang(language)
    if not lang:
        return [TextContent(type="text", text=json.dumps({"error": f"Unsupported language: {language}. Supported: {sorted(SUPPORTED_LANGUAGES)}"}))]

    for p in paths:
        err = _validate_path_exists(p)
        if err:
            return [TextContent(type="text", text=json.dumps({"error": err}))]

    # Use --rewrite for dry-run preview (without --update-all, it just shows what would change)
    cmd_args = ["run", "--pattern", pattern, "--rewrite", replacement, "--lang", lang, "--json=compact"] + paths
    result = _run_sg(cmd_args)

    if "error" in result:
        return [TextContent(type="text", text=json.dumps({"error": result["error"]}))]

    matches = _parse_json_output(result["stdout"])

    # Enrich with replacement info
    refactorings = []
    for m in matches:
        refactorings.append({
            "file": m.get("file"),
            "line_start": m.get("line_start"),
            "line_end": m.get("line_end"),
            "original": m.get("text"),
            "replacement": m.get("replacement", replacement),
            "context": m.get("lines"),
        })

    return [TextContent(type="text", text=json.dumps({
        "refactorings": refactorings,
        "count": len(refactorings),
        "language": lang,
        "pattern": pattern,
        "replacement_pattern": replacement,
        "dry_run": True,
        "note": "No files were modified. Use your editor or CLI to apply changes.",
    }))]


def _parse_json_output(stdout: str) -> list[dict]:
    """Parse ast-grep JSON output into a normalized list of match dicts."""
    if not stdout or not stdout.strip():
        return []

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        # Try line-by-line (stream mode)
        raw = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    raw.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not isinstance(raw, list):
        raw = [raw]

    matches = []
    for item in raw:
        match = {
            "text": item.get("text", ""),
            "file": item.get("file", ""),
            "lines": item.get("lines", ""),
            "language": item.get("language", ""),
        }
        # Extract line/column info from range
        rng = item.get("range", {})
        start = rng.get("start", {})
        end = rng.get("end", {})
        match["line_start"] = start.get("line", 0)
        match["column_start"] = start.get("column", 0)
        match["line_end"] = end.get("line", 0)
        match["column_end"] = end.get("column", 0)

        # Include replacement if present
        if "replacement" in item:
            match["replacement"] = item["replacement"]

        matches.append(match)

    return matches


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Fleet Manager MCP Server — health monitoring for registered MCP servers.

Owner of fleet.db. Reports health status and discovers servers.

NOTE: This server cannot restart MCP servers. Claude Code owns all stdio
connections to MCP servers; spawning a subprocess creates an orphan process
that is not connected to Claude Code. Restarts require Claude Code to
reconnect (close/reopen or `claude mcp remove` + `cap init`).

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from cap.lib.config import load_config
from cap.lib.db_init import init_fleet_db
from cap.lib.security import validate_fleet_command

logger = logging.getLogger("cap.fleet")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = load_config()
DATA_DIR = config.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = init_fleet_db(DATA_DIR)
server = Server("cap-fleet")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="fleet_status",
            description="Health status of all or one managed MCP server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "Specific server (optional, shows all if omitted)"},
                },
            },
        ),
        Tool(
            name="fleet_register",
            description="Register a new MCP server with the fleet manager. Command must be whitelisted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {"type": "string", "description": "Binary to execute"},
                    "args": {"type": "array", "items": {"type": "string"}, "default": []},
                    "env": {"type": "object", "default": {}},
                    "health_check": {"type": "object", "description": "Health check config"},
                    "max_restarts": {"type": "integer", "default": 5},
                },
                "required": ["name", "command"],
            },
        ),
        Tool(
            name="fleet_unregister",
            description="Remove a server from fleet management.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="fleet_restart",
            description=(
                "Flag a server for restart. NOTE: MCP servers are managed by Claude Code's "
                "stdio lifecycle — this tool marks the server as needing restart and logs the "
                "event, but the actual restart requires Claude Code to reconnect. Returns "
                "instructions for the user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string", "default": "Manual restart"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="fleet_health_check",
            description=(
                "Check health of registered MCP servers by verifying their PIDs are alive. "
                "Reports status only — cannot restart servers (Claude Code owns stdio connections)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="fleet_discover",
            description="Auto-discover MCP servers from workspace and global config.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                },
                "required": ["workspace"],
            },
        ),
        Tool(
            name="fleet_logs",
            description="Get recent events for a server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "lines": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                },
                "required": ["name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "fleet_status":
            return await _handle_status(arguments)
        elif name == "fleet_register":
            return await _handle_register(arguments)
        elif name == "fleet_unregister":
            return await _handle_unregister(arguments)
        elif name == "fleet_restart":
            return await _handle_restart(arguments)
        elif name == "fleet_health_check":
            return await _handle_health_check(arguments)
        elif name == "fleet_discover":
            return await _handle_discover(arguments)
        elif name == "fleet_logs":
            return await _handle_logs(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_status(args: dict):
    server_name = args.get("server_name")

    if server_name:
        row = db.execute(
            "SELECT name, command, status, pid, last_health_check, restart_count, max_restarts FROM fleet_servers WHERE name = ?",
            (server_name,)
        ).fetchone()
        if not row:
            return [TextContent(type="text", text=json.dumps({"error": f"Server '{server_name}' not found"}))]

        alive = _pid_alive(row[3]) if row[3] else False
        servers = [{
            "name": row[0], "command": row[1], "status": row[2],
            "pid": row[3], "pid_alive": alive,
            "last_health_check": row[4], "restart_count": row[5], "max_restarts": row[6],
        }]
    else:
        rows = db.execute(
            "SELECT name, command, status, pid, last_health_check, restart_count, max_restarts FROM fleet_servers"
        ).fetchall()
        servers = []
        for row in rows:
            alive = _pid_alive(row[3]) if row[3] else False
            servers.append({
                "name": row[0], "command": row[1], "status": row[2],
                "pid": row[3], "pid_alive": alive,
                "last_health_check": row[4], "restart_count": row[5], "max_restarts": row[6],
            })

    return [TextContent(type="text", text=json.dumps({"servers": servers, "count": len(servers)}))]


async def _handle_register(args: dict):
    name = args["name"]
    command = args["command"]
    cmd_args = args.get("args", [])
    env = args.get("env", {})
    health_check = args.get("health_check")
    max_restarts = args.get("max_restarts", 5)

    if not validate_fleet_command([command] + cmd_args):
        return [TextContent(type="text", text=json.dumps({
            "error": f"Command '{command}' not in allowed whitelist. Allowed: python, python3, node, npx, uvx, docker"
        }))]

    db.execute(
        """INSERT INTO fleet_servers (name, command, args, env, health_check, max_restarts)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               command = excluded.command, args = excluded.args,
               env = excluded.env, health_check = excluded.health_check,
               max_restarts = excluded.max_restarts""",
        (name, command, json.dumps(cmd_args), json.dumps(env),
         json.dumps(health_check) if health_check else None, max_restarts)
    )
    db.execute(
        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'registered', ?)",
        (name, f"Registered with command: {command}")
    )
    db.commit()

    logger.info("Server registered: %s (%s)", name, command)
    return [TextContent(type="text", text=json.dumps({"status": "registered", "name": name}))]


async def _handle_unregister(args: dict):
    name = args["name"]

    row = db.execute("SELECT pid FROM fleet_servers WHERE name = ?", (name,)).fetchone()
    if not row:
        return [TextContent(type="text", text=json.dumps({"error": f"Server '{name}' not found"}))]

    if row[0] and _pid_alive(row[0]):
        try:
            os.kill(row[0], signal.SIGTERM)
        except ProcessLookupError:
            pass

    db.execute("DELETE FROM fleet_servers WHERE name = ?", (name,))
    db.execute(
        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'unregistered', 'Removed from fleet')",
        (name,)
    )
    db.commit()

    return [TextContent(type="text", text=json.dumps({"status": "unregistered", "name": name}))]


async def _handle_restart(args: dict):
    name = args["name"]
    reason = args.get("reason", "Manual restart")

    row = db.execute("SELECT name FROM fleet_servers WHERE name = ?", (name,)).fetchone()
    if not row:
        return [TextContent(type="text", text=json.dumps({"error": f"Server '{name}' not found"}))]

    db.execute(
        "UPDATE fleet_servers SET status = 'needs_restart', last_health_check = ? WHERE name = ?",
        (_now(), name)
    )
    db.execute(
        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'restart_requested', ?)",
        (name, reason)
    )
    db.commit()

    logger.info("Server %s flagged for restart. Reason: %s", name, reason)
    return [TextContent(type="text", text=json.dumps({
        "status": "needs_restart",
        "name": name,
        "message": (
            f"Server '{name}' flagged for restart. "
            "To restart: close and reopen Claude Code, or run "
            f"`claude mcp remove {name}` then `cap init`."
        ),
        "note": (
            "MCP servers are managed by Claude Code's stdio lifecycle. "
            "This fleet server cannot spawn a replacement process that Claude Code will connect to."
        ),
    }))]


async def _handle_health_check(args: dict):
    rows = db.execute("SELECT name, pid, status FROM fleet_servers").fetchall()
    results = []
    now = _now()

    for name, pid, status in rows:
        if status == "registered" and not pid:
            results.append({"name": name, "status": "not_started", "healthy": None})
            continue

        alive = _pid_alive(pid) if pid else False
        health_status = "healthy" if alive else "dead"

        if not alive and status == "running":
            db.execute("UPDATE fleet_servers SET status = 'stopped' WHERE name = ?", (name,))
            db.execute(
                "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'died', ?)",
                (name, f"Process {pid} no longer alive")
            )

        db.execute("UPDATE fleet_servers SET last_health_check = ? WHERE name = ?", (now, name))
        results.append({"name": name, "pid": pid, "status": health_status, "healthy": alive})

    db.commit()
    return [TextContent(type="text", text=json.dumps({"results": results, "checked_at": now}))]


async def _handle_discover(args: dict):
    workspace = args["workspace"]
    discovered = []

    from cap.lib.security import validate_workspace

    validate_workspace(workspace)
    workspace_path = Path(workspace).resolve()

    claude_json_paths = [
        workspace_path / ".claude.json",
        Path.home() / ".claude.json",
    ]

    for path in claude_json_paths:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for name, cfg in data.get("mcpServers", {}).items():
                    if name.startswith("cap-"):
                        continue
                    existing = db.execute("SELECT name FROM fleet_servers WHERE name = ?", (name,)).fetchone()
                    if not existing:
                        discovered.append({
                            "name": name,
                            "command": cfg.get("command", ""),
                            "args": cfg.get("args", []),
                            "source": str(path),
                        })
            except (json.JSONDecodeError, KeyError):
                pass

    return [TextContent(type="text", text=json.dumps({
        "discovered": discovered,
        "count": len(discovered),
        "message": f"Found {len(discovered)} unmanaged servers. Use fleet_register to add them.",
    }))]


async def _handle_logs(args: dict):
    name = args["name"]
    lines = args.get("lines", 20)

    events = db.execute(
        "SELECT event_type, message, created_at FROM fleet_events WHERE server_name = ? ORDER BY created_at DESC LIMIT ?",
        (name, lines)
    ).fetchall()

    return [TextContent(type="text", text=json.dumps({
        "server": name,
        "events": [{"type": e[0], "message": e[1], "timestamp": e[2]} for e in events],
    }))]


async def _health_monitor_loop():
    """Background health status reporting loop.

    Checks whether registered server PIDs are still alive and updates DB status.
    Does NOT attempt to restart servers — Claude Code owns all stdio connections
    and a subprocess.Popen restart would create an orphan process.
    """
    interval = config.fleet.health_check_interval_seconds
    while True:
        try:
            rows = db.execute(
                "SELECT name, pid FROM fleet_servers WHERE status = 'running'"
            ).fetchall()

            now = _now()
            for name, pid in rows:
                if not pid or not _pid_alive(pid):
                    logger.warning("Server %s (pid=%s) is dead — marking stopped", name, pid)
                    db.execute(
                        "UPDATE fleet_servers SET status = 'stopped', last_health_check = ? WHERE name = ?",
                        (now, name)
                    )
                    db.execute(
                        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'health_failed', ?)",
                        (name, f"Process {pid} no longer alive")
                    )

            db.commit()
        except Exception as e:
            logger.error("Health monitor error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


async def _async_main():
    health_task = asyncio.create_task(_health_monitor_loop())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        health_task.cancel()


def main():
    """Entry point for the cap-fleet-server console script."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

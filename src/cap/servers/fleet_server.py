#!/usr/bin/env python3
"""Fleet Manager MCP Server — manages external MCP server lifecycle.

Owner of fleet.db. Monitors health, auto-restarts, discovers servers.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config
from lib.db_init import init_fleet_db
from lib.security import validate_fleet_command

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
            description="Restart a managed server.",
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
            description="Run immediate health check on all servers.",
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

    row = db.execute("SELECT command, args, env, pid, restart_count, max_restarts FROM fleet_servers WHERE name = ?", (name,)).fetchone()
    if not row:
        return [TextContent(type="text", text=json.dumps({"error": f"Server '{name}' not found"}))]

    command, cmd_args, env_json, pid, restart_count, max_restarts = row

    if restart_count >= max_restarts:
        return [TextContent(type="text", text=json.dumps({"error": f"Server '{name}' exceeded max restarts ({max_restarts})"}))]

    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(2)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    env = json.loads(env_json) if env_json else {}
    full_env = {**os.environ, **env}
    args_list = json.loads(cmd_args) if cmd_args else []

    from cap.lib.security import validate_fleet_command

    if not validate_fleet_command([command] + args_list):
        return [TextContent(type="text", text=json.dumps({"error": "restart blocked — command not in allowlist", "command": command}))]

    proc = subprocess.Popen(
        [command] + args_list,
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    db.execute(
        "UPDATE fleet_servers SET pid = ?, status = 'running', restart_count = restart_count + 1, last_health_check = ? WHERE name = ?",
        (proc.pid, _now(), name)
    )
    db.execute(
        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'restarted', ?)",
        (name, reason)
    )
    db.commit()

    logger.info("Server restarted: %s (pid=%d) reason=%s", name, proc.pid, reason)
    return [TextContent(type="text", text=json.dumps({"status": "restarted", "name": name, "pid": proc.pid}))]


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
    """Background health monitoring loop."""
    interval = config.fleet.health_check_interval_seconds
    while True:
        try:
            rows = db.execute(
                "SELECT name, pid, restart_count, max_restarts FROM fleet_servers WHERE status = 'running'"
            ).fetchall()

            for name, pid, restart_count, max_restarts in rows:
                if not pid or not _pid_alive(pid):
                    logger.warning("Server %s (pid=%s) is dead", name, pid)
                    db.execute("UPDATE fleet_servers SET status = 'stopped' WHERE name = ?", (name,))
                    db.execute(
                        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'health_failed', 'Process dead')",
                        (name,)
                    )

                    if config.fleet.auto_restart_enabled and restart_count < max_restarts:
                        logger.info("Auto-restarting %s (attempt %d/%d)", name, restart_count + 1, max_restarts)
                        backoff = config.fleet.restart_backoff_base ** restart_count
                        await asyncio.sleep(min(backoff, 60))
                        await _handle_restart({"name": name, "reason": "Auto-restart: health check failed"})

            db.commit()
        except Exception as e:
            logger.error("Health monitor error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


async def main():
    health_task = asyncio.create_task(_health_monitor_loop())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        health_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

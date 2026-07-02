#!/usr/bin/env python3
"""
CAP Enforcement Hook — PreToolUse

Exit 0 = allow, Exit 2 = HARD BLOCK (tool call rejected by Claude Code)

Reads JSON from stdin with: tool_name, tool_input, session_id
Enforces the delegation rule: max 3 distinct files without Agent() delegation.
"""
import json
import os
import sqlite3
import sys
import time

try:
    from cap.config import get_platform_db_path
    DB_PATH = str(get_platform_db_path())
except ImportError:
    # Fallback if cap package not importable (hook may run outside venv)
    _cap_home = os.environ.get("CAP_HOME", os.path.join(os.path.expanduser("~"), ".claude-platform"))
    DB_PATH = os.path.join(_cap_home, "data", "platform.db")
PASSTHROUGH_TTL = 300  # 5 minutes
MAX_STDIN_BYTES = 1_048_576  # 1 MB max input to prevent memory exhaustion


def get_db():
    """Get SQLite connection with WAL mode, ensuring schema exists."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    # Ensure enforcement tables exist (lightweight check: only create if needed)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS enforcement_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            delegated INTEGER NOT NULL DEFAULT 0,
            timestamp REAL NOT NULL,
            UNIQUE(session_id, file_path, delegated)
        );
        CREATE TABLE IF NOT EXISTS enforcement_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            tool_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            workspace TEXT
        );
        CREATE TABLE IF NOT EXISTS passthrough (
            workspace TEXT PRIMARY KEY,
            enabled_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            reason TEXT,
            enabled_by TEXT DEFAULT 'user'
        );
        CREATE TABLE IF NOT EXISTS kb_search_flags (
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
    """)
    return conn


def validate_file_path(file_path: str, workspace: str) -> bool:
    """
    Validate that file_path resolves within the workspace.
    Prevents path traversal attacks via symlinks or ../
    """
    if not file_path or not workspace:
        return False
    try:
        # Resolve both paths to eliminate symlinks and ../ traversal
        resolved_file = os.path.realpath(file_path)
        resolved_workspace = os.path.realpath(workspace)
        # Ensure the resolved path is within the workspace
        return resolved_file.startswith(resolved_workspace + os.sep) or resolved_file == resolved_workspace
    except (ValueError, OSError):
        return False


def check_passthrough(db, workspace: str) -> bool:
    """Check if passthrough mode is active and not expired."""
    row = db.execute(
        "SELECT expires_at FROM passthrough WHERE workspace = ? AND expires_at > ?",
        (workspace, time.time())
    ).fetchone()
    return row is not None


def get_session_file_edits(db, session_id: str) -> set:
    """Get distinct files edited in current session without Agent() delegation."""
    rows = db.execute(
        """SELECT DISTINCT file_path FROM enforcement_edits
           WHERE session_id = ? AND delegated = 0""",
        (session_id,)
    ).fetchall()
    return {r[0] for r in rows}


def record_violation(db, session_id: str, tool_name: str, file_path: str, reason: str):
    """Record an enforcement violation."""
    db.execute(
        """INSERT INTO enforcement_violations
           (session_id, timestamp, tool_name, file_path, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, time.time(), tool_name, file_path, reason)
    )
    db.commit()


def record_edit(db, session_id: str, file_path: str, delegated: bool):
    """Record a file edit event."""
    db.execute(
        """INSERT OR IGNORE INTO enforcement_edits
           (session_id, file_path, delegated, timestamp)
           VALUES (?, ?, ?, ?)""",
        (session_id, file_path, int(delegated), time.time())
    )
    db.commit()


def main():
    try:
        raw_input = sys.stdin.read(MAX_STDIN_BYTES)
        input_data = json.loads(raw_input)
    except (json.JSONDecodeError, ValueError, OSError):
        # Malformed input — fail open to avoid blocking legitimate tool calls
        # but log to stderr for observability
        print("CAP pretool: failed to parse stdin JSON", file=sys.stderr)
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    # Sanitize session_id to prevent injection via crafted session identifiers
    if not isinstance(session_id, str) or len(session_id) > 256:
        session_id = "unknown"

    # Ensure DB directory exists with restricted permissions
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, mode=0o700, exist_ok=True)

    try:
        db = get_db()
    except sqlite3.Error:
        # If DB is unavailable, fail open (don't block user's work)
        sys.exit(0)

    # Routing enforcement — track Agent vs cap_orchestrate usage
    try:
        if tool_name in ("Agent", "agent", "cap_orchestrate"):
            from cap.enforcement.routing_enforcer import check_agent_routing
            check_agent_routing(tool_name, tool_input)
    except Exception:
        pass  # Never break the hook for routing telemetry

    # Passthrough check — if active, allow everything
    workspace = os.getcwd()
    if check_passthrough(db, workspace):
        sys.exit(0)

    # ── Bash grep/find enforcement: knowledge_search must be called first ────
    if tool_name == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        # Only enforce when the command IS a search tool, not when "find" appears in args
        cmd_stripped = command.lstrip()
        first_word = cmd_stripped.split()[0] if cmd_stripped.split() else ""
        search_commands = {"grep", "find", "rg", "ag", "ack"}
        is_code_search = first_word in search_commands
        # Also catch piped patterns like "cat | grep" or "... | grep"
        if not is_code_search and "| grep " in command:
            is_code_search = True
        # Exclude git commands entirely — git grep, git log, etc.
        is_exempt = cmd_stripped.startswith("git ") or first_word in {"npm", "pip", "uv", "docker", "kubectl"}

        if is_code_search and not is_exempt:
            try:
                row = db.execute(
                    """SELECT 1 FROM kb_search_flags
                       WHERE session_id = ? AND timestamp > ?""",
                    (session_id, time.time() - 600)  # Flag valid for 10 minutes
                ).fetchone()
                if row is None:
                    reason = (
                        "BLOCKED: You must call mcp__cap-knowledge__knowledge_search BEFORE "
                        "using grep/find/rg. The knowledge base is faster and more complete. "
                        "Call knowledge_search first, then use grep only if KB results are insufficient."
                    )
                    record_violation(db, session_id, tool_name, command[:100], reason)
                    print(reason, file=sys.stderr)
                    sys.exit(2)
            except sqlite3.OperationalError:
                pass  # Table doesn't exist yet — fail open
        sys.exit(0)

    # Only enforce on file-writing tools
    WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
    if tool_name not in WRITE_TOOLS:
        sys.exit(0)

    # Extract file path from tool input
    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Path traversal protection: ensure file is within workspace
    if not validate_file_path(file_path, workspace):
        reason = (
            f"BLOCKED: file_path '{file_path}' resolves outside workspace '{workspace}'. "
            f"Potential path traversal attack."
        )
        record_violation(db, session_id, tool_name, file_path, reason)
        print(reason, file=sys.stderr)
        sys.exit(2)

    # Use BEGIN IMMEDIATE to prevent TOCTOU race between check and insert.
    # This acquires a write lock atomically, ensuring no concurrent process
    # can modify the edit count between our read and our write.
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # If we can't get a lock (another hook is running), fail open
        sys.exit(0)

    try:
        # Check if this edit is happening inside an Agent() context
        agent_context = db.execute(
            "SELECT 1 FROM agent_contexts WHERE session_id = ? AND active = 1",
            (session_id,)
        ).fetchone()
        delegated = agent_context is not None

        # Record this edit
        db.execute(
            """INSERT OR IGNORE INTO enforcement_edits
               (session_id, file_path, delegated, timestamp)
               VALUES (?, ?, ?, ?)""",
            (session_id, file_path, int(delegated), time.time())
        )

        # If delegated, allow
        if delegated:
            db.commit()
            sys.exit(0)

        # Check distinct file count for non-delegated edits (inside the same txn)
        rows = db.execute(
            """SELECT DISTINCT file_path FROM enforcement_edits
               WHERE session_id = ? AND delegated = 0""",
            (session_id,)
        ).fetchall()
        edited_files = {r[0] for r in rows}
        edited_files.add(file_path)  # include current

        if len(edited_files) >= 3:
            reason = (
                f"BLOCKED: Editing {len(edited_files)} distinct files without Agent() delegation. "
                f"Files: {', '.join(sorted(edited_files)[:5])}. "
                f"Use Agent({{ subagent_type: 'orchestrator', ... }}) to delegate, "
                f"or run `cap passthrough` for temporary bypass."
            )
            # Record violation inside the same transaction
            db.execute(
                """INSERT INTO enforcement_violations
                   (session_id, timestamp, tool_name, file_path, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, time.time(), tool_name, file_path, reason)
            )
            db.commit()
            # Output block reason to stderr (shown to user)
            print(reason, file=sys.stderr)
            sys.exit(2)

        db.commit()
    except Exception:
        db.rollback()
        # On any error, fail open
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()

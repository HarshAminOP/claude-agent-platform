#!/usr/bin/env python3
"""
CAP PostToolUse Hook — Sync trigger and state updates.

Reads JSON from stdin with: tool_name, tool_output
Always exits 0 (never blocks).

Responsibilities:
- If tool was Agent(): mark agent_contexts as complete, reset edit counter
- If tool was Bash and output contains 'git pull' or 'git fetch': record sync_trigger
- If tool was Edit/Write: invalidate witness manifest for that file
- Record agent health event (tool_call) with timestamp and estimated tokens
"""
import json
import os
import sqlite3
import sys
import time

DB_PATH = os.path.expanduser("~/.cap/cap.db")
MAX_STDIN_BYTES = 2_097_152  # 2 MB max input (outputs can be larger than inputs)


def get_db():
    """Get SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token. Capped to prevent int overflow."""
    if not text:
        return 0
    # Cap at 10M chars to avoid overflow in token math
    capped_len = min(len(text), 10_000_000)
    return max(1, capped_len // 4)


def main():
    try:
        raw_input = sys.stdin.read(MAX_STDIN_BYTES)
        input_data = json.loads(raw_input)
    except (json.JSONDecodeError, ValueError, OSError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_output = input_data.get("tool_output", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    # Sanitize session_id
    if not isinstance(session_id, str) or len(session_id) > 256:
        session_id = "unknown"

    # Ensure DB directory exists with restricted permissions
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, mode=0o700, exist_ok=True)

    try:
        db = get_db()
    except Exception:
        sys.exit(0)

    now = time.time()

    # ── Agent() completion: mark context complete, reset edit counter ──────────
    if tool_name == "Agent":
        try:
            # Mark all active agent contexts for this session as complete
            db.execute(
                "UPDATE agent_contexts SET active = 0 WHERE session_id = ? AND active = 1",
                (session_id,)
            )
            # Reset the edit counter (mark existing non-delegated edits as delegated)
            db.execute(
                "UPDATE enforcement_edits SET delegated = 1 WHERE session_id = ? AND delegated = 0",
                (session_id,)
            )
            db.commit()
        except sqlite3.OperationalError:
            pass  # Tables may not exist yet if cap init hasn't run

    # ── Bash with git pull/fetch: record sync trigger ─────────────────────────
    if tool_name == "Bash":
        # Check both command input and output for git operations
        command_str = ""
        if isinstance(tool_input, dict):
            command_str = str(tool_input.get("command", ""))
        output_str = str(tool_output) if tool_output else ""
        combined = command_str + " " + output_str
        if any(cmd in combined for cmd in ["git pull", "git merge", "git checkout", "git rebase", "git fetch"]):
            try:
                db.execute(
                    "INSERT INTO sync_triggers (timestamp, trigger_type, detail) VALUES (?, ?, ?)",
                    (now, "git_post_pull", f"Detected in Bash output at {now}")
                )
                db.commit()
            except Exception:
                pass  # sync_triggers table may not exist yet

    # ── Edit/Write: invalidate witness manifest ───────────────────────────────
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        file_path = ""
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "")
        if file_path:
            try:
                db.execute(
                    "DELETE FROM witness_manifests WHERE file_path = ?",
                    (file_path,)
                )
                db.commit()
            except Exception:
                pass  # witness_manifests table may not exist yet

    # ── Record agent health event ─────────────────────────────────────────────
    estimated_tokens = estimate_tokens(str(tool_output))
    try:
        db.execute(
            """INSERT INTO agent_health_events
               (agent_id, event_type, tool_name, timestamp, estimated_tokens)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, "tool_call", tool_name, now, estimated_tokens)
        )
        db.commit()
    except Exception:
        pass  # agent_health_events table may not exist yet

    sys.exit(0)


if __name__ == "__main__":
    main()

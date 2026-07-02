"""CAP Harness Governance — Policy enforcement, manifest integrity, and audit logging.

Implements:
- HarnessPolicy: loaded from .harness/mcp-policy.json (default-deny posture)
- Dangerous-content scanning against configurable regex patterns
- Budget enforcement via cost_meter integration
- Manifest generation + verification (SHA-256 file hashes for drift detection)
- Audit log recording for every tool invocation

Reference: Ruflo ADR-150 mcp-policy.json schema, adapted for CAP Python harness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.governance")

# ---------------------------------------------------------------------------
# DB helpers — reuse platform.db
# ---------------------------------------------------------------------------

try:
    from cap.harness.agent_store import PLATFORM_DB_PATH
except ImportError:
    from cap.config import get_platform_db_path
    PLATFORM_DB_PATH = get_platform_db_path()

_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    tool_name TEXT NOT NULL,
    agent_id TEXT,
    input_summary TEXT,
    success INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(timestamp);
"""


def _get_audit_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open platform.db with audit_log table, return connection."""
    path = db_path or PLATFORM_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_AUDIT_DDL)
    return conn


# ---------------------------------------------------------------------------
# HarnessPolicy
# ---------------------------------------------------------------------------

_DEFAULT_DANGEROUS_PATTERNS: list[str] = [
    r"rm\s+-rf",
    r"sudo\b",
    r"git\s+push.*--force",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM.*WHERE\s+1",
    r"chmod\s+777",
    r"curl.*\|.*sh",
    r"eval\(",
]


@dataclass
class HarnessPolicy:
    """MCP governance policy — loaded from .harness/mcp-policy.json or defaults."""

    default_deny: bool = True
    allow_shell: bool = False
    allow_network: bool = False
    allow_file_write: bool = False
    require_approval_for_dangerous: bool = True
    audit_log: bool = True
    tool_timeout_ms: int = 600_000
    max_tool_calls_per_turn: int = 200
    daily_budget_usd: float = 5.0
    dangerous_patterns: list[str] = field(default_factory=lambda: list(_DEFAULT_DANGEROUS_PATTERNS))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_policy(workspace_path: Optional[Path] = None) -> HarnessPolicy:
    """Load policy from .harness/mcp-policy.json; fall back to defaults.

    Parameters
    ----------
    workspace_path:
        Root directory containing .harness/. When None, uses cwd.

    Returns
    -------
    HarnessPolicy
    """
    if workspace_path is None:
        workspace_path = Path.cwd()

    policy_file = workspace_path / ".harness" / "mcp-policy.json"

    if not policy_file.is_file():
        logger.debug("No policy file at %s — using defaults", policy_file)
        return HarnessPolicy()

    try:
        raw = json.loads(policy_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to parse policy file %s: %s — using defaults", policy_file, exc)
        return HarnessPolicy()

    return HarnessPolicy(
        default_deny=raw.get("defaultDeny", True),
        allow_shell=raw.get("allowShell", False),
        allow_network=raw.get("allowNetwork", False),
        allow_file_write=raw.get("allowFileWrite", False),
        require_approval_for_dangerous=raw.get("requireApprovalForDangerous", True),
        audit_log=raw.get("auditLog", True),
        tool_timeout_ms=int(raw.get("toolTimeoutMs", 600_000)),
        max_tool_calls_per_turn=int(raw.get("maxToolCallsPerTurn", 200)),
        daily_budget_usd=float(raw.get("dailyBudgetUsd", raw.get("daily_budget_usd", 5.0))),
        dangerous_patterns=raw.get("dangerousPatterns", list(_DEFAULT_DANGEROUS_PATTERNS)),
    )


def check_dangerous(content: str, policy: Optional[HarnessPolicy] = None) -> list[str]:
    """Scan content against dangerous_patterns regex list.

    Parameters
    ----------
    content:
        Arbitrary text to scan (command, prompt, tool input).
    policy:
        Policy instance. Uses defaults when None.

    Returns
    -------
    list[str]
        Matched pattern strings. Empty list means content is safe.
    """
    if policy is None:
        policy = HarnessPolicy()

    matched: list[str] = []
    for pattern in policy.dangerous_patterns:
        try:
            if re.search(pattern, content, re.IGNORECASE):
                matched.append(pattern)
        except re.error as exc:
            logger.warning("Invalid dangerous_pattern regex %r: %s", pattern, exc)

    return matched


def enforce_budget(policy: Optional[HarnessPolicy] = None, db_path: Optional[Path] = None) -> dict:
    """Check today's spend against daily_budget_usd.

    Parameters
    ----------
    policy:
        Policy instance with daily_budget_usd. Uses defaults when None.
    db_path:
        Optional DB path passed to cost_meter (for testing).

    Returns
    -------
    dict
        {allowed: bool, remaining_usd: float, spent_usd: float}
    """
    if policy is None:
        policy = HarnessPolicy()

    try:
        from cap.harness.cost_meter import budget_remaining
        kwargs: dict = {"daily_limit_usd": policy.daily_budget_usd}
        if db_path is not None:
            kwargs["db"] = sqlite3.connect(str(db_path))
        remaining = budget_remaining(**kwargs)
        spent = round(policy.daily_budget_usd - remaining, 6)
    except Exception as exc:
        logger.warning("enforce_budget: cost_meter unavailable: %s", exc)
        # Fallback: read directly from audit_log count or assume allowed
        remaining = policy.daily_budget_usd
        spent = 0.0

    return {
        "allowed": remaining > 0,
        "remaining_usd": round(remaining, 6),
        "spent_usd": spent,
    }


def generate_manifest(workspace_path: Optional[Path] = None) -> dict:
    """Compute SHA-256 hashes of key files and return manifest dict.

    Files checked:
    - .harness/mcp-policy.json
    - pyproject.toml
    - src/cap/harness/__init__.py

    Returns
    -------
    dict
        {template_version, generated_at, file_hashes: {path: sha256_hex}}
    """
    if workspace_path is None:
        workspace_path = Path.cwd()

    targets = [
        ".harness/mcp-policy.json",
        "pyproject.toml",
        "src/cap/harness/__init__.py",
    ]

    file_hashes: dict[str, str] = {}
    for rel in targets:
        fp = workspace_path / rel
        if fp.is_file():
            content = fp.read_bytes()
            file_hashes[rel] = hashlib.sha256(content).hexdigest()
        else:
            file_hashes[rel] = "MISSING"

    return {
        "template_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_hashes": file_hashes,
    }


def write_manifest(workspace_path: Optional[Path] = None) -> Path:
    """Generate manifest and write to .harness/manifest.json.

    Returns
    -------
    Path
        Path to the written manifest file.
    """
    if workspace_path is None:
        workspace_path = Path.cwd()

    manifest = generate_manifest(workspace_path)
    harness_dir = workspace_path / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = harness_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    logger.info("Wrote manifest to %s", manifest_path)
    return manifest_path


def verify_manifest(workspace_path: Optional[Path] = None) -> dict:
    """Read .harness/manifest.json, recompute hashes, compare.

    Returns
    -------
    dict
        {valid: bool, drift: [list of files whose hashes changed]}
    """
    if workspace_path is None:
        workspace_path = Path.cwd()

    manifest_path = workspace_path / ".harness" / "manifest.json"

    if not manifest_path.is_file():
        return {"valid": False, "drift": ["manifest.json MISSING"]}

    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"valid": False, "drift": [f"manifest.json UNREADABLE: {exc}"]}

    stored_hashes = stored.get("file_hashes", {})
    current = generate_manifest(workspace_path)
    current_hashes = current["file_hashes"]

    drift: list[str] = []
    for path, expected_hash in stored_hashes.items():
        actual_hash = current_hashes.get(path, "MISSING")
        if actual_hash != expected_hash:
            drift.append(path)

    return {"valid": len(drift) == 0, "drift": drift}


def record_audit(
    tool_name: str,
    agent_id: Optional[str] = None,
    input_summary: Optional[str] = None,
    success: bool = True,
    db_path: Optional[Path] = None,
) -> None:
    """Append an entry to the audit_log table.

    Parameters
    ----------
    tool_name:
        Name of the MCP tool invoked.
    agent_id:
        Identifier of the calling agent (may be None for system calls).
    input_summary:
        Truncated summary of the tool input (max 2000 chars stored).
    success:
        Whether the call succeeded.
    """
    try:
        conn = _get_audit_conn(db_path)
    except Exception as exc:
        logger.warning("record_audit: db unavailable: %s", exc)
        return

    try:
        entry_id = uuid.uuid4().hex
        ts = time.time()
        summary = (input_summary or "")[:2000]

        conn.execute(
            """INSERT INTO audit_log (id, timestamp, tool_name, agent_id, input_summary, success)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entry_id, ts, tool_name, agent_id, summary, 1 if success else 0),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("record_audit: write failed: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass

"""Input sanitization and security utilities for the Claude Agent Platform."""

import logging
import os
import re
import stat
from pathlib import Path

logger = logging.getLogger("platform.security")

INJECTION_PATTERNS = [
    # Prompt injection
    r"(?i)ignore\s+(all\s+)?previous\s+instructions",
    r"(?i)system:\s*you\s+are\s+now",
    r"(?i)<\s*system\s*>",
    r"(?i)forget\s+(everything|all)",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)you\s+are\s+now\s+in\s+developer\s+mode",
    r"(?i)simulate\s+being\s+DAN",
    r"(?i)your\s+new\s+system\s+prompt\s+is",
    # SQL injection
    r"(?i)(?:;\s*DROP\s+TABLE|UNION\s+SELECT|'\s*OR\s+'[^']*'\s*=\s*')",
    # XSS
    r"(?i)<\s*script[^>]*>",
    r"(?i)<!--\s*#\s*exec\s+cmd\s*=",
    # Template injection
    r"\{\{[^}]*\d+\s*[*+\-/]\s*\d+[^}]*\}\}",
    # JNDI / Log4Shell
    r"(?i)\$\{jndi:",
    # Shellshock
    r"\(\)\s*\{\s*:\s*;\s*\}\s*;",
    # Python code injection
    r"(?i)__import__\s*\(",
    r"(?i)eval\s*\(\s*compile\s*\(",
]

_WORKSPACE_RE = re.compile(r"^[a-zA-Z0-9/_.\-~]+$")

_COMPILED_INJECTION = [re.compile(p) for p in INJECTION_PATTERNS]

# ANSI CSI sequences and standalone ESC + single character
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|[^[])")

ALLOWED_FLEET_COMMANDS: frozenset = frozenset(
    {"python", "python3", "node", "npx", "uvx", "docker"}
)


def sanitize_content(
    content: str,
    field_name: str = "content",
    max_length: int = 1_048_576,
    strict: bool = True,
) -> str:
    if not content or not content.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")

    if len(content) > max_length:
        content = content[:max_length]

    content = content.replace("\x00", "")
    content = _ANSI_RE.sub("", content)

    for pattern in _COMPILED_INJECTION:
        if pattern.search(content):
            if strict:
                raise ValueError(
                    f"Prompt injection detected in {field_name} "
                    f"(matched: {pattern.pattern})"
                )
            logger.warning(
                "Potential prompt injection detected in %s (pattern: %s)",
                field_name,
                pattern.pattern,
            )

    return content


def validate_workspace(workspace: str) -> str:
    if not workspace or len(workspace) > 512:
        raise ValueError("workspace must be 1-512 characters")
    if not _WORKSPACE_RE.match(workspace):
        raise ValueError(
            f"workspace contains invalid characters: {workspace!r}"
        )
    return workspace


def validate_path(path: str, allowed_root: str) -> str:
    resolved = os.path.realpath(path)
    real_root = os.path.realpath(allowed_root)

    try:
        common = os.path.commonpath([resolved, real_root])
    except ValueError:
        # On Windows, commonpath raises ValueError for paths on different drives.
        raise ValueError(
            f"Path traversal detected: {path!r} is outside allowed root {allowed_root!r}"
        )

    if common != real_root:
        raise ValueError(
            f"Path traversal detected: {path!r} resolves to {resolved!r}, "
            f"which is outside allowed root {allowed_root!r}"
        )

    return resolved


def enforce_db_permissions(db_path: Path) -> None:
    _enforce_600(db_path)

    for suffix in ("-wal", "-shm"):
        sibling = db_path.parent / (db_path.name + suffix)
        if sibling.exists():
            _enforce_600(sibling)


def _enforce_600(path: Path) -> None:
    current = stat.S_IMODE(path.stat().st_mode)
    if current != 0o600:
        logger.warning(
            "Fixing insecure permissions on %s: %o → 600",
            path,
            current,
        )
        path.chmod(0o600)


def validate_fleet_command(command: list[str]) -> bool:
    if not command:
        return False

    binary = os.path.basename(command[0])
    if binary not in ALLOWED_FLEET_COMMANDS:
        logger.warning(
            "Fleet command rejected — binary %r not in allowlist", binary
        )
        return False

    return True

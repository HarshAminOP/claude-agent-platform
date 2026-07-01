"""CAP Harness Input Validation — sanitization for all harness tool inputs.

Prevents injection attacks, path traversal, and storage corruption.
Every public function raises ValueError on invalid input with a descriptive message.

Reference: Ruflo @claude-flow/shared/src/security/input-validation.ts
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
_SHELL_META_RE = re.compile(r"[;&|`$(){}<>!\[\]\\'\"\n\r]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f  ]")
_NULL_BYTE_RE = re.compile(r"\x00")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_identifier(value: str, field_name: str = "id") -> str:
    """Validate that value is a safe identifier.

    Rules:
    - Only alphanumeric characters, underscores, and hyphens
    - Maximum 64 characters
    - Non-empty

    Parameters
    ----------
    value:
        The string to validate.
    field_name:
        Human-readable name of the field (for error messages).

    Returns
    -------
    str
        The validated (stripped) value.

    Raises
    ------
    ValueError
        If the value does not pass validation.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name}: must be a string, got {type(value).__name__}")

    value = value.strip()

    if not value:
        raise ValueError(f"{field_name}: must not be empty")

    if len(value) > 64:
        raise ValueError(f"{field_name}: must be at most 64 characters, got {len(value)}")

    if not _IDENTIFIER_RE.match(value):
        raise ValueError(
            f"{field_name}: must contain only alphanumeric characters, underscores, "
            f"and hyphens. Got: {value[:20]!r}"
        )

    return value


def validate_text(value: str, max_length: int = 10000, field_name: str = "text") -> str:
    """Validate and sanitize free-text input.

    Rules:
    - Strip null bytes
    - Limit to max_length characters
    - No shell metacharacters in first 200 chars (prevents injection in summaries)

    Parameters
    ----------
    value:
        The string to validate.
    max_length:
        Maximum allowed length after stripping.
    field_name:
        Human-readable name of the field.

    Returns
    -------
    str
        The sanitized value.

    Raises
    ------
    ValueError
        If validation fails.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name}: must be a string, got {type(value).__name__}")

    # Strip null bytes
    value = _NULL_BYTE_RE.sub("", value)

    # Enforce length limit
    if len(value) > max_length:
        raise ValueError(
            f"{field_name}: exceeds maximum length of {max_length} characters "
            f"(got {len(value)})"
        )

    # Check first 200 chars for shell metacharacters
    prefix = value[:200]
    meta_match = _SHELL_META_RE.search(prefix)
    if meta_match:
        raise ValueError(
            f"{field_name}: contains shell metacharacter {meta_match.group()!r} "
            f"in the first 200 characters (potential injection)"
        )

    return value


def validate_path(value: str, field_name: str = "path") -> str:
    """Validate a file path — no traversal attacks allowed.

    Rules:
    - No ".." components (path traversal)
    - No null bytes
    - Must be either relative (no leading /) or absolute without traversal
    - Maximum 4096 characters

    Parameters
    ----------
    value:
        The path string to validate.
    field_name:
        Human-readable name of the field.

    Returns
    -------
    str
        The validated path string.

    Raises
    ------
    ValueError
        If the path contains traversal characters or is otherwise invalid.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name}: must be a string, got {type(value).__name__}")

    value = value.strip()

    if not value:
        raise ValueError(f"{field_name}: must not be empty")

    if len(value) > 4096:
        raise ValueError(f"{field_name}: path too long ({len(value)} > 4096)")

    # No null bytes
    if "\x00" in value:
        raise ValueError(f"{field_name}: path contains null bytes")

    # Normalize separators for consistent checking
    normalized = value.replace("\\", "/")

    # Check for path traversal
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError(f"{field_name}: path traversal detected (contains '..')")

    # Also check for encoded traversal attempts
    if "%2e%2e" in value.lower() or "%2f" in value.lower():
        raise ValueError(f"{field_name}: encoded path traversal detected")

    return value


def sanitize_for_storage(value: str, max_length: int = 2000) -> str:
    """Prepare a string for safe SQLite storage.

    Operations:
    - Truncate to max_length
    - Strip control characters (except newline and tab)
    - Strip null bytes

    Parameters
    ----------
    value:
        Input text.
    max_length:
        Maximum stored length.

    Returns
    -------
    str
        Sanitized string safe for SQLite TEXT columns.
    """
    if not isinstance(value, str):
        value = str(value)

    # Remove null bytes
    value = _NULL_BYTE_RE.sub("", value)

    # Remove control characters except \n and \t
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)

    # Truncate
    if len(value) > max_length:
        value = value[:max_length]

    return value

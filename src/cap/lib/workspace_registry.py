"""Workspace and endpoint registry backed by harness-config.json.

This module provides CRUD operations for workspace and endpoint configuration
stored in harness-config.json under the top-level "workspaces" and "endpoints"
keys.  It is the single source of truth for which local paths are managed and
which remote endpoints (GitHub, GitLab, …) are configured for auto-clone.

All public functions are thread-safe for concurrent reads but callers are
responsible for avoiding concurrent writes (the daemon serialises writes).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.workspace_registry")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYNC_FREQUENCY = "5m"
_DEFAULT_EXCLUDE_PATTERNS = [
    ".git", "node_modules", "__pycache__", ".terraform", ".venv",
    "venv", "vendor", "target", "dist", "build",
]
_DEFAULT_FILE_EXTENSIONS = [
    "*.py", "*.ts", "*.js", "*.tf", "*.yaml", "*.yml", "*.json",
    "*.md", "*.toml", "*.sh", "*.go", "*.java", "*.rs", "*.hcl",
]


# ---------------------------------------------------------------------------
# Low-level read/write
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    """Return the path to harness-config.json."""
    from cap.config import get_harness_config_path
    return get_harness_config_path()


def load_config() -> dict:
    """Read harness-config.json and return the full config dict.

    Returns an empty dict (not raising) if the file does not yet exist so
    callers that run before ``cap init`` can still bootstrap safely.
    """
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("load_config: failed to read %s: %s", path, exc)
        return {}


def save_config(config: dict) -> None:
    """Write *config* back to harness-config.json.

    Creates parent directories if they do not yet exist.

    Args:
        config: The full config dict to persist.

    Raises:
        OSError: If the file cannot be written.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    logger.debug("save_config: wrote %s", path)


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

def list_workspaces() -> list[dict]:
    """Return all configured workspace entries.

    Returns:
        List of workspace dicts.  Each dict has at minimum ``path`` and
        ``sync_frequency`` keys.  Returns an empty list when none are
        configured.
    """
    cfg = load_config()
    return cfg.get("workspaces", [])


def get_workspace(path: str) -> Optional[dict]:
    """Return the workspace entry for *path*, or ``None`` if not found.

    Args:
        path: Absolute path string.

    Returns:
        The workspace dict, or ``None``.
    """
    resolved = str(Path(path).resolve())
    for ws in list_workspaces():
        if str(Path(ws["path"]).resolve()) == resolved:
            return ws
    return None


def add_workspace(
    path: str,
    auto_added: bool = False,
    **settings,
) -> dict:
    """Add *path* as a managed workspace if it is not already registered.

    Idempotent: if *path* already exists, the existing entry is returned
    unchanged (settings are NOT overwritten).

    Args:
        path: Absolute directory path.
        auto_added: Set to ``True`` when called automatically from
            ``session_start`` so the registry can distinguish user-managed
            entries from auto-discovered ones.
        **settings: Optional overrides for ``sync_frequency``,
            ``include_patterns``, ``exclude_patterns``, and ``depth``.

    Returns:
        The workspace entry dict (existing or newly created).
    """
    resolved = str(Path(path).resolve())
    cfg = load_config()
    workspaces: list[dict] = cfg.setdefault("workspaces", [])

    # Idempotency: return existing entry if already present.
    for ws in workspaces:
        if str(Path(ws["path"]).resolve()) == resolved:
            logger.debug("add_workspace: %s already registered", resolved)
            return ws

    entry: dict = {
        "path": resolved,
        "sync_frequency": settings.get("sync_frequency", _DEFAULT_SYNC_FREQUENCY),
        "include_patterns": settings.get("include_patterns", _DEFAULT_FILE_EXTENSIONS),
        "exclude_patterns": settings.get("exclude_patterns", _DEFAULT_EXCLUDE_PATTERNS),
        "depth": settings.get("depth", None),
        "last_synced": None,
        "auto_added": auto_added,
    }

    workspaces.append(entry)
    save_config(cfg)
    logger.info("add_workspace: registered %s (auto_added=%s)", resolved, auto_added)
    return entry


def remove_workspace(path: str) -> bool:
    """Remove the workspace entry for *path*.

    Args:
        path: Absolute directory path.

    Returns:
        ``True`` if an entry was found and removed, ``False`` otherwise.
    """
    resolved = str(Path(path).resolve())
    cfg = load_config()
    workspaces: list[dict] = cfg.get("workspaces", [])
    original_len = len(workspaces)

    cfg["workspaces"] = [
        ws for ws in workspaces
        if str(Path(ws["path"]).resolve()) != resolved
    ]

    if len(cfg["workspaces"]) == original_len:
        logger.debug("remove_workspace: %s not found", resolved)
        return False

    save_config(cfg)
    logger.info("remove_workspace: removed %s", resolved)
    return True


def update_workspace(path: str, **settings) -> Optional[dict]:
    """Update settings for an existing workspace entry.

    Only keys present in *settings* are updated; other keys are left
    untouched.

    Args:
        path: Absolute directory path.
        **settings: Fields to update (e.g., ``sync_frequency="10m"``).

    Returns:
        The updated workspace dict, or ``None`` if *path* was not found.
    """
    resolved = str(Path(path).resolve())
    cfg = load_config()
    workspaces: list[dict] = cfg.get("workspaces", [])

    for ws in workspaces:
        if str(Path(ws["path"]).resolve()) == resolved:
            ws.update(settings)
            save_config(cfg)
            logger.info("update_workspace: updated %s keys=%s", resolved, list(settings))
            return ws

    logger.debug("update_workspace: %s not found", resolved)
    return None


def mark_workspace_synced(path: str) -> None:
    """Update ``last_synced`` for *path* to the current UTC timestamp.

    Silently does nothing if *path* is not in the registry.

    Args:
        path: Absolute directory path.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = update_workspace(path, last_synced=now)
    if result is None:
        logger.debug("mark_workspace_synced: %s not in registry — skipped", path)


# ---------------------------------------------------------------------------
# Endpoint CRUD
# ---------------------------------------------------------------------------

def list_endpoints() -> list[dict]:
    """Return all configured remote endpoint entries.

    Returns:
        List of endpoint dicts.  Each dict has at minimum ``type`` and
        ``org`` keys.  Returns an empty list when none are configured.
    """
    cfg = load_config()
    return cfg.get("endpoints", [])


def get_endpoint(org: str) -> Optional[dict]:
    """Return the endpoint entry for *org*, or ``None`` if not found.

    Args:
        org: The organisation name (e.g. ``"moia-oss"``).

    Returns:
        The endpoint dict, or ``None``.
    """
    for ep in list_endpoints():
        if ep.get("org") == org:
            return ep
    return None


def add_endpoint(
    endpoint_type: str,
    org: str,
    ssh_endpoint: str,
    **settings,
) -> dict:
    """Add a remote endpoint configuration.

    Idempotent: if an entry with the same *org* already exists, it is
    returned unchanged.

    Args:
        endpoint_type: ``"github"`` or ``"gitlab"``.
        org: GitHub/GitLab organisation or group name.
        ssh_endpoint: SSH endpoint base, e.g. ``"git@github.com"``.
        **settings: Optional overrides for ``auto_clone``,
            ``clone_base_path``, and ``discovery_frequency``.

    Returns:
        The endpoint entry dict (existing or newly created).
    """
    cfg = load_config()
    endpoints: list[dict] = cfg.setdefault("endpoints", [])

    # Idempotency check.
    for ep in endpoints:
        if ep.get("org") == org:
            logger.debug("add_endpoint: %s already registered", org)
            return ep

    entry: dict = {
        "type": endpoint_type,
        "org": org,
        "ssh_endpoint": ssh_endpoint,
        "auto_clone": settings.get("auto_clone", True),
        "clone_base_path": settings.get("clone_base_path", str(Path.home())),
        "discovery_frequency": settings.get("discovery_frequency", "1h"),
    }

    endpoints.append(entry)
    save_config(cfg)
    logger.info("add_endpoint: registered %s org=%s", endpoint_type, org)
    return entry


def remove_endpoint(org: str) -> bool:
    """Remove the endpoint entry for *org*.

    Args:
        org: The organisation name.

    Returns:
        ``True`` if an entry was found and removed, ``False`` otherwise.
    """
    cfg = load_config()
    endpoints: list[dict] = cfg.get("endpoints", [])
    original_len = len(endpoints)

    cfg["endpoints"] = [ep for ep in endpoints if ep.get("org") != org]

    if len(cfg["endpoints"]) == original_len:
        logger.debug("remove_endpoint: %s not found", org)
        return False

    save_config(cfg)
    logger.info("remove_endpoint: removed %s", org)
    return True

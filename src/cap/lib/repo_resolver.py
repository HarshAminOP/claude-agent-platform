"""Auto-resolve and clone missing dependent repos from GitHub org.

When the knowledge graph detects a dependency on a repo not present locally,
this module clones it (shallow, default branch) into the configured clone_base_path,
then triggers an incremental knowledge sync to index its contents.

Config lives in [github] section of config.toml:
    [github]
    org = "your-github-org"
    clone_base_path = "/path/to/your/workspace"
    use_ssh = true
    auto_clone_on_missing_dep = true
    max_auto_clones_per_session = 10
    clone_depth = 1
"""

import logging
import re
import sqlite3
import subprocess
from pathlib import Path

from cap.lib.config import GitHubConfig, load_config

logger = logging.getLogger("cap.repo_resolver")

_session_clone_count: int = 0

_SAFE_NAME = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,99}[a-zA-Z0-9])?$')


def _validate_name(name: str, label: str = "name") -> None:
    """Reject names that could cause path traversal or command injection."""
    if not name or not _SAFE_NAME.match(name):
        raise ValueError(f"Invalid {label}: {name!r} — must be alphanumeric with ._- only")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid {label}: {name!r} — path traversal not allowed")


def resolve_repo(
    repo_name: str,
    db: sqlite3.Connection | None = None,
    config: GitHubConfig | None = None,
    domain_hint: str | None = None,
    workspace: str | None = None,
) -> dict:
    """Resolve a dependent repo: check locally, clone from GitHub if missing.

    Args:
        repo_name:    Name of the repo (e.g., "alerting", "fleet-connector")
        db:           Optional SQLite connection for post-clone sync
        config:       GitHubConfig (loaded from platform config if not provided)
        domain_hint:  Optional subdirectory hint (e.g., "Observability-Alerting")
        workspace:    Optional clone destination directory. If provided, used as
                      the base path for cloning. Falls back to config.clone_base_path,
                      then to CWD.

    Returns:
        Dict with keys: status, path, cloned, message
    """
    global _session_clone_count

    _validate_name(repo_name, "repo_name")
    if domain_hint:
        _validate_name(domain_hint, "domain_hint")

    if config is None:
        config = load_config().github

    if not config.org:
        return {
            "status": "error",
            "path": None,
            "cloned": False,
            "message": "GitHub org not configured. Set [github].org in config.toml",
        }

    # Determine clone base: workspace param > config.clone_base_path > CWD
    if workspace:
        clone_base = Path(workspace)
    elif config.clone_base_path:
        clone_base = Path(config.clone_base_path)
    else:
        clone_base = Path.cwd()

    local_path = _find_local_repo(repo_name, clone_base, domain_hint)
    if local_path:
        return {
            "status": "found_locally",
            "path": str(local_path),
            "cloned": False,
            "message": f"Repo already exists at {local_path}",
        }

    if not config.auto_clone_on_missing_dep:
        return {
            "status": "not_found",
            "path": None,
            "cloned": False,
            "message": f"Repo '{repo_name}' not found locally. Auto-clone disabled.",
        }

    if _session_clone_count >= config.max_auto_clones_per_session:
        return {
            "status": "limit_reached",
            "path": None,
            "cloned": False,
            "message": f"Session clone limit ({config.max_auto_clones_per_session}) reached.",
        }

    if not _repo_exists_on_github(repo_name, config):
        return {
            "status": "not_found_remote",
            "path": None,
            "cloned": False,
            "message": f"Repo '{config.org}/{repo_name}' not found on GitHub.",
        }

    target_dir = _determine_clone_target(repo_name, clone_base, domain_hint)
    result = _clone_repo(repo_name, target_dir, config)

    if result["status"] == "cloned":
        _session_clone_count += 1
        if db is not None:
            _trigger_sync(db, str(target_dir))

    return result


def resolve_multiple(
    repo_names: list[str],
    db: sqlite3.Connection | None = None,
    config: GitHubConfig | None = None,
) -> list[dict]:
    """Resolve multiple repos. Stops at session limit."""
    results = []
    for name in repo_names:
        result = resolve_repo(name, db=db, config=config)
        results.append(result)
        if result["status"] == "limit_reached":
            break
    return results


def find_unresolved_dependencies(
    db: sqlite3.Connection,
    workspace: str | None = None,
) -> list[dict]:
    """Query knowledge graph for depends_on edges pointing to repos not indexed locally.

    Returns list of {repo_name, depended_on_by, reason} dicts.
    """
    base_sql = """
        SELECT DISTINCT
            tgt.entity_name AS dep_name,
            src.entity_name AS source_repo,
            e.metadata
        FROM knowledge_graph_edges e
        JOIN knowledge_graph_nodes src ON src.id = e.source_id
        JOIN knowledge_graph_nodes tgt ON tgt.id = e.target_id
        WHERE e.predicate = ?
          AND tgt.entity_type IN ('repo', 'chart', 'terraform_module')
    """
    if workspace:
        rows = db.execute(
            base_sql + " AND e.workspace = ?",
            ("depends_on", workspace),
        ).fetchall()
    else:
        rows = db.execute(base_sql, ("depends_on",)).fetchall()

    config = load_config().github
    clone_base = Path(config.clone_base_path) if config.clone_base_path else None
    if clone_base is None:
        return []

    unresolved = []
    seen = set()
    for dep_name, source_repo, meta_json in rows:
        if dep_name in seen:
            continue
        local = _find_local_repo(dep_name, clone_base)
        if local is None:
            seen.add(dep_name)
            reason = ""
            if meta_json:
                try:
                    import json
                    reason = json.loads(meta_json).get("reason", "")
                except Exception:
                    pass
            unresolved.append({
                "repo_name": dep_name,
                "depended_on_by": source_repo,
                "reason": reason,
            })

    return unresolved


def reset_session_counter() -> None:
    """Reset the per-session clone counter (called at session start)."""
    global _session_clone_count
    _session_clone_count = 0


def _find_local_repo(
    repo_name: str,
    clone_base: Path,
    domain_hint: str | None = None,
) -> Path | None:
    """Search for repo locally. Checks direct match and domain subdirectories."""
    if not clone_base.exists():
        return None

    direct = clone_base / repo_name
    if direct.is_dir() and (direct / ".git").exists():
        return direct

    if domain_hint:
        in_domain = clone_base / domain_hint / repo_name
        if in_domain.is_dir() and (in_domain / ".git").exists():
            return in_domain

    for domain_dir in clone_base.iterdir():
        if not domain_dir.is_dir() or domain_dir.is_symlink() or domain_dir.name.startswith("."):
            continue
        candidate = domain_dir / repo_name
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate

    return None


def _repo_exists_on_github(repo_name: str, config: GitHubConfig) -> bool:
    """Check if repo exists on GitHub using gh CLI (fast, no clone needed)."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", f"{config.org}/{repo_name}", "--json", "name"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        if config.use_ssh:
            try:
                result = subprocess.run(
                    ["git", "ls-remote", "--exit-code", "--heads",
                     f"git@github.com:{config.org}/{repo_name}.git", config.default_branch],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return False
        return False


def _determine_clone_target(
    repo_name: str,
    clone_base: Path,
    domain_hint: str | None = None,
) -> Path:
    """Determine where to clone. Uses domain_hint if provided, else root."""
    if domain_hint:
        target = clone_base / domain_hint / repo_name
    else:
        target = clone_base / repo_name
    return target


def _clone_repo(repo_name: str, target_dir: Path, config: GitHubConfig) -> dict:
    """Execute git clone."""
    if config.use_ssh:
        url = f"git@github.com:{config.org}/{repo_name}.git"
    else:
        url = f"https://github.com/{config.org}/{repo_name}.git"

    target_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone"]
    if config.clone_depth > 0:
        cmd.extend(["--depth", str(config.clone_depth)])
    cmd.extend(["--branch", config.default_branch, url, str(target_dir)])

    logger.info("Cloning %s/%s → %s", config.org, repo_name, target_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return {
                "status": "cloned",
                "path": str(target_dir),
                "cloned": True,
                "message": f"Cloned {config.org}/{repo_name} to {target_dir}",
            }
        else:
            stderr = result.stderr.strip()
            if "not found" in stderr.lower() or "does not exist" in stderr.lower():
                return {
                    "status": "not_found_remote",
                    "path": None,
                    "cloned": False,
                    "message": f"Repo not found: {config.org}/{repo_name}",
                }
            return {
                "status": "clone_failed",
                "path": None,
                "cloned": False,
                "message": f"Clone failed: {stderr[:200]}",
            }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "path": None,
            "cloned": False,
            "message": f"Clone timed out (120s) for {config.org}/{repo_name}",
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "path": None,
            "cloned": False,
            "message": "git not found in PATH",
        }


def _trigger_sync(db: sqlite3.Connection, workspace: str) -> None:
    """Trigger incremental knowledge sync for the newly cloned repo."""
    try:
        from cap.lib.sync_engine import sync_workspace
        stats = sync_workspace(db, workspace, full=True)
        logger.info(
            "Post-clone sync for %s: indexed=%d edges=%d",
            workspace, stats.files_indexed, stats.graph_edges_created,
        )
    except Exception as exc:
        logger.error("Post-clone sync failed for %s: %s", workspace, exc)

"""
CAP Auto-Sync — Git Operations.

Low-level git subprocess wrappers used by SyncEngine.
All operations are non-destructive reads except merge_ff_only (which only
fast-forwards, preserving working tree safety).

Design ref: CAP System Design Section 7 + Section 15 Patch 1.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("cap.sync.git_ops")

SUBPROCESS_TIMEOUT = 30  # seconds


@dataclass
class GitResult:
    """Result of a git operation."""

    success: bool
    output: str = ""
    error: str = ""


def _run_git(args: list[str], cwd: str, timeout: int = SUBPROCESS_TIMEOUT) -> GitResult:
    """Run a git command and return structured result."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return GitResult(
            success=result.returncode == 0,
            output=result.stdout.strip(),
            error=result.stderr.strip(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("git command timed out: %s (cwd=%s)", " ".join(cmd), cwd)
        return GitResult(success=False, error="timeout")
    except FileNotFoundError:
        logger.error("git binary not found")
        return GitResult(success=False, error="git not found")
    except OSError as e:
        logger.error("git command OS error: %s", e)
        return GitResult(success=False, error=str(e))


def fetch_all(workspace: str) -> GitResult:
    """
    Run git fetch --all --prune in the workspace.

    Fetches all remotes and prunes stale remote-tracking branches.
    This updates remote refs but does NOT modify the working tree.

    Args:
        workspace: Absolute path to the git repository root.

    Returns:
        GitResult with success/error status.
    """
    if not _is_git_dir(workspace):
        return GitResult(success=False, error="not a git repository")
    return _run_git(["fetch", "--all", "--prune"], cwd=workspace, timeout=30)


def merge_ff_only(workspace: str, branch: str = "main") -> GitResult:
    """
    Fast-forward merge from origin/<branch> if the working tree is clean.

    Only performs the merge if:
    1. Working tree has no uncommitted changes (clean)
    2. The merge can be done as a fast-forward (no divergence)

    This is safe: it never creates merge commits or modifies uncommitted work.

    Args:
        workspace: Absolute path to the git repository root.
        branch: Branch name to merge from origin. Defaults to 'main'.

    Returns:
        GitResult. success=True if merge completed, False if skipped or failed.
    """
    if not _is_git_dir(workspace):
        return GitResult(success=False, error="not a git repository")

    # Safety check: only merge if working tree is clean
    if not is_clean_tree(workspace):
        return GitResult(success=False, error="working tree is dirty, skipping ff-merge")

    remote_ref = f"origin/{branch}"
    return _run_git(["merge", "--ff-only", remote_ref], cwd=workspace, timeout=30)


def get_commits_behind(workspace: str, branch: str = "main") -> int:
    """
    Get number of commits the local branch is behind its upstream.

    Uses git rev-list --count HEAD..origin/<branch> to determine how many
    commits exist on the remote that are not in the local branch.

    Args:
        workspace: Absolute path to the git repository root.
        branch: Branch name to compare against. Defaults to 'main'.

    Returns:
        Number of commits behind. Returns 0 on any error.
    """
    if not _is_git_dir(workspace):
        return 0

    # First try the configured upstream
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=workspace,
        timeout=5,
    )
    if result.success:
        upstream = result.output
    else:
        # Fall back to origin/<branch>
        upstream = f"origin/{branch}"

    result = _run_git(
        ["rev-list", "--count", f"HEAD..{upstream}"],
        cwd=workspace,
        timeout=5,
    )
    if result.success:
        try:
            return int(result.output)
        except ValueError:
            return 0
    return 0


def get_changed_files_since(workspace: str, since_ref: str) -> list[str]:
    """
    Get list of files changed between since_ref and HEAD.

    Uses git diff --name-only to find files that differ. Returns absolute paths.

    Args:
        workspace: Absolute path to the git repository root.
        since_ref: Git ref to compare against (e.g., 'ORIG_HEAD', 'HEAD~5',
                   'origin/main', a commit SHA).

    Returns:
        List of absolute file paths that changed. Empty list on error.
    """
    if not _is_git_dir(workspace):
        return []

    result = _run_git(
        ["diff", "--name-only", f"{since_ref}..HEAD"],
        cwd=workspace,
        timeout=10,
    )
    if not result.success or not result.output:
        return []

    return [
        os.path.join(workspace, f)
        for f in result.output.split("\n")
        if f.strip()
    ]


def is_clean_tree(workspace: str) -> bool:
    """
    Check if the working tree has no uncommitted changes.

    Uses git status --porcelain which outputs nothing for a clean tree.

    Args:
        workspace: Absolute path to the git repository root.

    Returns:
        True if working tree is clean (no staged, unstaged, or untracked changes).
        False if dirty or on error.
    """
    if not _is_git_dir(workspace):
        return False

    result = _run_git(["status", "--porcelain"], cwd=workspace, timeout=10)
    if not result.success:
        return False
    return len(result.output) == 0


def get_upstream_ref(workspace: str) -> Optional[str]:
    """
    Get the upstream tracking ref for the current branch.

    Args:
        workspace: Absolute path to the git repository root.

    Returns:
        Upstream ref string (e.g., 'origin/main') or None if not configured.
    """
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=workspace,
        timeout=5,
    )
    if result.success and result.output:
        return result.output
    return None


def get_current_head(workspace: str) -> Optional[str]:
    """
    Get the current HEAD commit SHA.

    Args:
        workspace: Absolute path to the git repository root.

    Returns:
        Full commit SHA string or None on error.
    """
    result = _run_git(["rev-parse", "HEAD"], cwd=workspace, timeout=5)
    if result.success:
        return result.output
    return None


def _is_git_dir(workspace: str) -> bool:
    """Check if workspace is a git repository."""
    return os.path.isdir(os.path.join(workspace, ".git"))

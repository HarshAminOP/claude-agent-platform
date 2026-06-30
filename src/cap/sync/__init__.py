"""
CAP Auto-Sync Engine — Keeps code index fresh with workspace changes.

Provides:
- SyncEngine: Orchestrates incremental re-indexing based on git state changes,
  sync triggers from posttool hooks, and periodic staleness checks.
- git_ops: Low-level git operations (fetch, merge, diff, status).

Triggers (from CAP System Design Section 7):
- Session start: git fetch --all, detect behind remote, warn, re-index changed files
- Post git pull/merge: re-index files changed in the pull
- Staleness timer (>5 min): background hash check via asyncio task
- File write (Edit/Write tool): single-file re-extract (handled by posttool hook)
- Manual cap sync: full re-index via CLI
"""

from cap.sync.engine import SyncEngine
from cap.sync.git_ops import (
    fetch_all,
    get_changed_files_since,
    get_commits_behind,
    is_clean_tree,
    merge_ff_only,
)

__all__ = [
    "SyncEngine",
    "fetch_all",
    "get_changed_files_since",
    "get_commits_behind",
    "is_clean_tree",
    "merge_ff_only",
]

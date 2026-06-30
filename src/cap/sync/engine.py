"""
CAP Auto-Sync Engine — Detects stale indexes and triggers re-extraction.

Implements Section 7 (Auto-Sync Specification) with fixes from Section 15 Patch 1:
- background_loop polls sync_triggers table (bridging posttool.py -> SyncEngine)
- on_git_fetch compares local vs upstream refs (not dead ORIG_HEAD logic)
- session_start_sync fetches + ff-merges (clean tree) or indexes from blobs (dirty)
- _consume_sync_triggers reads/clears the table that posttool.py writes to

Design invariants:
- Never modifies working tree if it is dirty (safety first)
- All git operations have timeouts (no indefinite hangs)
- Gracefully degrades: errors are logged but never crash the MCP server
"""

import asyncio
import logging
import os
import subprocess
import time
from sqlite3 import Connection
from typing import Optional

from cap.sync.git_ops import (
    fetch_all,
    get_changed_files_since,
    get_commits_behind,
    get_current_head,
    get_upstream_ref,
    is_clean_tree,
    merge_ff_only,
)

logger = logging.getLogger("cap.sync.engine")

STALENESS_TTL = 300  # 5 minutes — file is stale if mtime > last_indexed_at + this
HASH_CHECK_INTERVAL = 300  # 5 minutes — periodic full check interval
TRIGGER_POLL_INTERVAL = 30  # 30 seconds — how often to poll sync_triggers


class SyncEngine:
    """
    Orchestrates incremental re-indexing of workspace code.

    Bridges posttool.py sync triggers with the code intelligence indexer.
    Runs as a background task in the MCP server's asyncio loop.
    """

    def __init__(self, workspace: str, db: Connection):
        """
        Initialize SyncEngine.

        Args:
            workspace: Absolute path to the git workspace root.
            db: SQLite connection (from cap.db.get_db) with WAL mode.
        """
        self.workspace = os.path.abspath(workspace)
        self.db = db
        self.last_sync: float = 0.0
        self._last_known_head: Optional[str] = None

    # ─── Public API ───────────────────────────────────────────────────────────

    def on_session_start(self, workspace: Optional[str] = None) -> dict:
        """
        Called at session start. Fetches remote, detects divergence, re-indexes.

        Strategy (Section 15 Patch 1):
        1. git fetch --all --prune
        2. Detect if local is behind remote
        3. If clean tree: ff-merge and index changed files
        4. If dirty tree: index from remote ref blobs without checkout
        5. Warn caller if behind remote

        Args:
            workspace: Override workspace path. Defaults to self.workspace.

        Returns:
            Dict with keys: fetched, behind_count, files_reindexed, warning
        """
        ws = workspace or self.workspace
        result = {"fetched": False, "behind_count": 0, "files_reindexed": 0, "warning": None}

        if not self._is_git_repo(ws):
            return result

        # Step 1: fetch
        fetch_result = fetch_all(ws)
        result["fetched"] = fetch_result.success
        if not fetch_result.success:
            logger.warning("git fetch failed: %s", fetch_result.error)

        # Step 2: detect behind
        behind, count = self._detect_behind_remote(ws)
        result["behind_count"] = count

        if count > 0:
            result["warning"] = (
                f"Local branch is {count} commit(s) behind remote. "
                "Consider pulling to stay current."
            )
            logger.info("Workspace %s is %d commits behind remote", ws, count)

        # Step 3/4: update working tree or index from blobs
        if count > 0:
            reindexed = self._sync_with_remote(ws)
            result["files_reindexed"] = reindexed

        # Record last known HEAD
        self._last_known_head = get_current_head(ws)
        self.last_sync = time.time()
        return result

    def on_git_pull(self, workspace: Optional[str] = None) -> int:
        """
        Called after a git pull completes. Re-indexes files changed in the pull.

        Uses ORIG_HEAD (set by pull/merge/rebase) to find what changed.

        Args:
            workspace: Override workspace path. Defaults to self.workspace.

        Returns:
            Number of files re-indexed.
        """
        ws = workspace or self.workspace
        if not self._is_git_repo(ws):
            return 0

        # ORIG_HEAD is set by git pull/merge/rebase
        changed_files = get_changed_files_since(ws, "ORIG_HEAD")
        if not changed_files:
            return 0

        self._reindex_files(changed_files, ws)
        self.last_sync = time.time()
        self._last_known_head = get_current_head(ws)
        return len(changed_files)

    def check_staleness(self, file_path: str) -> bool:
        """
        Check if a file's index is stale.

        A file is considered stale if its mtime is more than STALENESS_TTL
        seconds newer than its last_indexed_at timestamp in code_files.

        Args:
            file_path: Absolute path to the file to check.

        Returns:
            True if file is stale (needs re-indexing), False otherwise.
        """
        try:
            file_mtime = os.path.getmtime(file_path)
        except OSError:
            return False  # File doesn't exist, not stale (maybe deleted)

        row = self.db.execute(
            "SELECT extracted_at FROM code_files WHERE path = ?",
            (file_path,),
        ).fetchone()

        if row is None:
            # Not indexed at all — definitely stale
            return True

        last_indexed_at = row[0]
        return file_mtime > (last_indexed_at + STALENESS_TTL)

    def incremental_sync(self, workspace: Optional[str] = None) -> int:
        """
        Re-index only files that have changed since the last sync.

        Combines two detection methods:
        1. git diff --name-only HEAD (uncommitted changes)
        2. file mtime vs last extracted_at (catches external edits)

        Args:
            workspace: Override workspace path. Defaults to self.workspace.

        Returns:
            Number of files re-indexed.
        """
        ws = workspace or self.workspace
        changed = self._get_changed_files(ws)
        if not changed:
            return 0

        self._reindex_files(changed, ws)
        self.last_sync = time.time()
        return len(changed)

    async def background_loop(self, workspace: Optional[str] = None, interval: int = HASH_CHECK_INTERVAL) -> None:
        """
        Periodic background sync loop. Called by MCP server on timer.

        Runs indefinitely:
        - Every 30s: polls sync_triggers table for pending entries from posttool
        - Every <interval>s: runs incremental staleness check

        Section 15 Patch 1 fix: Now actually reads sync_triggers that posttool.py
        writes to, bridging the two components.

        Args:
            workspace: Override workspace path. Defaults to self.workspace.
            interval: Seconds between full staleness checks. Defaults to 300.
        """
        ws = workspace or self.workspace
        while True:
            try:
                # CHECK 1: Poll sync_triggers table (posttool.py -> SyncEngine bridge)
                pending_triggers = self._consume_sync_triggers()
                if pending_triggers:
                    logger.info(
                        "Processing %d sync triggers from posttool hook",
                        len(pending_triggers),
                    )
                    for trigger in pending_triggers:
                        await self._handle_trigger(trigger, ws)

                # CHECK 2: Periodic staleness (existing behavior)
                elapsed = time.time() - self.last_sync
                if elapsed >= interval:
                    count = self.incremental_sync(ws)
                    if count > 0:
                        logger.info("Periodic sync re-indexed %d files", count)

            except Exception as e:
                logger.error("Error in background sync loop: %s", e)

            await asyncio.sleep(TRIGGER_POLL_INTERVAL)

    # ─── Internal Methods ─────────────────────────────────────────────────────

    def _consume_sync_triggers(self) -> list[dict]:
        """
        Read and clear pending sync_triggers from DB.

        Bridges posttool.py (which INSERTs triggers) with the SyncEngine
        (which processes them). Uses DELETE ... RETURNING for atomic consume.

        Returns:
            List of trigger dicts with keys: id, type, detail, ts
        """
        try:
            rows = self.db.execute(
                """DELETE FROM sync_triggers
                   WHERE id IN (SELECT id FROM sync_triggers ORDER BY timestamp LIMIT 50)
                   RETURNING id, trigger_type, detail, timestamp"""
            ).fetchall()
            self.db.commit()
            return [
                {"id": r[0], "type": r[1], "detail": r[2], "ts": r[3]}
                for r in rows
            ]
        except Exception as e:
            logger.debug("Could not consume sync_triggers: %s", e)
            return []

    def _get_changed_files(self, workspace: str) -> list[str]:
        """
        Get all files that need re-indexing in the workspace.

        Combines:
        1. git diff --name-only HEAD (uncommitted working tree changes)
        2. mtime comparison against last extracted_at in code_files table

        Args:
            workspace: Absolute path to workspace root.

        Returns:
            Deduplicated list of absolute file paths needing re-index.
        """
        changed: list[str] = []

        # Method 1: git diff for uncommitted changes
        if self._is_git_repo(workspace):
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    changed.extend(
                        os.path.join(workspace, f.strip())
                        for f in result.stdout.strip().split("\n")
                        if f.strip()
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        # Method 2: mtime vs last extracted_at
        indexed_files = self.db.execute(
            "SELECT path, extracted_at FROM code_files WHERE workspace = ?",
            (workspace,),
        ).fetchall()

        for row in indexed_files:
            path, extracted_at = row[0], row[1]
            try:
                if os.path.getmtime(path) > extracted_at:
                    if path not in changed:
                        changed.append(path)
            except OSError:
                pass  # file deleted — will be handled during re-index

        return changed

    def _detect_behind_remote(self, workspace: str) -> tuple[bool, int]:
        """
        Detect if local branch is behind its remote tracking branch.

        Args:
            workspace: Absolute path to workspace root.

        Returns:
            Tuple of (is_behind: bool, commits_behind: int)
        """
        count = get_commits_behind(workspace)
        return (count > 0, count)

    def _sync_with_remote(self, workspace: str) -> int:
        """
        Sync workspace with remote: ff-merge if clean, or index from blobs if dirty.

        Section 15 Patch 1 strategy:
        - Clean tree: fast-forward merge, then index files changed in the merge
        - Dirty tree: read changed files from remote ref using git show (no checkout)

        Args:
            workspace: Absolute path to workspace root.

        Returns:
            Number of files re-indexed.
        """
        upstream = get_upstream_ref(workspace)
        if not upstream:
            return 0

        if is_clean_tree(workspace):
            # STRATEGY A: Fast-forward merge (updates working tree safely)
            old_head = get_current_head(workspace)
            merge_result = merge_ff_only(workspace)
            if merge_result.success and old_head:
                changed = get_changed_files_since(workspace, old_head)
                if changed:
                    self._reindex_files(changed, workspace)
                    return len(changed)
        else:
            # STRATEGY B: Index from remote ref blobs without checkout
            logger.info(
                "Working tree is dirty, indexing from remote ref %s without merge",
                upstream,
            )
            return self._index_from_remote_ref(workspace, upstream)

        return 0

    def _index_from_remote_ref(self, workspace: str, upstream: str) -> int:
        """
        Read file contents directly from a git ref (without checkout).

        Uses `git diff --name-only HEAD..<upstream>` to find changed paths,
        then `git show <ref>:<path>` to get blob content for indexing.
        Only indexes files in SUPPORTED_LANGUAGES.

        Args:
            workspace: Absolute path to workspace root.
            upstream: Remote ref (e.g., 'origin/main').

        Returns:
            Number of files indexed from remote ref.
        """
        from cap.code_intel.extractor import SUPPORTED_LANGUAGES, detect_language

        # Find files that differ between local and upstream
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"HEAD..{upstream}"],
                capture_output=True,
                text=True,
                cwd=workspace,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return 0

            changed_paths = [
                f.strip()
                for f in result.stdout.strip().split("\n")
                if f.strip()
            ]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return 0

        count = 0
        for rel_path in changed_paths:
            language = detect_language(rel_path)
            if language is None or language not in SUPPORTED_LANGUAGES:
                continue
            try:
                blob_result = subprocess.run(
                    ["git", "show", f"{upstream}:{rel_path}"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    timeout=5,
                )
                if blob_result.returncode == 0 and blob_result.stdout:
                    abs_path = os.path.join(workspace, rel_path)
                    self._index_content(abs_path, blob_result.stdout, language, workspace)
                    count += 1
            except subprocess.TimeoutExpired:
                continue

        return count

    def _index_content(self, file_path: str, content: str, language: str, workspace: str) -> None:
        """
        Index file content string into the database (for remote ref blobs).

        This is the equivalent of extract_file + store but from in-memory content
        rather than reading from disk.

        Args:
            file_path: Absolute path the file would have on disk.
            content: File content string.
            language: Detected language.
            workspace: Workspace path for DB storage.
        """
        import hashlib

        from cap.code_intel.extractor import extract_file

        # Store content hash in code_files so staleness detection works
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        now = time.time()

        try:
            # Use extract_file if the file exists on disk, otherwise store metadata
            if os.path.isfile(file_path):
                file_index = extract_file(file_path, language)
                if file_index:
                    from cap.code_intel.indexer import _store_file_index
                    _store_file_index(self.db, file_index, workspace)
                    self.db.commit()
            else:
                # File doesn't exist locally — store a placeholder with remote hash
                self.db.execute(
                    """INSERT OR REPLACE INTO code_files (path, workspace, language, hash, extracted_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (file_path, workspace, language, content_hash, now),
                )
                self.db.commit()
        except Exception as e:
            logger.debug("Error indexing content for %s: %s", file_path, e)

    def _reindex_files(self, file_paths: list[str], workspace: str) -> None:
        """
        Re-index a list of files using the code intelligence indexer.

        Args:
            file_paths: List of absolute file paths to re-index.
            workspace: Workspace path for context.
        """
        from cap.code_intel.indexer import index_file

        for file_path in file_paths:
            try:
                index_file(file_path, self.db)
            except Exception as e:
                logger.debug("Error re-indexing %s: %s", file_path, e)

    async def _handle_trigger(self, trigger: dict, workspace: str) -> None:
        """
        Handle a single sync trigger from posttool.py.

        Trigger types:
        - git_post_pull: re-index files changed by the pull
        - file_write: single file re-extract (already handled by posttool, but
          we re-check here for completeness)

        Args:
            trigger: Dict with keys: id, type, detail, ts
            workspace: Workspace path.
        """
        trigger_type = trigger.get("type", "")

        if trigger_type == "git_post_pull":
            # A git pull/merge/fetch was detected — sync with remote
            self.on_session_start(workspace)
        else:
            # Generic trigger — do incremental sync
            self.incremental_sync(workspace)

    def _is_git_repo(self, workspace: str) -> bool:
        """Check if workspace is a git repository."""
        return os.path.isdir(os.path.join(workspace, ".git"))

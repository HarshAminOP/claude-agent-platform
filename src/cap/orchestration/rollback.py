"""
Rollback Manager — in-memory file backup and restore for orchestration.

Tracks file contents before modifications so that a failed workflow
can revert all changes atomically. Backups are held in memory (dict)
during the workflow lifetime and discarded on commit.

Reference: CAP System Design Section 15 (rollback) and Section 16C.
"""

import os
from typing import Optional


class RollbackManager:
    """
    In-memory file backup manager for orchestration workflows.

    Usage:
        rm = RollbackManager()
        rm.track_file("/path/to/file.py")   # Backup original
        # ... workflow modifies the file ...
        rm.rollback()   # Restore all tracked files to original state

    On success:
        rm.commit()     # Discard backups, changes are permanent
    """

    def __init__(self):
        # {file_path: (original_content: Optional[str], existed: bool)}
        self._backups: dict[str, tuple[Optional[str], bool]] = {}

    def track_file(self, file_path: str) -> None:
        """
        Backup the current content of a file before modification.

        If the file does not exist, records that fact so rollback can
        remove any newly created file.

        Args:
            file_path: Absolute path to the file to track.

        Note:
            If the file is already tracked, this is a no-op (preserves
            the original backup, not an intermediate state).
        """
        if file_path in self._backups:
            return  # Already tracking — keep original backup

        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            self._backups[file_path] = (content, True)
        else:
            self._backups[file_path] = (None, False)

    def commit(self) -> None:
        """
        Discard all backups (workflow succeeded, changes are permanent).

        After commit(), rollback() becomes a no-op.
        """
        self._backups.clear()

    def rollback(self) -> list[str]:
        """
        Restore all tracked files to their original state.

        - Files that existed before: restore original content.
        - Files that did not exist before: delete them.

        Returns:
            List of file paths that were restored/removed.
        """
        restored = []

        for file_path, (original_content, existed) in self._backups.items():
            try:
                if existed:
                    # Restore original content
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(original_content)
                    restored.append(file_path)
                else:
                    # File was created during workflow — remove it
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        restored.append(file_path)
            except OSError:
                # Best-effort rollback — continue with remaining files
                pass

        self._backups.clear()
        return restored

    @property
    def tracked_files(self) -> list[str]:
        """List of currently tracked file paths."""
        return list(self._backups.keys())

    @property
    def has_backups(self) -> bool:
        """Whether any files are currently tracked."""
        return len(self._backups) > 0

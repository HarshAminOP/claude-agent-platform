"""
Inter-agent artifact sharing via scratchpad.

Provides a shared temp directory per workflow where agents can write
artifacts by key and other agents can read them. Supports DB-backed
metadata tracking and cleanup on workflow completion.

Storage: ~/.cap/scratchpad/<workflow_id>/<key>
"""

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


SCRATCHPAD_ROOT = os.path.expanduser("~/.cap/scratchpad")


@dataclass
class ArtifactMetadata:
    key: str
    agent_type: str
    created_at: float
    size_bytes: int
    path: str


class Scratchpad:
    """
    Per-workflow artifact storage for multi-agent orchestration.

    Agents write artifacts by key, other agents read them by key.
    All artifacts are cleaned up when the workflow completes or on rollback.
    """

    def __init__(self, workflow_id: str, db: Optional[sqlite3.Connection] = None):
        self.workflow_id = workflow_id
        self.db = db
        self.root = os.path.join(SCRATCHPAD_ROOT, workflow_id)
        os.makedirs(self.root, exist_ok=True)
        self._metadata: dict[str, ArtifactMetadata] = {}

    def write(self, key: str, content: str, agent_type: str = "unknown") -> str:
        """
        Write an artifact to the scratchpad.

        Args:
            key: Artifact name/key (can include path separators for nesting).
            content: String content to write.
            agent_type: Which agent produced this artifact.

        Returns:
            Absolute path to the written file.

        Raises:
            ValueError: If the key would escape the scratchpad root (path traversal).
        """
        path = os.path.realpath(os.path.join(self.root, key))
        # Security: prevent path traversal outside scratchpad root
        if not path.startswith(os.path.realpath(self.root) + os.sep) and path != os.path.realpath(self.root):
            raise ValueError(
                f"Path traversal detected: key '{key}' resolves outside scratchpad root"
            )
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        metadata = ArtifactMetadata(
            key=key,
            agent_type=agent_type,
            created_at=time.time(),
            size_bytes=len(content.encode("utf-8")),
            path=path,
        )
        self._metadata[key] = metadata

        # Record in DB if available
        if self.db is not None:
            self._record_to_db(metadata)

        return path

    def read(self, key: str) -> str:
        """
        Read an artifact by key.

        Args:
            key: Artifact name/key.

        Returns:
            Content of the artifact.

        Raises:
            FileNotFoundError: If the artifact does not exist.
            ValueError: If the key would escape the scratchpad root (path traversal).
        """
        path = os.path.realpath(os.path.join(self.root, key))
        # Security: prevent path traversal outside scratchpad root
        if not path.startswith(os.path.realpath(self.root) + os.sep) and path != os.path.realpath(self.root):
            raise ValueError(
                f"Path traversal detected: key '{key}' resolves outside scratchpad root"
            )
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Scratchpad artifact '{key}' not found for workflow '{self.workflow_id}'"
            )

        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def list(self) -> list[dict]:
        """
        List all artifacts with metadata.

        Returns:
            List of dicts with key, agent_type, created_at, size_bytes.
        """
        results = []

        for root_dir, _, files in os.walk(self.root):
            for filename in files:
                full_path = os.path.join(root_dir, filename)
                rel_key = os.path.relpath(full_path, self.root)

                # Use cached metadata if available
                if rel_key in self._metadata:
                    meta = self._metadata[rel_key]
                    results.append({
                        "key": meta.key,
                        "agent_type": meta.agent_type,
                        "created_at": meta.created_at,
                        "size_bytes": meta.size_bytes,
                    })
                else:
                    # Fallback: read from filesystem
                    stat = os.stat(full_path)
                    results.append({
                        "key": rel_key,
                        "agent_type": "unknown",
                        "created_at": stat.st_mtime,
                        "size_bytes": stat.st_size,
                    })

        return results

    def cleanup(self, workflow_id: Optional[str] = None) -> None:
        """
        Remove all artifacts for this workflow (or specified workflow_id).

        Removes temp files and clears DB references.

        Args:
            workflow_id: Override workflow ID to clean up. Defaults to self.workflow_id.
        """
        target_id = workflow_id or self.workflow_id
        target_root = os.path.join(SCRATCHPAD_ROOT, target_id)

        if os.path.exists(target_root):
            shutil.rmtree(target_root)

        self._metadata.clear()

        # Clean DB references if available
        if self.db is not None:
            self._cleanup_db(target_id)

    def _record_to_db(self, metadata: ArtifactMetadata) -> None:
        """Record artifact metadata in the runtime_state table."""
        state_key = f"scratchpad:{self.workflow_id}:{metadata.key}"
        payload = json.dumps({
            "workflow_id": self.workflow_id,
            "key": metadata.key,
            "agent_type": metadata.agent_type,
            "created_at": metadata.created_at,
            "size_bytes": metadata.size_bytes,
            "path": metadata.path,
        })
        try:
            self.db.execute(
                """INSERT OR REPLACE INTO runtime_state (key, value, updated_at)
                   VALUES (?, ?, ?)""",
                (state_key, payload, time.time()),
            )
            self.db.commit()
        except sqlite3.OperationalError:
            # Table may not exist in test contexts — silently skip
            pass

    def _cleanup_db(self, workflow_id: str) -> None:
        """Remove all DB references for a workflow."""
        try:
            self.db.execute(
                "DELETE FROM runtime_state WHERE key LIKE ?",
                (f"scratchpad:{workflow_id}:%",),
            )
            self.db.commit()
        except sqlite3.OperationalError:
            pass

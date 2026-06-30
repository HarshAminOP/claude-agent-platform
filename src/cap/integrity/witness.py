"""
CAP Witness Manifest — Cryptographic proof that files were reviewed.

Provides:
- WitnessManifest: stamp(), verify(), invalidate()

Uses SHA-256 hashing to create tamper-evident records of reviewed files.
HMAC signatures prevent DB-level tampering of witness stamps.
Stored in the witness_manifests table.
"""

import hashlib
import hmac
import os
import sqlite3
import time
from typing import List

# HMAC key file for witness integrity. Generated on first use.
_HMAC_KEY_PATH = os.path.expanduser("~/.cap/witness.key")
_MAX_FILE_SIZE = 100_000_000  # 100 MB - refuse to hash files larger than this
_MAX_FILES_PER_CALL = 10_000  # Prevent DoS from unbounded file lists


def _get_hmac_key() -> bytes:
    """
    Load or generate the HMAC key for witness stamp integrity.
    Key is stored in ~/.cap/witness.key with owner-only permissions.
    """
    if os.path.exists(_HMAC_KEY_PATH):
        with open(_HMAC_KEY_PATH, "rb") as f:
            key = f.read()
            if len(key) >= 32:
                return key
    # Generate a new 32-byte key
    key = os.urandom(32)
    key_dir = os.path.dirname(_HMAC_KEY_PATH)
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    # Write atomically via temp file to avoid partial reads
    tmp_path = _HMAC_KEY_PATH + ".tmp"
    with open(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(key)
    os.replace(tmp_path, _HMAC_KEY_PATH)
    return key


def _compute_signature(content_hash: str, file_path: str, reviewer: str, workflow_id: str) -> str:
    """Compute HMAC-SHA256 signature over witness fields to prevent tampering."""
    key = _get_hmac_key()
    msg = f"{content_hash}|{file_path}|{reviewer}|{workflow_id}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _verify_signature(content_hash: str, file_path: str, reviewer: str, workflow_id: str, signature: str) -> bool:
    """Verify HMAC signature of a witness stamp."""
    expected = _compute_signature(content_hash, file_path, reviewer, workflow_id)
    return hmac.compare_digest(expected, signature)


class WitnessManifest:
    """Cryptographic witness that files were reviewed at a specific content state."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def stamp(self, file_paths: List[str], reviewer: str, workflow_id: str) -> dict:
        """
        Stamp files as reviewed. Records content hash and HMAC signature for each file.

        Args:
            file_paths: List of absolute file paths to stamp.
            reviewer: Identifier of the reviewing agent/user.
            workflow_id: Workflow that produced the review.

        Returns:
            Manifest dict with stamped files, hashes, and timestamp.
        """
        now = time.time()
        stamped = []
        skipped = []

        # Bound the number of files to prevent DoS
        if len(file_paths) > _MAX_FILES_PER_CALL:
            return {"error": f"Too many files ({len(file_paths)}), max is {_MAX_FILES_PER_CALL}"}

        for path in file_paths:
            # Validate path is absolute to prevent relative path confusion
            if not os.path.isabs(path):
                skipped.append({"path": path, "reason": "not_absolute_path"})
                continue

            if not os.path.isfile(path):
                skipped.append({"path": path, "reason": "file_not_found"})
                continue

            # Check file size to prevent DoS via enormous files
            try:
                file_size = os.path.getsize(path)
                if file_size > _MAX_FILE_SIZE:
                    skipped.append({"path": path, "reason": "file_too_large"})
                    continue
            except OSError:
                skipped.append({"path": path, "reason": "stat_failed"})
                continue

            # Resolve symlinks to get canonical path (prevent symlink attacks)
            real_path = os.path.realpath(path)

            content_hash = self._hash_file(real_path)
            signature = _compute_signature(content_hash, real_path, reviewer, workflow_id)
            try:
                self.db.execute(
                    """
                    INSERT OR REPLACE INTO witness_manifests
                    (file_path, content_hash, reviewer, workflow_id, signature, stamped_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (real_path, content_hash, reviewer, workflow_id, signature, now),
                )
                stamped.append({"path": real_path, "hash": content_hash, "signature": signature})
            except sqlite3.Error:
                skipped.append({"path": path, "reason": "db_error"})

        self.db.commit()

        return {
            "workflow_id": workflow_id,
            "reviewer": reviewer,
            "stamped_at": now,
            "stamped": stamped,
            "skipped": skipped,
            "total_stamped": len(stamped),
        }

    def verify(self, file_paths: List[str]) -> dict:
        """
        Verify files against their witness stamps.

        Returns dict with passed (hash matches), failed (hash changed),
        and unreviewed (no stamp exists) lists.
        """
        passed = []
        failed = []
        unreviewed = []

        # Bound the number of files to prevent DoS
        if len(file_paths) > _MAX_FILES_PER_CALL:
            return {"error": f"Too many files ({len(file_paths)}), max is {_MAX_FILES_PER_CALL}"}

        for path in file_paths:
            # Resolve to canonical path for consistent lookup
            real_path = os.path.realpath(path) if os.path.isabs(path) else path

            row = self.db.execute(
                """
                SELECT content_hash, reviewer, workflow_id, stamped_at, signature
                FROM witness_manifests
                WHERE file_path = ?
                ORDER BY stamped_at DESC LIMIT 1
                """,
                (real_path,),
            ).fetchone()

            if not row:
                unreviewed.append(real_path)
                continue

            stored_hash = row[0]
            reviewer = row[1]
            workflow_id = row[2]
            try:
                stored_signature = row[4] or ""
            except (IndexError, KeyError):
                stored_signature = ""

            # Verify HMAC signature to detect DB-level tampering
            if stored_signature:
                if not _verify_signature(stored_hash, real_path, reviewer, workflow_id, stored_signature):
                    failed.append({
                        "path": real_path,
                        "reason": "signature_tampered",
                        "expected_hash": stored_hash,
                    })
                    continue

            if not os.path.isfile(real_path):
                failed.append({
                    "path": real_path,
                    "reason": "file_missing",
                    "expected_hash": stored_hash,
                })
                continue

            # Check file size before hashing to prevent DoS
            try:
                if os.path.getsize(real_path) > _MAX_FILE_SIZE:
                    failed.append({
                        "path": real_path,
                        "reason": "file_too_large_to_verify",
                        "expected_hash": stored_hash,
                    })
                    continue
            except OSError:
                failed.append({
                    "path": real_path,
                    "reason": "stat_failed",
                    "expected_hash": stored_hash,
                })
                continue

            current_hash = self._hash_file(real_path)
            if current_hash == stored_hash:
                # Update verified_at timestamp
                self.db.execute(
                    """
                    UPDATE witness_manifests SET verified_at = ?
                    WHERE file_path = ? AND content_hash = ?
                    """,
                    (time.time(), real_path, stored_hash),
                )
                passed.append({
                    "path": real_path,
                    "hash": current_hash,
                    "reviewer": reviewer,
                    "workflow_id": workflow_id,
                    "stamped_at": row[3],
                })
            else:
                failed.append({
                    "path": real_path,
                    "reason": "hash_mismatch",
                    "expected_hash": stored_hash,
                    "actual_hash": current_hash,
                })

        self.db.commit()

        return {
            "passed": passed,
            "failed": failed,
            "unreviewed": unreviewed,
            "total_checked": len(file_paths),
        }

    def invalidate(self, file_path: str) -> None:
        """Remove all witness stamps for a file (e.g., after modification)."""
        # Resolve to canonical path for consistent lookup
        real_path = os.path.realpath(file_path) if os.path.isabs(file_path) else file_path
        self.db.execute(
            "DELETE FROM witness_manifests WHERE file_path = ?",
            (real_path,),
        )
        self.db.commit()

    @staticmethod
    def _hash_file(path: str) -> str:
        """Compute SHA-256 hash of file contents."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

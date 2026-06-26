"""Database maintenance for the CAP platform.

Handles WAL checkpointing, vacuum, data pruning, online backups, restore,
and database health checks (doctor).  Schema creation and migrations live in
db_init.py; this module only operates on databases that already exist.

Thread-safety note: each public method opens and closes its own connection so
that callers are free to use DBMaintenance from multiple threads without
external locking.
"""

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("cap.maintenance")

# ── Whitelist ─────────────────────────────────────────────────────────────────

ALLOWED_PRUNE_TABLES: frozenset[str] = frozenset({
    "concurrency_slots",
    "inbox",
    "maintenance_log",
    "embedding_queue",
    "checkpoints",
    "session_events",
    "api_calls",
    "workflow_events",
    "fleet_events",
})

# ── Retention rules ───────────────────────────────────────────────────────────
# Keys are database filenames; values map table → pruning parameters.
# condition_col/condition_val, when present, add an extra WHERE filter so that
# only rows matching the condition are deleted (useful for status-gated tables).

RETENTION_RULES: dict[str, dict[str, dict]] = {
    "platform.db": {
        "api_calls": {"ttl_days": 90, "condition_col": None},
        "workflow_events": {"ttl_days": 90, "condition_col": None},
    },
    "knowledge.db": {
        "embedding_queue": {
            "ttl_days": 7,
            "condition_col": "status",
            "condition_val": "done",
        },
    },
    "sessions.db": {
        "checkpoints": {"ttl_days": 30, "condition_col": None},
        "session_events": {
            "ttl_days": 180,
            "condition_col": "event_type",
            "condition_val": "error",
        },
    },
    "fleet.db": {
        "fleet_events": {"ttl_days": 90, "condition_col": None},
    },
}

# Expected schema versions — must stay in sync with db_init._*_VERSION constants.
_EXPECTED_SCHEMA_VERSIONS: dict[str, int] = {
    "platform.db": 2,
    "knowledge.db": 1,
    "sessions.db": 1,
    "fleet.db": 1,
}

_WAL_WARN_BYTES: int = 100 * 1024 * 1024   # 100 MB
_BACKUP_KEEP: int = 5
_FILE_MODE_DB: int = 0o600


# ── Internal helpers ──────────────────────────────────────────────────────────

def _open(path: Path) -> sqlite3.Connection:
    """Open an existing database with standard CAP pragmas."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _wal_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + "-wal")


def _shm_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + "-shm")


# ── Main class ────────────────────────────────────────────────────────────────

class DBMaintenance:
    """Maintenance operations for all CAP SQLite databases.

    Parameters
    ----------
    data_dir:
        Directory that contains the .db files (and where the *backups/*
        sub-directory will be created).
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.backup_dir = self.data_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ── WAL checkpoint ────────────────────────────────────────────────────────

    def checkpoint(self, db_path: Path) -> dict:
        """Run a WAL checkpoint in RESTART mode.

        RESTART is preferred over FULL/PASSIVE because it resets WAL write
        position (keeping WAL file small) while still allowing concurrent
        readers to proceed.

        Returns
        -------
        dict with keys: db, mode, pages_written, pages_in_wal, ok, duration_ms
        """
        start = time.monotonic()
        result: dict = {"db": db_path.name, "mode": "RESTART"}
        try:
            with _open(db_path) as conn:
                row = conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
                # row = (busy, log, checkpointed)
                result["pages_in_wal"] = row[1] if row else -1
                result["pages_written"] = row[2] if row else -1
                result["ok"] = True
        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            logger.warning("checkpoint failed for %s: %s", db_path.name, exc)
        result["duration_ms"] = _elapsed_ms(start)
        self._log_maintenance(db_path, "checkpoint", "ok" if result["ok"] else "error", result)
        return result

    def auto_checkpoint(self, db_path: Path, threshold_mb: float = 50.0) -> bool:
        """Checkpoint only when the WAL file exceeds *threshold_mb*.

        Returns True if a checkpoint was actually run.
        """
        wal = _wal_path(db_path)
        if not wal.exists():
            return False
        wal_bytes = wal.stat().st_size
        if wal_bytes < threshold_mb * 1024 * 1024:
            return False
        logger.info(
            "auto_checkpoint: %s WAL is %.1f MB (threshold %.1f MB) — running",
            db_path.name,
            wal_bytes / (1024 * 1024),
            threshold_mb,
        )
        self.checkpoint(db_path)
        return True

    # ── Vacuum ────────────────────────────────────────────────────────────────

    def vacuum(self, db_path: Path) -> dict:
        """Full VACUUM with a pre-vacuum backup.

        VACUUM rewrites the entire database file; it cannot run inside a
        transaction and will fail if another connection holds an open
        write transaction.  A backup is taken first so the original can be
        restored if something goes wrong.

        Returns
        -------
        dict with keys: db, backup_path, size_before_bytes, size_after_bytes,
                         ok, duration_ms
        """
        start = time.monotonic()
        result: dict = {"db": db_path.name, "ok": False}
        try:
            result["size_before_bytes"] = db_path.stat().st_size
            backup_path = self.backup(db_path)
            result["backup_path"] = str(backup_path)

            # VACUUM cannot run inside an implicit transaction opened by the
            # context manager; use isolation_level=None (autocommit).
            conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("VACUUM")
            finally:
                conn.close()

            result["size_after_bytes"] = db_path.stat().st_size
            result["ok"] = True
        except Exception as exc:
            result["error"] = str(exc)
            logger.error("vacuum failed for %s: %s", db_path.name, exc)
        result["duration_ms"] = _elapsed_ms(start)
        self._log_maintenance(db_path, "vacuum", "ok" if result["ok"] else "error", result)
        return result

    # ── Prune ─────────────────────────────────────────────────────────────────

    def prune(self, db_name: str) -> dict:
        """Delete stale rows according to RETENTION_RULES.

        Security note: table names in the f-string below are validated against
        ALLOWED_PRUNE_TABLES (a frozenset constant) before use, making injection
        impossible.  condition_col values come exclusively from our own
        RETENTION_RULES dict, never from external input.

        Parameters
        ----------
        db_name:
            Filename of the database (e.g. "platform.db").

        Returns
        -------
        dict with keys: db, deleted (per-table counts), total_deleted, ok,
                         duration_ms
        """
        start = time.monotonic()
        result: dict = {"db": db_name, "deleted": {}, "total_deleted": 0, "ok": False}

        rules = RETENTION_RULES.get(db_name)
        if not rules:
            result["ok"] = True
            result["skipped"] = "no retention rules for this database"
            return result

        db_path = self.data_dir / db_name
        if not db_path.exists():
            result["error"] = f"database file not found: {db_path}"
            return result

        try:
            with _open(db_path) as conn:
                for table, rule in rules.items():
                    # Validate against whitelist — table name is safe to interpolate.
                    if table not in ALLOWED_PRUNE_TABLES:
                        raise ValueError(f"Table '{table}' not in allowed prune list")

                    cutoff = (
                        datetime.utcnow() - timedelta(days=rule["ttl_days"])
                    ).isoformat()

                    if rule.get("condition_col"):
                        # condition_col is from RETENTION_RULES, not user input — safe to interpolate.
                        conn.execute(
                            f"DELETE FROM {table} WHERE created_at < ? AND {rule['condition_col']} = ?",
                            (cutoff, rule["condition_val"]),
                        )
                    else:
                        conn.execute(
                            f"DELETE FROM {table} WHERE created_at < ?",
                            (cutoff,),
                        )

                    deleted = conn.execute("SELECT changes()").fetchone()[0]
                    result["deleted"][table] = deleted
                    result["total_deleted"] += deleted
                    logger.debug("prune %s.%s: removed %d rows", db_name, table, deleted)

                conn.commit()
            result["ok"] = True
        except Exception as exc:
            result["error"] = str(exc)
            logger.error("prune failed for %s: %s", db_name, exc)

        result["duration_ms"] = _elapsed_ms(start)
        db_path_obj = self.data_dir / db_name
        self._log_maintenance(db_path_obj, "prune", "ok" if result["ok"] else "error", result)
        return result

    # ── Backup ────────────────────────────────────────────────────────────────

    def backup(self, db_path: Path) -> Path:
        """Online backup using the SQLite backup API.

        The backup API is safe to run while the database is open and being
        written; it copies pages in small batches and handles concurrent
        writers automatically.

        Backup filename pattern: <stem>_<YYYYMMDD_HHMMSS>.db
        Permissions: 0600 (set explicitly after creation).
        Old backups beyond _BACKUP_KEEP are pruned.

        Returns the Path of the newly created backup file.
        """
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"{db_path.stem}_{ts}.db"

        src = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            dst = sqlite3.connect(str(backup_path), check_same_thread=False)
            try:
                src.backup(dst, pages=100)
            finally:
                dst.close()
        finally:
            src.close()

        os.chmod(backup_path, _FILE_MODE_DB)
        logger.info("backup created: %s", backup_path)

        self._prune_old_backups(db_path.stem)
        return backup_path

    def _prune_old_backups(self, stem: str) -> None:
        """Keep only the _BACKUP_KEEP most recent backups for a given db stem."""
        pattern = f"{stem}_*.db"
        existing = sorted(self.backup_dir.glob(pattern))
        to_remove = existing[: max(0, len(existing) - _BACKUP_KEEP)]
        for old in to_remove:
            try:
                old.unlink()
                logger.debug("pruned old backup: %s", old.name)
            except OSError as exc:
                logger.warning("could not remove old backup %s: %s", old.name, exc)

    # ── Restore ───────────────────────────────────────────────────────────────

    def restore_latest_backup(self, db_path: Path) -> bool:
        """Restore the most recent backup for *db_path*.

        The live database file is replaced atomically (rename-over).  WAL and
        SHM side-files are removed first so SQLite does not attempt to replay
        a stale WAL on top of the restored image.

        Returns True on success, False if no backup exists or on error.
        """
        pattern = f"{db_path.stem}_*.db"
        candidates = sorted(self.backup_dir.glob(pattern))
        if not candidates:
            logger.warning("no backups found for %s", db_path.name)
            return False

        latest = candidates[-1]
        tmp = db_path.with_suffix(".db.restoring")
        try:
            shutil.copy2(str(latest), str(tmp))
            os.chmod(tmp, _FILE_MODE_DB)

            # Remove WAL/SHM so SQLite starts clean from the restored snapshot.
            for side in (_wal_path(db_path), _shm_path(db_path)):
                if side.exists():
                    side.unlink()
                    logger.info("removed side-file before restore: %s", side.name)

            os.replace(str(tmp), str(db_path))
            logger.info("restored %s from %s", db_path.name, latest.name)
            return True
        except Exception as exc:
            logger.error("restore failed for %s: %s", db_path.name, exc)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False

    # ── Doctor ────────────────────────────────────────────────────────────────

    def doctor(self, db_path: Path, fix: bool = False) -> dict:
        """Health check with optional repair.

        Checks performed
        ----------------
        1. integrity_check PRAGMA — detects corruption.
        2. File permissions — warns if not 0600.
        3. WAL file size — warns if > 100 MB.
        4. Stale PID lockfile (*.lock) — warns if process is gone.
        5. Schema version — warns if user_version does not match expected.

        fix=False (default)
            Dry-run: report issues and what WOULD be done, but take no action.
        fix=True
            Attempt repair: restore from backup on corruption, fix permissions,
            run WAL checkpoint, remove stale lockfiles.

        Returns
        -------
        dict with keys:
            db, ok (bool), issues (list[str]), actions_taken (list[str]),
            would_do (list[str], populated only when fix=False), duration_ms
        """
        start = time.monotonic()
        result: dict = {
            "db": db_path.name,
            "ok": True,
            "issues": [],
            "actions_taken": [],
            "would_do": [],
        }

        if not db_path.exists():
            result["ok"] = False
            result["issues"].append("database file does not exist")
            result["duration_ms"] = _elapsed_ms(start)
            return result

        # ── Check 1: integrity ────────────────────────────────────────────────
        try:
            with _open(db_path) as conn:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                integrity_ok = len(rows) == 1 and rows[0][0] == "ok"
        except Exception as exc:
            integrity_ok = False
            rows = [(str(exc),)]

        if not integrity_ok:
            messages = [r[0] for r in rows]
            result["ok"] = False
            result["issues"].append(f"integrity_check failed: {messages}")
            if fix:
                success = self.restore_latest_backup(db_path)
                if success:
                    result["actions_taken"].append("restored from latest backup due to integrity failure")
                else:
                    result["actions_taken"].append("restore attempted but no backup available")
            else:
                result["would_do"].append("restore from latest backup to repair corruption")

        # ── Check 2: file permissions ─────────────────────────────────────────
        try:
            mode = db_path.stat().st_mode & 0o777
            if mode != _FILE_MODE_DB:
                result["issues"].append(
                    f"insecure file permissions: {oct(mode)} (expected {oct(_FILE_MODE_DB)})"
                )
                if fix:
                    os.chmod(db_path, _FILE_MODE_DB)
                    result["actions_taken"].append(f"fixed permissions to {oct(_FILE_MODE_DB)}")
                else:
                    result["would_do"].append(f"chmod {oct(_FILE_MODE_DB)} {db_path.name}")
        except OSError as exc:
            result["issues"].append(f"could not stat database file: {exc}")

        # ── Check 3: WAL size ─────────────────────────────────────────────────
        wal = _wal_path(db_path)
        if wal.exists():
            wal_bytes = wal.stat().st_size
            if wal_bytes > _WAL_WARN_BYTES:
                result["issues"].append(
                    f"WAL file is large: {wal_bytes / (1024 * 1024):.1f} MB (threshold 100 MB)"
                )
                if fix:
                    self.checkpoint(db_path)
                    result["actions_taken"].append("ran WAL checkpoint (RESTART) to reduce WAL size")
                else:
                    result["would_do"].append("run WAL checkpoint (RESTART) to reduce WAL size")

        # ── Check 4: stale PID lockfile ───────────────────────────────────────
        lock_file = db_path.with_suffix(".lock")
        if lock_file.exists():
            stale = False
            try:
                pid = int(lock_file.read_text().strip())
                try:
                    os.kill(pid, 0)   # signal 0 = existence check only
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    pass   # process exists, we just can't signal it
            except (ValueError, OSError):
                stale = True   # unreadable / corrupt lockfile — treat as stale

            if stale:
                result["issues"].append(f"stale lockfile found: {lock_file.name}")
                if fix:
                    lock_file.unlink(missing_ok=True)
                    result["actions_taken"].append(f"removed stale lockfile {lock_file.name}")
                else:
                    result["would_do"].append(f"remove stale lockfile {lock_file.name}")

        # ── Check 5: schema version ───────────────────────────────────────────
        expected_version = _EXPECTED_SCHEMA_VERSIONS.get(db_path.name)
        if expected_version is not None:
            try:
                with _open(db_path) as conn:
                    actual_version = conn.execute("PRAGMA user_version").fetchone()[0]
                if actual_version != expected_version:
                    result["issues"].append(
                        f"schema version mismatch: got {actual_version}, "
                        f"expected {expected_version} — run db_init to migrate"
                    )
                    # Migration is handled by db_init, not here; no auto-fix.
                    if not fix:
                        result["would_do"].append(
                            "run db_init.initialize_all_databases() to apply pending migrations"
                        )
            except Exception as exc:
                result["issues"].append(f"could not read schema version: {exc}")

        result["duration_ms"] = _elapsed_ms(start)
        self._log_maintenance(
            db_path,
            "doctor",
            "ok" if result["ok"] else "issues_found",
            result,
        )
        return result

    # ── Maintenance log ───────────────────────────────────────────────────────

    def _log_maintenance(
        self,
        db_path: Path,
        operation: str,
        status: str,
        details: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Append a row to platform.db maintenance_log.

        Failures are swallowed so that a broken platform.db never prevents
        other maintenance operations from completing.
        """
        platform_db = self.data_dir / "platform.db"
        if not platform_db.exists():
            return

        # Prefer duration_ms from details dict if not supplied explicitly.
        if duration_ms is None and isinstance(details, dict):
            duration_ms = details.get("duration_ms")

        details_json: str | None = None
        if details:
            try:
                details_json = json.dumps(details, default=str)
            except (TypeError, ValueError):
                details_json = str(details)

        try:
            conn = sqlite3.connect(str(platform_db), check_same_thread=False)
            try:
                conn.execute(
                    """
                    INSERT INTO maintenance_log
                        (database_name, operation, status, details, duration_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        db_path.name,
                        operation,
                        status,
                        details_json,
                        duration_ms,
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            # Never let logging failures cascade into the calling operation.
            logger.debug("maintenance_log write failed (non-fatal): %s", exc)

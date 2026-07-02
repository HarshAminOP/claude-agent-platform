"""
CAP Database Manager — SQLite connection, migrations, WAL mode.

Provides:
- get_db(path) -> sqlite3.Connection with WAL mode and foreign keys
- migrate(db) -> runs all CREATE TABLE statements
- Default DB path: CAP_HOME/data/platform.db (via cap.config)
"""

import os
import sqlite3

from cap.config import get_platform_db_path

DEFAULT_DB_PATH = str(get_platform_db_path())


def get_db(path: str = None) -> sqlite3.Connection:
    """
    Get a SQLite connection with WAL mode and foreign keys enabled.

    Args:
        path: Path to the database file. Defaults to CAP_HOME/data/platform.db

    Returns:
        sqlite3.Connection configured for CAP usage.
    """
    if path is None:
        path = DEFAULT_DB_PATH

    # Ensure parent directory exists with restrictive permissions (owner-only)
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, mode=0o700, exist_ok=True)

    # Check if DB file already exists
    db_exists = os.path.exists(path)

    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # Set restrictive file permissions on newly created DB files
    if not db_exists and os.path.exists(path):
        os.chmod(path, 0o600)

    return conn


def migrate(db: sqlite3.Connection) -> None:
    """
    Run all CREATE TABLE statements for CAP.
    Uses IF NOT EXISTS for idempotent execution.

    Consolidates all schemas from the CAP System Design:
    - Section 4: Enforcement (edits, violations, agent_contexts, passthrough)
    - Section 5: Memory (active, archive, working, FTS5)
    - Section 6: Code Intelligence (files, symbols, relationships)
    - Section 8: Self-Learning (events, routing, corrections, trust)
    - Section 11: Cost & Runtime (cost_events, runtime_state)
    - Section 15: Patches (sync_triggers, rollback, orchestration_plans, checkpoints)
    - Section 16: Reliability (circuit_breaker, dead_letter, cascade, health)
    - Section 17: Witness Manifests
    - Section 18: DAG Task Plans
    """
    cursor = db.cursor()

    # ─── Section 4: Enforcement ───────────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS enforcement_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            delegated INTEGER NOT NULL DEFAULT 0,
            timestamp REAL NOT NULL,
            UNIQUE(session_id, file_path, delegated)
        );

        CREATE TABLE IF NOT EXISTS enforcement_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            tool_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            reason TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            workspace TEXT
        );

        CREATE TABLE IF NOT EXISTS passthrough (
            workspace TEXT PRIMARY KEY,
            enabled_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            reason TEXT,
            enabled_by TEXT DEFAULT 'user'
        );

        CREATE TABLE IF NOT EXISTS passthrough_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace TEXT NOT NULL,
            timestamp REAL NOT NULL,
            ttl INTEGER NOT NULL,
            reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_edits_session ON enforcement_edits(session_id);
        CREATE INDEX IF NOT EXISTS idx_violations_session ON enforcement_violations(session_id);
        CREATE INDEX IF NOT EXISTS idx_agent_ctx_session ON agent_contexts(session_id, active);
    """)

    # ─── Section 5: Memory ────────────────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS memory_active (
            id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            token_count INTEGER NOT NULL,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            access_count INTEGER DEFAULT 1,
            importance REAL DEFAULT 0.5,
            relevance_score REAL DEFAULT 0.5,
            frequency_score REAL DEFAULT 0.0,
            composite_score REAL DEFAULT 0.5,
            stale_since REAL,
            consolidated_into TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_archive (
            id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_ids TEXT NOT NULL,
            compressed_content BLOB,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            access_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS memory_working (
            session_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            content TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            loaded_at REAL NOT NULL,
            PRIMARY KEY (session_id, entry_id)
        );

        CREATE INDEX IF NOT EXISTS idx_active_workspace ON memory_active(workspace);
        CREATE INDEX IF NOT EXISTS idx_active_score ON memory_active(composite_score);
        CREATE INDEX IF NOT EXISTS idx_active_stale ON memory_active(stale_since);
        CREATE INDEX IF NOT EXISTS idx_archive_workspace ON memory_archive(workspace);
    """)

    # FTS5 virtual table for memory search
    # FTS5 tables cannot use IF NOT EXISTS, so we check manually
    fts_exists = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    if not fts_exists:
        cursor.executescript("""
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                content, category, workspace,
                content='memory_active',
                content_rowid='rowid'
            );
        """)

    # ─── Section 6: Code Intelligence ─────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS code_files (
            path TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            language TEXT NOT NULL,
            hash TEXT NOT NULL,
            extracted_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qualified_name TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            signature TEXT,
            docstring TEXT,
            parent TEXT,
            visibility TEXT DEFAULT 'public',
            FOREIGN KEY (file_path) REFERENCES code_files(path)
        );

        CREATE TABLE IF NOT EXISTS code_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line INTEGER,
            FOREIGN KEY (file_path) REFERENCES code_files(path)
        );

        CREATE INDEX IF NOT EXISTS idx_symbols_name ON code_symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON code_symbols(qualified_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON code_symbols(file_path);
        CREATE INDEX IF NOT EXISTS idx_rel_source ON code_relationships(source);
        CREATE INDEX IF NOT EXISTS idx_rel_target ON code_relationships(target);
        CREATE INDEX IF NOT EXISTS idx_rel_kind ON code_relationships(kind);
    """)

    # ─── Section 8: Self-Learning ─────────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            workspace TEXT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            session_id TEXT
        );

        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            session_id TEXT NOT NULL,
            task_description TEXT NOT NULL,
            complexity_score REAL NOT NULL,
            tier_selected TEXT NOT NULL,
            agents_used TEXT,
            task_hash TEXT,
            outcome TEXT,
            duration_ms INTEGER,
            token_cost INTEGER,
            user_satisfaction INTEGER
        );

        CREATE TABLE IF NOT EXISTS correction_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            correction TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            auto_generated INTEGER DEFAULT 0,
            baseline_rule TEXT
        );

        CREATE TABLE IF NOT EXISTS trust_levels (
            agent_type TEXT NOT NULL,
            action_type TEXT NOT NULL,
            trust_score REAL DEFAULT 0.5,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_updated REAL NOT NULL,
            PRIMARY KEY (agent_type, action_type)
        );

        CREATE INDEX IF NOT EXISTS idx_learning_type ON learning_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_routing_outcome ON routing_decisions(outcome);
        CREATE INDEX IF NOT EXISTS idx_corrections_pattern ON correction_patterns(pattern);
    """)

    # Add task_hash column if missing (handles DBs created before this column existed)
    try:
        cursor.execute("SELECT task_hash FROM routing_decisions LIMIT 0")
    except sqlite3.OperationalError:
        try:
            cursor.execute("ALTER TABLE routing_decisions ADD COLUMN task_hash TEXT")
        except sqlite3.OperationalError:
            pass

    # Create index on task_hash (safe now that column exists)
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_routing_task_hash ON routing_decisions(task_hash)")
    except sqlite3.OperationalError:
        pass

    # ─── Section 11: Cost & Runtime ───────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_type TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            workflow_id TEXT,
            timestamp REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_cost_time ON cost_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_cost_workflow ON cost_events(workflow_id);
    """)

    # ─── Section 15 Patches: Sync, Rollback, Orchestration ────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS sync_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            trigger_type TEXT NOT NULL,
            detail TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_pending (
            file_path TEXT PRIMARY KEY,
            changed_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rollback_sessions (
            orchestration_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rollback_tracked_files (
            orchestration_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            original_content TEXT,
            existed_before INTEGER NOT NULL DEFAULT 1,
            tracked_at REAL NOT NULL,
            PRIMARY KEY (orchestration_id, file_path)
        );

        CREATE TABLE IF NOT EXISTS orchestration_checkpoints (
            orchestration_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            data TEXT NOT NULL,
            phase TEXT DEFAULT 'running',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orchestration_plans (
            orchestration_id TEXT PRIMARY KEY,
            plan_data TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sync_triggers_ts ON sync_triggers(timestamp);
    """)

    # ─── Section 16: Reliability (Circuit Breaker, DLQ, Cascade, Health) ──────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            agent_type TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at REAL,
            updated_at REAL NOT NULL,
            failure_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS dead_letter_queue (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            task_description TEXT NOT NULL,
            failures_json TEXT NOT NULL,
            agent_type TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS cascade_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT,
            detected_at REAL NOT NULL,
            failure_count INTEGER,
            agent_types TEXT,
            resolution TEXT,
            resolved_at REAL
        );

        CREATE TABLE IF NOT EXISTS agent_health_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            estimated_tokens INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS agent_health_baselines (
            agent_type TEXT NOT NULL PRIMARY KEY,
            avg_duration REAL,
            avg_tool_calls INTEGER,
            avg_tokens INTEGER,
            p95_duration REAL,
            failure_rate REAL,
            sample_count INTEGER,
            updated_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_dlq_expires ON dead_letter_queue(expires_at);
        CREATE INDEX IF NOT EXISTS idx_health_agent ON agent_health_events(agent_id, timestamp DESC);
    """)

    # ─── Section 16A: Disagreement Resolutions ────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS disagreement_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            agent_a TEXT NOT NULL,
            agent_b TEXT NOT NULL,
            field TEXT NOT NULL,
            value_a TEXT,
            value_b TEXT,
            winner TEXT,
            method TEXT NOT NULL,
            confidence REAL,
            outcome TEXT,
            timestamp REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_disagree_domain ON disagreement_resolutions(domain);
    """)

    # ─── Section 17: Witness Manifests ────────────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS witness_manifests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '',
            stamped_at REAL NOT NULL,
            verified_at REAL,
            UNIQUE(file_path, content_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_witness_path ON witness_manifests(file_path);
        CREATE INDEX IF NOT EXISTS idx_witness_workflow ON witness_manifests(workflow_id);
    """)

    # Add signature column if missing (handles DBs created before this column existed)
    try:
        cursor.execute("SELECT signature FROM witness_manifests LIMIT 0")
    except sqlite3.OperationalError:
        try:
            cursor.execute("ALTER TABLE witness_manifests ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

    # ─── Section 18: DAG Task Plans & Steps ───────────────────────────────────

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS task_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            critical_path TEXT,
            parallelism_factor REAL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_steps (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            description TEXT NOT NULL,
            agent_type TEXT NOT NULL,
            depends_on TEXT,
            state TEXT NOT NULL DEFAULT 'pending',
            started_at REAL,
            completed_at REAL,
            result_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_steps_workflow ON task_steps(workflow_id, state);
    """)

    db.commit()

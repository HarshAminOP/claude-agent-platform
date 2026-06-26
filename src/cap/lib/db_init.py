"""Database initialization for the CAP platform.

Creates and migrates all four platform databases:
  platform.db  — workflow engine (extends existing schema)
  knowledge.db — knowledge server
  sessions.db  — session server
  fleet.db     — fleet manager

Each database uses WAL mode, busy_timeout=5000, foreign_keys=ON and is
created with 0600 permissions via umask.
"""

import os
import sqlite3
from pathlib import Path


# ── Schema versions ────────────────────────────────────────────────────────────

_PLATFORM_VERSION = 2   # existing schema is version 1; we add tables here
_KNOWLEDGE_VERSION = 1
_SESSIONS_VERSION = 1
_FLEET_VERSION = 1


# ── SQL ────────────────────────────────────────────────────────────────────────

_PLATFORM_ADDITIONS_SQL = """
CREATE TABLE IF NOT EXISTS budget_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    period TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    embedding_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_bl_workspace_period ON budget_ledger(workspace, period);
CREATE INDEX IF NOT EXISTS idx_bl_model ON budget_ledger(model);

CREATE TABLE IF NOT EXISTS maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    database_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ml_database ON maintenance_log(database_name);
"""

_KNOWLEDGE_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    source_path TEXT,
    source_type TEXT NOT NULL,
    content_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata TEXT,
    embedding_status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ke_workspace ON knowledge_entries(workspace);
CREATE INDEX IF NOT EXISTS idx_ke_source_path ON knowledge_entries(source_path);
CREATE INDEX IF NOT EXISTS idx_ke_content_type ON knowledge_entries(content_type);
CREATE INDEX IF NOT EXISTS idx_ke_embedding_status ON knowledge_entries(embedding_status);
CREATE INDEX IF NOT EXISTS idx_ke_content_hash ON knowledge_entries(content_hash);

CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
    id TEXT PRIMARY KEY,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    workspace TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kgn_entity ON knowledge_graph_nodes(entity_name, entity_type, workspace);

CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    predicate TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    metadata TEXT,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, target_id, predicate)
);
CREATE INDEX IF NOT EXISTS idx_kge_source ON knowledge_graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kge_target ON knowledge_graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kge_predicate ON knowledge_graph_edges(predicate);
CREATE INDEX IF NOT EXISTS idx_kge_workspace ON knowledge_graph_edges(workspace);

CREATE TABLE IF NOT EXISTS business_knowledge (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(workspace, category, key)
);
CREATE INDEX IF NOT EXISTS idx_bk_workspace_category ON business_knowledge(workspace, category);

CREATE TABLE IF NOT EXISTS sync_state (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    source_type TEXT NOT NULL,
    last_sync_at TEXT,
    last_commit_sha TEXT,
    file_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'never',
    error TEXT,
    UNIQUE(workspace, source_type)
);

CREATE TABLE IF NOT EXISTS embedding_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES knowledge_entries(id),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_status ON embedding_queue(status);
"""

_KNOWLEDGE_FTS_SQL = """
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    title, content, content_type, workspace,
    content='knowledge_entries', content_rowid='id',
    tokenize='porter unicode61'
);
"""

_KNOWLEDGE_TRIGGERS_SQL = """
CREATE TRIGGER knowledge_fts_insert AFTER INSERT ON knowledge_entries BEGIN
    INSERT INTO knowledge_fts(rowid, title, content, content_type, workspace)
    VALUES (new.id, new.title, new.content, new.content_type, new.workspace);
END;
CREATE TRIGGER knowledge_fts_delete AFTER DELETE ON knowledge_entries BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, content_type, workspace)
    VALUES ('delete', old.id, old.title, old.content, old.content_type, old.workspace);
END;
CREATE TRIGGER knowledge_fts_update AFTER UPDATE ON knowledge_entries BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, content_type, workspace)
    VALUES ('delete', old.id, old.title, old.content, old.content_type, old.workspace);
    INSERT INTO knowledge_fts(rowid, title, content, content_type, workspace)
    VALUES (new.id, new.title, new.content, new.content_type, new.workspace);
END;
"""

_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT,
    context TEXT,
    stats TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_type TEXT NOT NULL,
    category TEXT,
    content TEXT NOT NULL,
    data TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_se_session ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_se_type ON session_events(event_type);

CREATE TABLE IF NOT EXISTS learnings (
    id TEXT PRIMARY KEY,
    workspace TEXT,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    times_applied INTEGER DEFAULT 0,
    times_reinforced INTEGER DEFAULT 0,
    source_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_applied_at TEXT,
    UNIQUE(workspace, category, key)
);
CREATE INDEX IF NOT EXISTS idx_learnings_workspace ON learnings(workspace);
CREATE INDEX IF NOT EXISTS idx_learnings_category ON learnings(category);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    workspace TEXT NOT NULL,
    domain TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    alternatives_considered TEXT,
    outcome TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_workspace ON decisions(workspace);
CREATE INDEX IF NOT EXISTS idx_decisions_domain ON decisions(domain);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    workspace TEXT,
    what_was_wrong TEXT NOT NULL,
    what_is_correct TEXT NOT NULL,
    category TEXT,
    applied_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_corrections_workspace ON corrections(workspace);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);
"""

_SESSIONS_FTS_SQL = """
CREATE VIRTUAL TABLE decisions_fts USING fts5(
    decision, rationale,
    content='decisions', content_rowid='rowid',
    tokenize='porter unicode61'
);
"""

_SESSIONS_TRIGGERS_SQL = """
CREATE TRIGGER decisions_fts_insert AFTER INSERT ON decisions BEGIN
    INSERT INTO decisions_fts(rowid, decision, rationale)
    VALUES (new.rowid, new.decision, new.rationale);
END;
CREATE TRIGGER decisions_fts_delete AFTER DELETE ON decisions BEGIN
    INSERT INTO decisions_fts(decisions_fts, rowid, decision, rationale)
    VALUES ('delete', old.rowid, old.decision, old.rationale);
END;
"""

_FLEET_SQL = """
CREATE TABLE IF NOT EXISTS fleet_servers (
    name TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    args TEXT,
    env TEXT,
    health_check TEXT,
    status TEXT NOT NULL DEFAULT 'registered',
    pid INTEGER,
    last_health_check TEXT,
    restart_count INTEGER DEFAULT 0,
    max_restarts INTEGER DEFAULT 5,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    config TEXT
);

CREATE TABLE IF NOT EXISTS fleet_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL REFERENCES fleet_servers(name),
    event_type TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fe_server ON fleet_events(server_name);
"""


# ── Core helpers ───────────────────────────────────────────────────────────────

def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def create_database(path: Path) -> sqlite3.Connection:
    """Create a SQLite database at *path* with 0600 permissions.

    Uses the umask trick: temporarily set umask to 0177 so that the file
    sqlite3.connect() creates gets mode 0600.  The original umask is restored
    immediately after the file exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o177)
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    finally:
        os.umask(old_umask)
    _configure(conn)
    return conn


def _open_existing(path: Path) -> sqlite3.Connection:
    """Open a database that already exists without altering its permissions."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    _configure(conn)
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    assert isinstance(version, int) and 0 <= version < 10000
    conn.execute(f"PRAGMA user_version = {version}")


def _try_create_virtual(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a CREATE VIRTUAL TABLE statement, ignoring 'already exists'."""
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError as exc:
        if "already exists" not in str(exc):
            raise


def _try_create_triggers(conn: sqlite3.Connection, sql: str) -> None:
    """Execute trigger CREATE statements, ignoring 'already exists'.

    Splits on 'END;' boundaries since trigger bodies contain internal semicolons.
    """
    parts = sql.split("END;")
    for part in parts:
        stmt = part.strip()
        if not stmt:
            continue
        stmt += "END;"
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "already exists" not in str(exc):
                raise
    conn.commit()


# ── Public init functions ──────────────────────────────────────────────────────

def init_platform_db(data_dir: Path) -> sqlite3.Connection:
    """Open (or create) platform.db and apply any pending migrations."""
    path = data_dir / "platform.db"
    if path.exists():
        conn = _open_existing(path)
    else:
        conn = create_database(path)

    current = _version(conn)

    if current < 1:
        # Bootstrap base schema (mirrors models.py SCHEMA_SQL without version pragma)
        conn.executescript("""
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    budget_tokens INTEGER NOT NULL DEFAULT 500000,
    max_agents INTEGER NOT NULL DEFAULT 15,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    agents_spawned INTEGER NOT NULL DEFAULT 0,
    killed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status);

CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    event_type TEXT NOT NULL,
    agent_id TEXT,
    phase TEXT,
    message TEXT,
    tokens_delta INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_workflow ON workflow_events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON workflow_events(timestamp);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    agent_id TEXT,
    model_tier TEXT NOT NULL,
    model_id TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    cost_usd REAL NOT NULL DEFAULT 0.0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_workflow ON api_calls(workflow_id);
CREATE INDEX IF NOT EXISTS idx_api_timestamp ON api_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_model ON api_calls(model_tier);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
        """)
        _set_version(conn, 1)
        conn.commit()

    if current < 2:
        conn.executescript(_PLATFORM_ADDITIONS_SQL)
        _set_version(conn, _PLATFORM_VERSION)
        conn.commit()

    return conn


def init_knowledge_db(data_dir: Path) -> sqlite3.Connection:
    """Create or open knowledge.db and apply schema."""
    path = data_dir / "knowledge.db"
    if path.exists():
        conn = _open_existing(path)
    else:
        conn = create_database(path)

    if _version(conn) < _KNOWLEDGE_VERSION:
        conn.executescript(_KNOWLEDGE_SQL)
        _try_create_virtual(conn, _KNOWLEDGE_FTS_SQL)
        _try_create_triggers(conn, _KNOWLEDGE_TRIGGERS_SQL)
        _set_version(conn, _KNOWLEDGE_VERSION)
        conn.commit()

    return conn


def init_sessions_db(data_dir: Path) -> sqlite3.Connection:
    """Create or open sessions.db and apply schema."""
    path = data_dir / "sessions.db"
    if path.exists():
        conn = _open_existing(path)
    else:
        conn = create_database(path)

    if _version(conn) < _SESSIONS_VERSION:
        conn.executescript(_SESSIONS_SQL)
        _try_create_virtual(conn, _SESSIONS_FTS_SQL)
        _try_create_triggers(conn, _SESSIONS_TRIGGERS_SQL)
        _set_version(conn, _SESSIONS_VERSION)
        conn.commit()

    return conn


def init_fleet_db(data_dir: Path) -> sqlite3.Connection:
    """Create or open fleet.db and apply schema."""
    path = data_dir / "fleet.db"
    if path.exists():
        conn = _open_existing(path)
    else:
        conn = create_database(path)

    if _version(conn) < _FLEET_VERSION:
        conn.executescript(_FLEET_SQL)
        _set_version(conn, _FLEET_VERSION)
        conn.commit()

    return conn


def initialize_all_databases(data_dir: str | Path) -> None:
    """Initialize all four CAP databases under *data_dir*."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    init_platform_db(data_dir)
    init_knowledge_db(data_dir)
    init_sessions_db(data_dir)
    init_fleet_db(data_dir)

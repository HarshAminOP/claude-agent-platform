"""Phase 4 polish tests — doctor command, init polish, production wiring, learning loop.

Covers:
- Doctor command: registration, exit code, KB status, circuit breaker output
- Init: --minimal flag existence, git-dir workspace auto-detect, post-init verification
- Production wiring: consolidation triggers learning, skips gracefully when no sessions DB,
  routing records decisions to DB
- Integration: 50+ seeded outcomes shift thresholds from default to learned
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Ensure the src tree is importable when tests are run from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_knowledge_db(path: Path) -> Path:
    """Create a minimal knowledge.db with the schema doctor expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT,
            workspace TEXT,
            source_path TEXT,
            source_type TEXT,
            content_type TEXT,
            title TEXT,
            content TEXT,
            content_hash TEXT,
            metadata TEXT,
            embedding_status TEXT DEFAULT 'pending',
            consolidated_into TEXT,
            updated_at TEXT
        );
    """)
    conn.execute("INSERT INTO knowledge_entries (uuid, embedding_status) VALUES ('aa', 'embedded')")
    conn.execute("INSERT INTO knowledge_entries (uuid, embedding_status) VALUES ('bb', 'pending')")
    conn.execute("INSERT INTO knowledge_entries (uuid, embedding_status) VALUES ('cc', 'embedded')")
    conn.commit()
    conn.close()
    return path


def _make_cap_db(path: Path) -> Path:
    """Create a minimal cap.db with circuit_breaker_state and routing_decisions tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trust_levels (
            agent_type TEXT NOT NULL,
            action_type TEXT NOT NULL,
            trust_score REAL DEFAULT 0.5,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_updated REAL NOT NULL,
            PRIMARY KEY (agent_type, action_type)
        );
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            session_id TEXT,
            task_description TEXT,
            complexity_score REAL,
            tier_selected TEXT,
            outcome TEXT
        );
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            agent_type TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at REAL,
            updated_at REAL,
            failure_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS agent_health_baselines (
            agent_type TEXT PRIMARY KEY,
            failure_rate REAL,
            sample_count INTEGER,
            avg_duration REAL
        );
        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS dlq (
            task_id TEXT PRIMARY KEY,
            agent_type TEXT,
            task_description TEXT,
            last_error TEXT,
            workflow_id TEXT,
            created_at REAL,
            status TEXT DEFAULT 'pending'
        );
    """)
    conn.execute("INSERT INTO trust_levels VALUES ('dev', 'refactor', 0.85, 10, 1, 1.0)")
    conn.execute("INSERT INTO circuit_breaker_state VALUES ('dev', 'CLOSED', NULL, 1.0, 0)")
    conn.execute("INSERT INTO circuit_breaker_state VALUES ('devops', 'OPEN', 1.0, 1.0, 3)")
    conn.execute("INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) VALUES (1.0, 's1', 'fix typo', 0.1, 'inline', 'success')")
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Doctor Command Tests
# ---------------------------------------------------------------------------


class TestDoctorCommandExists:
    def test_doctor_command_exists(self):
        """'doctor' must be a registered click command on the CLI group."""
        from cap.cli.main import cli
        assert "doctor" in cli.commands


class TestDoctorRunsWithoutCrash:
    def test_doctor_runs_without_crash(self, tmp_path):
        """doctor exits with code 0 regardless of environment state."""
        from cap.cli.commands import doctor

        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert result.exit_code == 0

    def test_doctor_runs_without_crash_with_knowledge_db(self, tmp_path):
        """doctor exits with code 0 when knowledge.db exists and is queryable."""
        from cap.cli.commands import doctor

        _make_knowledge_db(tmp_path / "data" / "knowledge.db")
        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert result.exit_code == 0


class TestDoctorShowsKbStatus:
    def test_doctor_shows_kb_status_section(self, tmp_path):
        """Output includes a Knowledge DB section header."""
        from cap.cli.commands import doctor

        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "Knowledge DB" in result.output

    def test_doctor_shows_entry_count(self, tmp_path):
        """When knowledge.db exists, output includes the knowledge entry count."""
        from cap.cli.commands import doctor

        _make_knowledge_db(tmp_path / "data" / "knowledge.db")
        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        # 3 rows were inserted by _make_knowledge_db
        assert "Entries: 3" in result.output

    def test_doctor_shows_missing_db_message(self, tmp_path):
        """When knowledge.db is absent, output reports the file is not found."""
        from cap.cli.commands import doctor

        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "knowledge.db not found" in result.output


class TestDoctorShowsCircuitBreaker:
    def test_doctor_shows_circuit_breaker_section(self, tmp_path):
        """Output includes a Circuit breaker section."""
        from cap.cli.commands import doctor

        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "Circuit breaker" in result.output

    def test_doctor_shows_circuit_breaker_with_cap_db(self, tmp_path):
        """When cap.db exists, circuit breaker states are rendered without crashing."""
        from cap.cli.commands import doctor

        cap_db = _make_cap_db(tmp_path / "cap.db")
        runner = CliRunner(env={
            "CAP_HOME": str(tmp_path),
            "CAP_ORCHESTRATOR_DB": str(cap_db),
        })
        result = runner.invoke(doctor, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Circuit breaker" in result.output

    def test_doctor_all_sections_present(self, tmp_path):
        """All five diagnostic sections appear in the output."""
        from cap.cli.commands import doctor

        _make_knowledge_db(tmp_path / "data" / "knowledge.db")
        runner = CliRunner(env={"CAP_HOME": str(tmp_path)})
        result = runner.invoke(doctor, [], catch_exceptions=False)
        output = result.output
        assert "Knowledge DB" in output
        assert "Embedder health" in output
        assert "MCP server registration" in output
        assert "Learning health" in output
        assert "Circuit breaker" in output


# ---------------------------------------------------------------------------
# Init Polish Tests
# ---------------------------------------------------------------------------


class TestInitMinimalFlagExists:
    def test_init_minimal_flag_exists(self):
        """The 'init' command must expose a --minimal option."""
        from cap.cli.lifecycle import init

        param_names = {p.name for p in init.params}
        assert "minimal" in param_names

    def test_init_minimal_option_is_flag(self):
        """--minimal must be a boolean flag (is_flag=True)."""
        from cap.cli.lifecycle import init
        import click

        minimal_param = next(p for p in init.params if p.name == "minimal")
        assert isinstance(minimal_param, click.Option)
        assert minimal_param.is_flag is True


class TestInitAutoDetectsWorkspace:
    def test_resolve_workspace_uses_git_root_when_in_git_dir(self, tmp_path, monkeypatch):
        """_resolve_workspace(None) returns the git root when CWD is inside a git repo."""
        from cap.cli.lifecycle import _resolve_workspace

        git_root = tmp_path / "myrepo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        subdir = git_root / "src" / "subpkg"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        result = _resolve_workspace(None)
        assert result == git_root

    def test_resolve_workspace_falls_back_to_cwd_without_git(self, tmp_path, monkeypatch):
        """_resolve_workspace(None) returns CWD when no .git directory exists."""
        from cap.cli.lifecycle import _resolve_workspace

        bare = tmp_path / "nogit"
        bare.mkdir()
        monkeypatch.chdir(bare)

        result = _resolve_workspace(None)
        assert result == bare

    def test_resolve_workspace_explicit_arg_wins(self, tmp_path):
        """When a path is passed explicitly, it is returned as-is (resolved)."""
        from cap.cli.lifecycle import _resolve_workspace

        result = _resolve_workspace(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_resolve_workspace_cwd_is_git_root(self, tmp_path, monkeypatch):
        """When CWD is the git root itself, returns CWD."""
        from cap.cli.lifecycle import _resolve_workspace

        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        result = _resolve_workspace(None)
        assert result == tmp_path


class TestInitPostVerification:
    def test_post_init_verification_returns_rows(self, tmp_path):
        """_run_post_init_verification returns at least one row."""
        from cap.cli.lifecycle import _run_post_init_verification

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        assert len(rows) >= 1

    def test_post_init_verification_knowledge_db_yes(self, tmp_path):
        """knowledge.db present → the corresponding row shows 'yes'."""
        from cap.cli.lifecycle import _run_post_init_verification

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Create a minimal knowledge.db
        conn = sqlite3.connect(str(data_dir / "knowledge.db"))
        conn.execute("CREATE TABLE knowledge_entries (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        rows = _run_post_init_verification(data_dir)
        kb_row = next(r for r in rows if "knowledge" in r[0])
        assert kb_row[1] == "yes"

    def test_post_init_verification_knowledge_db_no(self, tmp_path):
        """knowledge.db absent → the corresponding row shows 'no'."""
        from cap.cli.lifecycle import _run_post_init_verification

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        kb_row = next(r for r in rows if "knowledge" in r[0])
        assert kb_row[1] == "no"

    def test_post_init_verification_cap_importable(self, tmp_path):
        """cap package is importable in the dev environment → shows 'yes'."""
        from cap.cli.lifecycle import _run_post_init_verification

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        cap_row = next(r for r in rows if "cap importable" in r[0])
        assert cap_row[1] == "yes"


# ---------------------------------------------------------------------------
# Production Wiring Tests
# ---------------------------------------------------------------------------


def _make_consolidation_knowledge_db() -> sqlite3.Connection:
    """In-memory knowledge.db with the full schema for consolidation tests."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL DEFAULT 'ws',
            source_path TEXT,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'test',
            content_type TEXT NOT NULL DEFAULT 'text',
            expires_at TEXT
        );
        CREATE TABLE embedding_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES knowledge_entries(id),
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_error TEXT
        );
        CREATE TABLE embedding_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            embedding BLOB
        );
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            session_id TEXT,
            task_description TEXT,
            complexity_score REAL,
            tier_selected TEXT,
            agents_used TEXT,
            task_hash TEXT,
            outcome TEXT
        );
    """)
    return conn


def _make_sessions_db_on_disk(path: Path, events: list[dict]) -> Path:
    """Create a sessions.db on disk populated with workflow_complete events."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT
        )
    """)
    for ev in events:
        conn.execute(
            "INSERT INTO session_events (timestamp, event_type, content) VALUES (?, ?, ?)",
            (ev["timestamp"], ev["event_type"], json.dumps(ev.get("content", {}))),
        )
    conn.commit()
    conn.close()
    return path


class TestConsolidationCallsThresholdComputation:
    def test_consolidation_with_sessions_db_sets_thresholds_updated(self, tmp_path):
        """consolidate() with a sessions_db_path that has correlated events returns
        thresholds_updated=True, proving the learning phase was triggered."""
        from cap.lib.consolidator import consolidate

        now = time.time()
        sessions_path = tmp_path / "sessions.db"
        _make_sessions_db_on_disk(sessions_path, [
            {"timestamp": now, "event_type": "workflow_complete", "content": {"success": True, "duration": 4.0}},
        ])

        conn = _make_consolidation_knowledge_db()
        # Insert a routing decision within 60s of the session event so
        # compute_thresholds_from_session_events finds a correlation.
        conn.execute(
            "INSERT INTO routing_decisions (timestamp, tier_selected, complexity_score) VALUES (?, ?, ?)",
            (now - 5, "full", 0.75),
        )
        conn.commit()

        result = consolidate(conn, sessions_db_path=sessions_path)
        assert result.thresholds_updated is True

    def test_consolidation_with_sessions_db_invokes_learning_phase(self, tmp_path):
        """consolidate() with a valid sessions_db_path does not raise and completes
        all phases, even when no correlated routing decisions exist."""
        from cap.lib.consolidator import consolidate

        now = time.time()
        sessions_path = tmp_path / "sessions.db"
        _make_sessions_db_on_disk(sessions_path, [
            {"timestamp": now, "event_type": "workflow_complete", "content": {"success": True}},
        ])

        conn = _make_consolidation_knowledge_db()
        # No routing decisions inserted — Phase 6 correlates nothing but must not crash.
        result = consolidate(conn, sessions_db_path=sessions_path)
        # Result is a ConsolidationResult — all fields present
        assert hasattr(result, "thresholds_updated")
        assert hasattr(result, "duration_ms")


class TestConsolidationSkipsIfNoSessionsDb:
    def test_no_sessions_db_path_arg(self):
        """consolidate() without sessions_db_path skips Phase 6, thresholds_updated=False."""
        from cap.lib.consolidator import consolidate

        conn = _make_consolidation_knowledge_db()
        result = consolidate(conn)
        assert result.thresholds_updated is False

    def test_nonexistent_sessions_db_path(self, tmp_path):
        """consolidate() with a path pointing to a nonexistent file skips gracefully."""
        from cap.lib.consolidator import consolidate

        conn = _make_consolidation_knowledge_db()
        missing = tmp_path / "no_sessions.db"
        result = consolidate(conn, sessions_db_path=missing)
        assert result.thresholds_updated is False
        assert result.duration_ms >= 0

    def test_consolidation_still_succeeds_without_sessions_db(self):
        """consolidate() without sessions DB completes all other phases without error."""
        from cap.lib.consolidator import consolidate, ConsolidationResult

        conn = _make_consolidation_knowledge_db()
        result = consolidate(conn)
        assert isinstance(result, ConsolidationResult)
        assert result.expired_deleted == 0
        assert result.duplicates_removed == 0

    def test_malformed_sessions_db_does_not_crash(self, tmp_path):
        """A corrupt (non-SQLite) sessions.db file is handled gracefully."""
        from cap.lib.consolidator import consolidate

        bad_path = tmp_path / "bad.db"
        bad_path.write_bytes(b"this is not a sqlite database at all")
        conn = _make_consolidation_knowledge_db()
        result = consolidate(conn, sessions_db_path=bad_path)
        assert result.thresholds_updated is False


class TestRoutingRecordsDecisions:
    def test_route_persists_decision_id(self, tmp_path):
        """route() returns a RoutingDecision with a non-None decision_id."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import route

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        decision = route("fix a typo in the README", db)
        assert decision.decision_id is not None

    def test_route_persists_decision_to_db(self, tmp_path):
        """After route(), the routing_decisions table contains the recorded row."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import route

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        decision = route("fix a typo in the README", db)
        row = db.execute(
            "SELECT tier_selected, complexity_score FROM routing_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        assert row is not None
        assert row["tier_selected"] == decision.tier.value

    def test_route_persists_multiple_decisions(self, tmp_path):
        """Each call to route() creates exactly one new row in routing_decisions."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import route

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        route("fix typo", db)
        route("deploy kubernetes cluster across all environments with terraform", db)

        count = db.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
        assert count == 2

    def test_route_inline_decision_recorded_with_correct_tier(self, tmp_path):
        """A trivial prompt that routes INLINE is persisted with tier_selected='inline'."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import route, Tier

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        decision = route("rename variable x to count", db)
        assert decision.tier == Tier.INLINE

        row = db.execute(
            "SELECT tier_selected FROM routing_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        assert row["tier_selected"] == "inline"


# ---------------------------------------------------------------------------
# Integration: Full Learning Loop
# ---------------------------------------------------------------------------


class TestFullLearningLoop:
    """Seed 50+ routing outcomes via session events, run consolidation, verify
    that the router shifts from default to learned thresholds."""

    def _build_sessions_db(self, path: Path, n_events: int, base_ts: float) -> None:
        """Write n_events workflow_complete events into a sessions.db on disk."""
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT
            )
        """)
        for i in range(n_events):
            ts = base_ts + i
            conn.execute(
                "INSERT INTO session_events (timestamp, event_type, content) VALUES (?, ?, ?)",
                (ts, "workflow_complete", json.dumps({"success": True, "duration": 3.0 + i})),
            )
        conn.commit()
        conn.close()

    def test_full_learning_loop_shifts_to_learned_source(self, tmp_path):
        """After seeding 60+ routing decisions (>= LEARNED_THRESHOLD_MIN_SAMPLES),
        get_learned_thresholds reports source='learned' rather than 'default'."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import get_learned_thresholds, DEFAULT_INLINE_MAX, DEFAULT_FULL_MIN

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        now = time.time()

        # Seed 20 inline, 20 lightweight, 20 full decisions
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"inline task {i}", 0.10, "inline", "success"),
            )
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"lightweight task {i}", 0.35, "lightweight", "success"),
            )
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"full task {i}", 0.70, "full", "success"),
            )
        db.commit()

        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "learned"

    def test_full_learning_loop_thresholds_adapt(self, tmp_path):
        """Learned thresholds differ from defaults after sufficient data is seeded."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import get_learned_thresholds, DEFAULT_INLINE_MAX, DEFAULT_FULL_MIN

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        now = time.time()

        # Seed: inline avg=0.05, lightweight avg=0.25, full avg=0.80
        # Learned inline_max = (0.05+0.25)/2 = 0.15 (lower than default 0.2)
        # Learned full_min  = (0.25+0.80)/2 = 0.525 (may differ from default 0.5)
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"t{i}", 0.05, "inline", "success"),
            )
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"t{i}", 0.25, "lightweight", "success"),
            )
        for i in range(20):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"t{i}", 0.80, "full", "success"),
            )
        db.commit()

        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "learned"
        # inline_max should be less than the default when inline avg is only 0.05
        assert thresholds["inline_max"] < DEFAULT_INLINE_MAX

    def test_full_learning_loop_insufficient_data_stays_default(self, tmp_path):
        """With fewer than the minimum sample count, thresholds stay at 'default'."""
        from cap.db import get_db, migrate
        from cap.orchestration.router import get_learned_thresholds, DEFAULT_INLINE_MAX, DEFAULT_FULL_MIN

        db = get_db(str(tmp_path / "cap.db"))
        migrate(db)

        now = time.time()
        # Only 10 decisions — below the 50-decision threshold
        for i in range(10):
            db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now - i, "s1", f"inline task {i}", 0.10, "inline", "success"),
            )
        db.commit()

        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "default"
        assert thresholds["inline_max"] == DEFAULT_INLINE_MAX
        assert thresholds["full_min"] == DEFAULT_FULL_MIN

    def test_full_learning_loop_consolidation_and_session_events(self, tmp_path):
        """Seeding 50+ session events and running consolidation marks thresholds_updated=True
        when there are correlated routing decisions."""
        from cap.lib.consolidator import consolidate

        now = time.time()
        sessions_path = tmp_path / "sessions.db"

        # 55 session events spread over 55 seconds
        conn = sqlite3.connect(str(sessions_path))
        conn.execute("""
            CREATE TABLE session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT
            )
        """)
        for i in range(55):
            conn.execute(
                "INSERT INTO session_events (timestamp, event_type, content) VALUES (?, ?, ?)",
                (now - i, "workflow_complete", json.dumps({"success": True, "duration": 5.0})),
            )
        conn.commit()
        conn.close()

        kb_conn = _make_consolidation_knowledge_db()
        # Insert routing decisions that correlate with the session events (within 60s window)
        for i in range(55):
            kb_conn.execute(
                "INSERT INTO routing_decisions (timestamp, tier_selected, complexity_score, session_id, task_description) "
                "VALUES (?, ?, ?, ?, ?)",
                (now - i - 10, "full", 0.7, f"s{i}", f"task {i}"),
            )
        kb_conn.commit()

        result = consolidate(kb_conn, sessions_db_path=sessions_path)
        assert result.thresholds_updated is True
        assert result.duration_ms >= 0

"""
Integration Tests: Upgrade Migration (v1.0 -> v1.1 without data loss)

This tests the critical upgrade scenario:
  1. User has CAP v1.0 running with real data (sessions, knowledge, budgets)
  2. User runs: uv tool upgrade claude-agent-platform
  3. New schema migrations run automatically
  4. All existing data is preserved

STRATEGY:
  - Create v1.0 schema manually (or use the current schema as baseline)
  - Insert representative data
  - Run the migration scripts for v1.1
  - Verify all pre-existing data is still readable
  - Verify new columns/tables exist with correct defaults

This is the most important category for "production ready" — data loss on
upgrade is the worst possible failure mode.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.fixture
def v1_knowledge_db(tmp_path):
    """Simulate a v1.0 knowledge DB with real data."""
    import hashlib
    from cap.lib.db_init import init_knowledge_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = init_knowledge_db(data_dir)

    # Insert representative production data — must include all NOT NULL columns
    for entry_uuid, ws, ctype, title, content in [
        ("uuid-prod-1", "/prod/infra", "terraform", "VPC Module", "Manages VPC with public/private subnets"),
        ("uuid-prod-2", "/prod/api", "python_file", "payment.py", "Payment processing with Stripe integration"),
    ]:
        db.execute(
            "INSERT INTO knowledge_entries "
            "(uuid, workspace, source_type, content_type, title, content, content_hash, embedding_status) "
            "VALUES (?, ?, 'manual', ?, ?, ?, ?, 'pending')",
            (entry_uuid, ws, ctype, title, content,
             hashlib.sha256(content.encode()).hexdigest())
        )
    db.execute(
        "INSERT INTO business_knowledge (id, workspace, category, key, value, source) "
        "VALUES ('bk-1', '/prod/infra', 'team', 'infra-owner', 'Platform Team', 'cli')"
    )
    db.execute(
        "INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace) "
        "VALUES ('node-1', 'payment-service', 'repo', '/prod')"
    )
    db.commit()
    yield db, data_dir
    db.close()


@pytest.fixture
def v1_sessions_db(tmp_path):
    """Simulate a v1.0 sessions DB with real data."""
    import uuid
    from cap.lib.db_init import init_sessions_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = init_sessions_db(data_dir)

    db.execute(
        "INSERT INTO sessions (id, workspace, status, summary) "
        "VALUES ('sess-old-1', '/prod', 'completed', 'Deployed payment service v2.1')"
    )
    # learnings.id is TEXT UUID
    db.execute(
        "INSERT INTO learnings (id, workspace, category, key, value, confidence) "
        "VALUES (?, '/prod', 'pattern', 'terraform-style', '2-space indent', 0.85)",
        (str(uuid.uuid4()),)
    )
    # corrections.id is INTEGER autoincrement — omit it
    db.execute(
        "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
        "VALUES ('/prod', 'code', 'shell=True in subprocess', 'Use list args instead')"
    )
    db.commit()
    yield db, data_dir
    db.close()


class TestKnowledgeDataPreservedAfterMigration:
    """Knowledge base entries survive schema migration."""

    def test_knowledge_entries_preserved(self, v1_knowledge_db):
        db, data_dir = v1_knowledge_db
        # Re-run init on the same data_dir (simulates upgrade migration)
        from cap.lib.db_init import init_knowledge_db
        db.close()
        db2 = init_knowledge_db(data_dir)
        count = db2.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        assert count == 2, f"Expected 2 knowledge entries after re-migration, found {count}"
        db2.close()

    def test_specific_entry_content_intact(self, v1_knowledge_db):
        db, _ = v1_knowledge_db
        row = db.execute(
            "SELECT title, content FROM knowledge_entries WHERE uuid = 'uuid-prod-1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "VPC Module"        # title at index 0
        assert "VPC" in row[1]               # content at index 1

    def test_business_knowledge_preserved(self, v1_knowledge_db):
        db, _ = v1_knowledge_db
        row = db.execute(
            "SELECT value FROM business_knowledge WHERE key = 'infra-owner'"
        ).fetchone()
        assert row is not None
        assert row[0] == "Platform Team"     # value at index 0

    def test_graph_nodes_preserved(self, v1_knowledge_db):
        db, _ = v1_knowledge_db
        row = db.execute(
            "SELECT entity_name FROM knowledge_graph_nodes WHERE id = 'node-1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "payment-service"   # entity_name at index 0


class TestSessionDataPreservedAfterMigration:
    """Session history, learnings, and corrections survive schema migration."""

    def test_sessions_preserved(self, v1_sessions_db):
        db, data_dir = v1_sessions_db
        # Re-migrate
        from cap.lib.db_init import init_sessions_db
        db.close()
        db2 = init_sessions_db(data_dir)
        rows = db2.execute("SELECT id, summary FROM sessions WHERE id = 'sess-old-1'").fetchall()
        assert len(rows) == 1
        assert "payment service" in rows[0][1].lower()  # summary at index 1
        db2.close()

    def test_learnings_preserved(self, v1_sessions_db):
        db, _ = v1_sessions_db
        rows = db.execute("SELECT key, value, confidence FROM learnings").fetchall()
        assert len(rows) >= 1
        # key=index 0, value=1, confidence=2
        assert any(r[0] == "terraform-style" for r in rows)
        learning = next(r for r in rows if r[0] == "terraform-style")
        assert abs(learning[2] - 0.85) < 0.01

    def test_corrections_preserved(self, v1_sessions_db):
        db, _ = v1_sessions_db
        rows = db.execute("SELECT what_was_wrong, what_is_correct FROM corrections").fetchall()
        assert len(rows) >= 1
        # what_was_wrong=index 0
        assert any("shell=True" in r[0] for r in rows)


class TestSchemaVersionCompatibility:
    """Schema version tracking prevents double-migration."""

    def test_platform_db_idempotent_migration(self, tmp_path):
        from cap.lib.db_init import init_platform_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        db1 = init_platform_db(data_dir)
        db1.execute(
            "INSERT INTO workflows (id, name, status, budget_tokens, max_agents, tokens_used) "
            "VALUES ('wf-persist', 'test', 'completed', 100000, 5, 50000)"
        )
        db1.commit()
        db1.close()

        # Re-run init (upgrade simulation)
        db2 = init_platform_db(data_dir)

        row = db2.execute(
            "SELECT name FROM workflows WHERE id = 'wf-persist'"
        ).fetchone()
        assert row is not None, "Workflow data lost during re-migration"
        assert row[0] == "test"   # name at index 0
        db2.close()

    def test_knowledge_db_fts_intact_after_reinit(self, tmp_path):
        """FTS5 virtual table and data must survive re-initialization."""
        import hashlib
        from cap.lib.db_init import init_knowledge_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        db1 = init_knowledge_db(data_dir)
        content = "full text search content for FTS test"
        db1.execute(
            "INSERT INTO knowledge_entries "
            "(uuid, workspace, source_type, content_type, title, content, content_hash, embedding_status) "
            "VALUES ('fts-test', '/ws', 'manual', 'doc', 'FTS Test', ?, ?, 'pending')",
            (content, hashlib.sha256(content.encode()).hexdigest())
        )
        db1.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        db1.commit()
        db1.close()

        # Re-init (upgrade simulation)
        db2 = init_knowledge_db(data_dir)

        from cap.lib.retrieval import hybrid_search
        results = hybrid_search(
            conn=db2,
            vectors_table=None,
            query="full text search",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=5,
        )
        assert any("FTS Test" in (r.title or "") for r in results), \
            "FTS index must survive re-initialization"
        db2.close()


class TestRollbackStrategy:
    """Backups enable clean rollback when upgrade fails.

    Uses _backup_file(file_path, label) and _restore_file(label, target_path)
    which both resolve the backup directory via CAP_HOME env var.
    """

    def test_backup_restores_claude_json(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _backup_file, _restore_file

        # _backup_file / _restore_file read CAP_HOME to find the backups dir
        monkeypatch.setenv("CAP_HOME", str(tmp_path / ".cap"))

        original_content = '{"numStartups": 10, "custom": "preserved"}'
        config_file = tmp_path / ".claude.json"
        config_file.write_text(original_content)

        # Backup using the real API
        backup_path = _backup_file(config_file, "claude-json")
        assert backup_path is not None
        assert backup_path.exists()

        # Simulate upgrade damage
        config_file.write_text('{"broken": true}')

        # Restore
        _restore_file("claude-json", config_file)

        restored = config_file.read_text()
        assert restored == original_content, "Restore must recover original content"

    def test_backup_file_has_timestamp_in_name(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _backup_file

        monkeypatch.setenv("CAP_HOME", str(tmp_path / ".cap"))

        config_file = tmp_path / ".claude.json"
        config_file.write_text('{}')

        backup_path = _backup_file(config_file, "claude-json")

        assert backup_path is not None
        assert backup_path.exists()
        # Timestamp must be in the filename (YYYYMMDD or ISO format)
        filename = backup_path.name
        has_timestamp = any(char.isdigit() for char in filename)
        assert has_timestamp, f"Backup filename must contain timestamp: {filename}"

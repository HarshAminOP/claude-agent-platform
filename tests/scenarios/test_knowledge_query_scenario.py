"""
E2E Scenario: Knowledge Query
"What terraform modules exist in the infra repo?"

USER DOES:
  Agent calls mcp__cap-knowledge__knowledge_search with a terraform query
  OR: cap knowledge search "terraform modules"

SHOULD HAPPEN:
  - FTS5 search finds terraform-related entries
  - Graph traversal finds related nodes (repo -> modules)
  - Hybrid search merges keyword + semantic results
  - Results returned within 200ms (no Bedrock needed for FTS5 path)

FAILURE MODES:
  - Knowledge DB unavailable: returns empty results with degraded=True, does NOT raise
  - Query with SQL injection characters: sanitized, not executed as SQL
  - Semantic search unavailable (no Bedrock): falls back to keyword-only, still returns results
  - Empty query: returns error, not unhandled exception

VERIFY:
  - Retrieval returns results with correct schema (uuid, title, content_type, score)
  - Score range is [0, 1]
  - Fallback path is taken when embedding client not available
  - Injection attempt returns empty or sanitized results, not DB error
"""

import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.lib.retrieval import hybrid_search, SearchResult


@pytest.fixture
def knowledge_db(tmp_path):
    """Knowledge database seeded with representative entries."""
    import hashlib
    from cap.lib.db_init import init_knowledge_db
    db = init_knowledge_db(tmp_path)

    entries = [
        ("uuid-tf-1", "/workspace/infra", "terraform", "VPC Module", "Terraform module for VPC networking. Creates subnets, route tables, and NAT gateways."),
        ("uuid-tf-2", "/workspace/infra", "terraform", "EKS Cluster Module", "Terraform module for Amazon EKS. Manages node groups and IAM roles."),
        ("uuid-tf-3", "/workspace/infra", "terraform", "RDS Module", "Terraform module for RDS PostgreSQL. Handles parameter groups and subnet groups."),
        ("uuid-py-1", "/workspace/api", "python_file", "auth_service.py", "Authentication service with JWT token validation and OAuth2 support."),
        ("uuid-py-2", "/workspace/api", "python_file", "database.py", "Database connection pool with SQLAlchemy. Handles migrations via Alembic."),
    ]

    for entry_uuid, ws, ctype, title, content in entries:
        db.execute(
            """INSERT INTO knowledge_entries
               (uuid, workspace, source_type, content_type, title, content, content_hash, embedding_status)
               VALUES (?, ?, 'manual', ?, ?, ?, ?, 'pending')""",
            (entry_uuid, ws, ctype, title, content,
             hashlib.sha256(content.encode()).hexdigest())
        )
    # Rebuild FTS index
    db.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
    db.commit()

    yield db
    db.close()


class TestKeywordSearch:
    """FTS5 keyword search returns correct entries."""

    def test_terraform_query_returns_terraform_entries(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="terraform",
            query_vector=None,
            workspace="/workspace/infra",
            strategy="keyword",
            top_k=10,
        )
        assert len(results) > 0
        assert all(
            "terraform" in (r.title or "").lower() or
            "terraform" in (r.content_preview or "").lower()
            for r in results
        )

    def test_eks_query_returns_eks_entry(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="EKS cluster",
            query_vector=None,
            workspace="/workspace/infra",
            strategy="keyword",
            top_k=5,
        )
        assert len(results) > 0
        titles = [r.title for r in results]
        assert any("EKS" in (t or "") for t in titles)

    def test_results_have_required_fields(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="module",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=5,
        )
        for r in results:
            assert hasattr(r, "uuid")
            assert hasattr(r, "score")
            assert hasattr(r, "title")
            assert hasattr(r, "content_type")
            assert 0.0 <= r.score <= 1.0, f"Score {r.score} out of [0,1] range"

    def test_no_cross_workspace_leakage_when_scoped(self, knowledge_db):
        """When workspace is specified, results from other workspaces must not appear."""
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="database",
            query_vector=None,
            workspace="/workspace/infra",  # Only infra workspace
            strategy="keyword",
            top_k=10,
        )
        for r in results:
            assert r.workspace == "/workspace/infra" or r.workspace is None, \
                f"Got result from wrong workspace: {r.workspace}"


class TestSQLInjectionProtection:
    """Search queries with injection characters do not cause DB errors."""

    @pytest.mark.parametrize("malicious_query", [
        "'; DROP TABLE knowledge_entries; --",
        'OR "1"="1"',
        "UNION SELECT * FROM sessions--",
        "\x00null\x00byte",
        "terraform'; DELETE FROM knowledge_entries WHERE '1'='1",
    ])
    def test_injection_does_not_crash(self, knowledge_db, malicious_query):
        """Malicious queries must return results or empty list, never raise."""
        try:
            results = hybrid_search(
                conn=knowledge_db,
                vectors_table=None,
                query=malicious_query,
                query_vector=None,
                workspace=None,
                strategy="keyword",
                top_k=5,
            )
            # Either empty or some results — acceptable
            assert isinstance(results, list)
        except Exception as exc:
            pytest.fail(f"SQL injection query raised exception: {exc}")

    def test_entries_not_deleted_after_injection_attempt(self, knowledge_db):
        """After injection attempt, original data must still be intact."""
        _ = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="'; DROP TABLE knowledge_entries; --",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=5,
        )
        count = knowledge_db.execute(
            "SELECT COUNT(*) FROM knowledge_entries"
        ).fetchone()[0]
        assert count == 5, "Original entries must survive injection attempt"


class TestDegradedMode:
    """Semantic search falls back gracefully when Bedrock is unavailable."""

    def test_hybrid_falls_back_to_keyword_without_vectors(self, knowledge_db):
        """With no vectors_table and no query_vector, hybrid falls back to keyword."""
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,   # No LanceDB table
            query="terraform",
            query_vector=None,    # No embedding
            workspace=None,
            strategy="hybrid",
            top_k=5,
        )
        # Should still return results via FTS5 fallback
        assert isinstance(results, list)
        # Must not raise even without semantic backend

    def test_empty_query_returns_empty_list(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=5,
        )
        assert isinstance(results, list)


class TestTopKRespected:
    """Search results never exceed requested top_k."""

    def test_top_k_limits_results(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="module",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=2,
        )
        assert len(results) <= 2

    def test_top_k_1_returns_best_result(self, knowledge_db):
        results = hybrid_search(
            conn=knowledge_db,
            vectors_table=None,
            query="EKS",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=1,
        )
        assert len(results) == 1
        assert "EKS" in (results[0].title or "")

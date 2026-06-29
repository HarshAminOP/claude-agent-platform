"""Tests for blast radius assessment module."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database, init_knowledge_db
from cap.lib.blast_radius import (
    assess_blast_radius, BlastRadiusAssessment, ImpactZone,
    _determine_scope, _determine_risk, _generate_recommendations,
)


@pytest.fixture
def knowledge_db():
    tmp = tempfile.mkdtemp()
    conn = init_knowledge_db(Path(tmp))
    yield conn
    conn.close()


def _add_node(db, entity_name, entity_type, workspace="test-ws"):
    import uuid
    node_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace) VALUES (?, ?, ?, ?)",
        (node_id, entity_name, entity_type, workspace),
    )
    db.commit()
    return node_id


def _add_edge(db, source_id, target_id, predicate, workspace="test-ws"):
    db.execute(
        "INSERT INTO knowledge_graph_edges (source_id, target_id, predicate, workspace) VALUES (?, ?, ?, ?)",
        (source_id, target_id, predicate, workspace),
    )
    db.commit()


def test_no_graph_data(knowledge_db):
    result = assess_blast_radius(knowledge_db, "unknown-service")
    assert result.target == "unknown-service"
    assert result.total_services_affected == 0
    assert result.risk_level == "low"
    assert result.requires_approval is False


def test_direct_dependents(knowledge_db):
    target_id = _add_node(knowledge_db, "auth-service", "service")
    dep_id = _add_node(knowledge_db, "api-gateway", "service")
    _add_edge(knowledge_db, dep_id, target_id, "depends_on")

    result = assess_blast_radius(knowledge_db, "auth-service")
    assert result.total_services_affected >= 1
    assert any(z.impact_type == "direct" for z in result.impact_zones)


def test_transitive_dependents(knowledge_db):
    target_id = _add_node(knowledge_db, "database-lib", "service")
    direct_id = _add_node(knowledge_db, "user-service", "service")
    transitive_id = _add_node(knowledge_db, "frontend-app", "service")

    _add_edge(knowledge_db, direct_id, target_id, "depends_on")
    _add_edge(knowledge_db, transitive_id, direct_id, "depends_on")

    result = assess_blast_radius(knowledge_db, "database-lib")
    assert result.total_services_affected >= 2


def test_team_ownership_detection(knowledge_db):
    target_id = _add_node(knowledge_db, "payments", "service")
    team_id = _add_node(knowledge_db, "team-billing", "team")
    _add_edge(knowledge_db, team_id, target_id, "owns")

    result = assess_blast_radius(knowledge_db, "payments")
    assert result.total_teams_affected >= 1


def test_requires_approval_multi_team(knowledge_db):
    target_id = _add_node(knowledge_db, "shared-lib", "service")
    team1_id = _add_node(knowledge_db, "team-alpha", "team")
    team2_id = _add_node(knowledge_db, "team-beta", "team")
    _add_edge(knowledge_db, team1_id, target_id, "owns")
    _add_edge(knowledge_db, team2_id, target_id, "maintains")

    result = assess_blast_radius(knowledge_db, "shared-lib")
    assert result.requires_approval is True
    assert "team" in result.approval_reason.lower()


def test_scope_determination():
    assert _determine_scope(0, 0) == "single_file"
    assert _determine_scope(0, 1) == "single_file"
    assert _determine_scope(1, 5) == "module"
    assert _determine_scope(3, 10) == "service"
    assert _determine_scope(5, 20) == "cross_service"


def test_risk_determination():
    assert _determine_risk("single_file", "modify", 1) == "low"
    assert _determine_risk("service", "delete", 2) == "critical"
    assert _determine_risk("module", "modify", 1) == "medium"
    assert _determine_risk("cross_service", "modify", 1) == "high"


def test_recommendations_for_high_risk():
    recs = _generate_recommendations("cross_service", "high", "modify", 5)
    assert any("integration test" in r.lower() for r in recs)
    assert any("notify" in r.lower() for r in recs)


def test_recommendations_for_delete():
    recs = _generate_recommendations("module", "medium", "delete", 1)
    assert any("runtime references" in r.lower() for r in recs)


def test_assessment_serialization(knowledge_db):
    result = assess_blast_radius(knowledge_db, "test-target", change_type="delete")
    d = result.to_dict()
    assert d["target"] == "test-target"
    assert d["change_type"] == "delete"
    assert isinstance(d["impact_zones"], list)
    assert isinstance(d["recommendations"], list)

"""Tests for dashboard TUI module."""
import pytest
import sys
import tempfile
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.dashboard import Dashboard


@pytest.fixture
def data_dir():
    tmp = Path(tempfile.mkdtemp())

    # Create minimal platform.db
    pdb = sqlite3.connect(str(tmp / "platform.db"))
    pdb.executescript("""
        CREATE TABLE workflows (
            id TEXT PRIMARY KEY, name TEXT, status TEXT, budget_tokens INTEGER,
            tokens_used INTEGER, agents_spawned INTEGER, max_agents INTEGER,
            started_at TEXT, completed_at TEXT, error TEXT, metadata TEXT DEFAULT '{}',
            killed INTEGER DEFAULT 0
        );
        CREATE TABLE workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT,
            event_type TEXT, agent_id TEXT, phase TEXT, message TEXT,
            tokens_delta INTEGER DEFAULT 0, timestamp TEXT
        );
        CREATE TABLE budget_ledger (
            id INTEGER PRIMARY KEY, workspace TEXT, period TEXT,
            model TEXT, total_cost_usd REAL DEFAULT 0.0,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            embedding_tokens INTEGER DEFAULT 0
        );
    """)
    pdb.execute(
        "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL, NULL, '{}', 0)",
        ("wf-1", "deploy-prod", "running", 500000, 120000, 3, 10),
    )
    pdb.execute(
        "INSERT INTO workflow_events (workflow_id, event_type, agent_id, message, timestamp) VALUES (?, ?, ?, ?, datetime('now'))",
        ("wf-1", "agent_start", "dev-1", "Starting deployment"),
    )
    pdb.commit()
    pdb.close()

    # Create minimal backlog.db
    bdb = sqlite3.connect(str(tmp / "backlog.db"))
    bdb.executescript("""
        CREATE TABLE backlog_tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT, priority TEXT,
            assigned_to TEXT, created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT, description TEXT, created_by TEXT,
            workflow_id TEXT, parent_id TEXT, depends_on TEXT DEFAULT '[]',
            labels TEXT DEFAULT '[]', acceptance_criteria TEXT DEFAULT '[]',
            output TEXT, error TEXT, started_at TEXT, completed_at TEXT
        );
        CREATE TABLE decision_cards (
            id TEXT PRIMARY KEY, title TEXT, status TEXT DEFAULT 'pending',
            context TEXT, options TEXT, recommendation_index INTEGER DEFAULT -1,
            recommendation_rationale TEXT, deadline TEXT, domain TEXT,
            agent_id TEXT, workflow_id TEXT, chosen_option INTEGER DEFAULT -1,
            po_notes TEXT, created_at TEXT DEFAULT (datetime('now')), resolved_at TEXT
        );
        CREATE TABLE conflicts (
            id TEXT PRIMARY KEY, title TEXT, severity TEXT, status TEXT,
            workflow_id TEXT, phase TEXT, side_a TEXT, side_b TEXT,
            resolution TEXT, resolution_notes TEXT, resolved_by TEXT,
            created_at TEXT DEFAULT (datetime('now')), resolved_at TEXT
        );
    """)
    bdb.execute("INSERT INTO backlog_tasks (id, title, status, priority) VALUES ('t1', 'Task A', 'done', 'medium')")
    bdb.execute("INSERT INTO backlog_tasks (id, title, status, priority) VALUES ('t2', 'Task B', 'in_progress', 'high')")
    bdb.execute("INSERT INTO decision_cards (id, title, status) VALUES ('d1', 'Choose strategy', 'pending')")
    bdb.execute("INSERT INTO conflicts (id, title, severity, status) VALUES ('c1', 'IAM conflict', 'blocking', 'escalated')")
    bdb.commit()
    bdb.close()

    yield tmp


def test_dashboard_render_once(data_dir):
    dash = Dashboard(data_dir)
    layout = dash.render_once()
    assert layout is not None


def test_dashboard_workflows_panel(data_dir):
    dash = Dashboard(data_dir)
    panel = dash._render_workflows()
    assert panel is not None
    assert panel.title == "Active Work"


def test_dashboard_sidebar_panel(data_dir):
    dash = Dashboard(data_dir)
    panel = dash._render_sidebar()
    assert panel is not None
    assert panel.title == "Status"


def test_dashboard_no_data():
    tmp = Path(tempfile.mkdtemp())
    dash = Dashboard(tmp)
    layout = dash.render_once()
    assert layout is not None


def test_dashboard_stop():
    tmp = Path(tempfile.mkdtemp())
    dash = Dashboard(tmp)
    dash._running = True
    dash.stop()
    assert dash._running is False

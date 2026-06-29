"""Tests for decision cards module."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database
from cap.lib.decision_cards import (
    DecisionCard, DecisionStatus, RiskLevel, Option,
    init_decision_cards_table, save_card, get_card, resolve_card, list_cards,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    conn = create_database(Path(tmp) / "test.db")
    init_decision_cards_table(conn)
    yield conn
    conn.close()


def _make_card(**kwargs) -> DecisionCard:
    defaults = {
        "title": "Deploy strategy",
        "context": "Need to pick between blue-green and canary",
        "options": [
            Option(label="Blue-green", description="Full swap", risk=RiskLevel.low),
            Option(label="Canary", description="Gradual", risk=RiskLevel.medium, recommended=True),
        ],
        "recommendation_index": 1,
        "recommendation_rationale": "Less risk of full outage",
        "domain": "infra",
        "agent_id": "devops-1",
    }
    defaults.update(kwargs)
    return DecisionCard(**defaults)


def test_save_and_get(db):
    card = _make_card()
    save_card(db, card)
    retrieved = get_card(db, card.id)
    assert retrieved is not None
    assert retrieved.title == "Deploy strategy"
    assert len(retrieved.options) == 2
    assert retrieved.options[1].recommended is True
    assert retrieved.status == DecisionStatus.pending


def test_resolve_card(db):
    card = _make_card()
    save_card(db, card)
    resolved = resolve_card(db, card.id, chosen_option=0, po_notes="Go with blue-green")
    assert resolved.status == DecisionStatus.approved
    assert resolved.chosen_option == 0
    assert resolved.po_notes == "Go with blue-green"
    assert resolved.resolved_at is not None


def test_resolve_with_reject(db):
    card = _make_card()
    save_card(db, card)
    resolved = resolve_card(db, card.id, chosen_option=-1, status=DecisionStatus.rejected, po_notes="Neither works")
    assert resolved.status == DecisionStatus.rejected


def test_list_cards_all(db):
    save_card(db, _make_card(title="A"))
    save_card(db, _make_card(title="B"))
    cards = list_cards(db)
    assert len(cards) == 2


def test_list_cards_by_status(db):
    card = _make_card(title="Pending one")
    save_card(db, card)
    save_card(db, _make_card(title="Another pending"))
    resolve_card(db, card.id, chosen_option=0)

    pending = list_cards(db, status=DecisionStatus.pending)
    assert len(pending) == 1
    assert pending[0].title == "Another pending"

    approved = list_cards(db, status=DecisionStatus.approved)
    assert len(approved) == 1
    assert approved[0].title == "Pending one"


def test_list_cards_by_workflow(db):
    save_card(db, _make_card(title="W1", workflow_id="wf-abc"))
    save_card(db, _make_card(title="W2", workflow_id="wf-xyz"))

    results = list_cards(db, workflow_id="wf-abc")
    assert len(results) == 1
    assert results[0].title == "W1"


def test_option_tradeoffs(db):
    card = _make_card(options=[
        Option(
            label="Option A",
            description="Fast",
            tradeoffs={"speed": "fast", "cost": "high"},
            risk=RiskLevel.high,
            estimated_effort="2 days",
        ),
    ])
    save_card(db, card)
    retrieved = get_card(db, card.id)
    assert retrieved.options[0].tradeoffs == {"speed": "fast", "cost": "high"}
    assert retrieved.options[0].risk == RiskLevel.high
    assert retrieved.options[0].estimated_effort == "2 days"


def test_card_round_trip_serialization():
    card = _make_card()
    d = card.to_dict()
    restored = DecisionCard.from_dict(d)
    assert restored.title == card.title
    assert len(restored.options) == len(card.options)
    assert restored.recommendation_index == card.recommendation_index

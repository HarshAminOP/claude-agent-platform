"""Unit tests for cap.harness.embed_router.EmbeddingRouter.

All tests are fully offline — no Bedrock calls, no LanceDB on disk.
PatternEmbedder is patched at ``cap.harness.embed_router.PatternEmbedder``
(module-level import), and _get_conn at ``cap.harness.agentdb._get_conn``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.embed_router import EmbeddingRouter, _DEFAULT_MODELS, _MODEL_COST_ORDER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pe(is_avail: bool = True, hits: list[dict] | None = None):
    """Return a mock PatternEmbedder instance."""
    pe = MagicMock()
    type(pe).is_available = PropertyMock(return_value=is_avail)
    pe.search_similar.return_value = hits or []
    return pe


def _make_conn_with_rows(rows: list[tuple]) -> sqlite3.Connection:
    """Return a real in-memory SQLite connection seeded with pattern rows.

    rows: (id, agent_type, success, cost_usd, model)
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE patterns (
               id TEXT PRIMARY KEY,
               agent_type TEXT,
               success INTEGER,
               cost_usd REAL,
               model TEXT,
               prompt_hash TEXT,
               prompt_summary TEXT,
               task_type TEXT,
               duration_ms INTEGER,
               output_summary TEXT,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.executemany(
        "INSERT INTO patterns (id, agent_type, success, cost_usd, model) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


def _route_with_mocks(hits, db_rows):
    """Run EmbeddingRouter.route() with PatternEmbedder and _get_conn mocked."""
    pe = _make_pe(is_avail=True, hits=hits)
    conn = _make_conn_with_rows(db_rows)

    with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
         patch("cap.harness.agentdb._get_conn", return_value=conn), \
         patch("cap.harness.embed_router._get_conn", return_value=conn):
        er = EmbeddingRouter()
        return er.route("some task description")


# ---------------------------------------------------------------------------
# EmbeddingRouter.route — embedder unavailable / insufficient data
# ---------------------------------------------------------------------------

class TestRouteEmbedderUnavailable:
    def test_returns_none_when_embedder_unavailable(self):
        pe = _make_pe(is_avail=False)
        with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe):
            er = EmbeddingRouter()
            result = er.route("deploy the auth service")
        assert result is None

    def test_returns_none_when_fewer_than_5_hits(self):
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(3)]
        pe = _make_pe(is_avail=True, hits=hits)
        conn = _make_conn_with_rows([])

        with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
             patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            result = er.route("some task")
        assert result is None

    def test_returns_none_when_PatternEmbedder_is_None(self):
        with patch("cap.harness.embed_router.PatternEmbedder", None):
            er = EmbeddingRouter()
            result = er.route("some task")
        assert result is None

    def test_returns_none_on_exception(self):
        with patch("cap.harness.embed_router.PatternEmbedder", side_effect=RuntimeError("boom")):
            er = EmbeddingRouter()
            result = er.route("some task")
        assert result is None


# ---------------------------------------------------------------------------
# EmbeddingRouter.route — scoring logic
# ---------------------------------------------------------------------------

class TestRouteScoring:
    def test_returns_dict_with_expected_keys(self):
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(5)]
        db_rows = [(f"p{i}", "dev", 1, 0.001, "sonnet") for i in range(5)]
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        for key in ("recommended_agent_type", "confidence", "model",
                    "reasoning", "alternatives", "based_on_patterns"):
            assert key in result

    def test_selects_highest_scored_agent(self):
        # dev: high similarity + 100% success  vs  sre: low similarity + 0% success
        hits = (
            [{"pattern_id": f"dev{i}", "score": 0.9, "text": "x"} for i in range(5)]
            + [{"pattern_id": f"sre{i}", "score": 0.4, "text": "x"} for i in range(5)]
        )
        db_rows = (
            [(f"dev{i}", "dev", 1, 0.001, "sonnet") for i in range(5)]
            + [(f"sre{i}", "sre", 0, 0.001, "sonnet") for i in range(5)]
        )
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert result["recommended_agent_type"] == "dev"

    def test_cost_efficiency_favours_cheap_agent(self):
        # Both agents: same similarity (0.7) + 100% success; devops is cheaper
        hits = (
            [{"pattern_id": f"dv{i}", "score": 0.7, "text": "x"} for i in range(5)]
            + [{"pattern_id": f"sec{i}", "score": 0.7, "text": "x"} for i in range(5)]
        )
        db_rows = (
            [(f"dv{i}", "devops", 1, 0.001, "sonnet") for i in range(5)]   # cheap
            + [(f"sec{i}", "security", 1, 0.09, "opus") for i in range(5)]  # expensive
        )
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert result["recommended_agent_type"] == "devops"

    def test_alternatives_contains_runners_up(self):
        hits = (
            [{"pattern_id": f"d{i}", "score": 0.9, "text": "x"} for i in range(5)]
            + [{"pattern_id": f"s{i}", "score": 0.7, "text": "x"} for i in range(5)]
            + [{"pattern_id": f"t{i}", "score": 0.5, "text": "x"} for i in range(5)]
        )
        db_rows = (
            [(f"d{i}", "dev", 1, 0.001, "sonnet") for i in range(5)]
            + [(f"s{i}", "sre", 1, 0.001, "sonnet") for i in range(5)]
            + [(f"t{i}", "test", 1, 0.001, "sonnet") for i in range(5)]
        )
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert len(result["alternatives"]) == 2
        alt_types = {a["agent_type"] for a in result["alternatives"]}
        assert result["recommended_agent_type"] not in alt_types

    def test_based_on_patterns_equals_hit_count(self):
        hits = [{"pattern_id": f"p{i}", "score": 0.6, "text": "x"} for i in range(7)]
        db_rows = [(f"p{i}", "dev", 1, 0.001, "sonnet") for i in range(7)]
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert result["based_on_patterns"] == 7

    def test_returns_none_when_all_hits_missing_from_db(self):
        hits = [{"pattern_id": f"ghost{i}", "score": 0.8, "text": "x"} for i in range(5)]
        pe = _make_pe(is_avail=True, hits=hits)
        conn = _make_conn_with_rows([])  # empty DB — no matching rows

        with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
             patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            result = er.route("some task")
        assert result is None

    def test_score_formula_correctness(self):
        # avg_sim=0.8, success_rate=1.0, avg_cost=0.0
        # Expected score: 0.5*0.8 + 0.3*1.0 + 0.2*1.0 = 0.9
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(5)]
        db_rows = [(f"p{i}", "dev", 1, 0.0, "sonnet") for i in range(5)]
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert abs(result["confidence"] - 0.9) < 0.01

    def test_cost_efficiency_capped_at_zero_for_expensive_tasks(self):
        # cost_usd=0.20 -> cost_eff = 1-min(1, 0.20/0.10) = 0.0
        # score: 0.5*0.8 + 0.3*1.0 + 0.2*0.0 = 0.7
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(5)]
        db_rows = [(f"p{i}", "dev", 1, 0.20, "sonnet") for i in range(5)]
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert abs(result["confidence"] - 0.7) < 0.01

    def test_returns_none_when_get_conn_is_none(self):
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(5)]
        pe = _make_pe(is_avail=True, hits=hits)

        with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
             patch("cap.harness.embed_router._get_conn", None):
            er = EmbeddingRouter()
            result = er.route("some task")
        assert result is None


# ---------------------------------------------------------------------------
# EmbeddingRouter.recommend_model
# ---------------------------------------------------------------------------

class TestRecommendModel:
    def _conn_with_model_stats(self, rows: list[tuple]) -> sqlite3.Connection:
        """rows: (model, count, avg_success) — expands into individual pattern rows."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE patterns (
                   id TEXT PRIMARY KEY,
                   agent_type TEXT,
                   model TEXT,
                   success INTEGER,
                   cost_usd REAL
               )"""
        )
        for model, cnt, avg_success in rows:
            for i in range(cnt):
                success = 1 if i < round(avg_success * cnt) else 0
                conn.execute(
                    "INSERT INTO patterns (id, agent_type, model, success, cost_usd) VALUES (?,?,?,?,?)",
                    (f"{model}-{i}", "dev", model, success, 0.001),
                )
        conn.commit()
        return conn

    def test_returns_cheapest_model_with_80pct_success(self):
        conn = self._conn_with_model_stats([
            ("haiku", 5, 1.0), ("sonnet", 5, 1.0), ("opus", 5, 1.0)
        ])
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            assert er.recommend_model("dev") == "haiku"

    def test_skips_model_below_threshold(self):
        conn = self._conn_with_model_stats([("haiku", 5, 0.6), ("sonnet", 5, 0.9)])
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            assert er.recommend_model("dev") == "sonnet"

    def test_falls_back_to_default_when_no_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE patterns (id TEXT PRIMARY KEY, agent_type TEXT, model TEXT, success INTEGER)"
        )
        conn.commit()
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            model = er.recommend_model("security")
        assert model == _DEFAULT_MODELS["security"]

    def test_falls_back_to_sonnet_for_unknown_agent(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE patterns (id TEXT PRIMARY KEY, agent_type TEXT, model TEXT, success INTEGER)"
        )
        conn.commit()
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            model = er.recommend_model("unknown-agent-xyz")
        assert model == "sonnet"

    def test_falls_back_gracefully_on_exception(self):
        with patch("cap.harness.embed_router._get_conn", side_effect=Exception("db boom")):
            er = EmbeddingRouter()
            model = er.recommend_model("dev")
        assert model == _DEFAULT_MODELS["dev"]

    def test_returns_default_when_get_conn_is_none(self):
        with patch("cap.harness.embed_router._get_conn", None):
            er = EmbeddingRouter()
            model = er.recommend_model("docs")
        assert model == _DEFAULT_MODELS["docs"]

    def test_all_known_agents_have_default(self):
        for at in ["dev", "devops", "security", "code-review", "sre",
                   "test", "docs", "optimization", "aws-architect"]:
            assert at in _DEFAULT_MODELS


# ---------------------------------------------------------------------------
# hooks_route integration — embedding path takes precedence
# ---------------------------------------------------------------------------

class TestHooksRouteEmbeddingIntegration:
    """Verify hooks_route() respects the embedding router result.

    Because hooks_route does ``from cap.harness.embed_router import EmbeddingRouter``
    inside the function, we patch ``cap.harness.embed_router.EmbeddingRouter``
    so the local import picks up the mock.
    """

    _PATCH_TARGET = "cap.harness.embed_router.EmbeddingRouter"

    def _mock_er(self, route_return):
        mock_er_instance = MagicMock()
        mock_er_instance.route.return_value = route_return
        return MagicMock(return_value=mock_er_instance)

    def test_uses_embedding_when_confident(self, tmp_path):
        from cap.harness.hooks import hooks_route

        mock_er_class = self._mock_er({
            "recommended_agent_type": "devops",
            "confidence": 0.82,
            "model": "sonnet",
            "reasoning": "Based on 8 similar patterns, devops scored highest (0.82)",
            "alternatives": [],
            "based_on_patterns": 8,
        })

        with patch(self._PATCH_TARGET, mock_er_class):
            result = hooks_route("deploy to kubernetes", _db_path=tmp_path / "test.db")

        assert result["routing_method"] == "embedding"
        assert result["confidence"] == pytest.approx(0.82)
        assert result["recommended_model"] == "claude-sonnet-4-6"
        assert result["tier"] == "lightweight"

    def test_falls_back_to_keyword_when_route_returns_none(self, tmp_path):
        from cap.harness.hooks import hooks_route

        with patch(self._PATCH_TARGET, self._mock_er(None)):
            result = hooks_route("fix the login bug", _db_path=tmp_path / "test.db")

        assert result["routing_method"] == "keyword"

    def test_falls_back_when_confidence_below_threshold(self, tmp_path):
        from cap.harness.hooks import hooks_route

        with patch(self._PATCH_TARGET, self._mock_er({
            "recommended_agent_type": "dev",
            "confidence": 0.45,  # below 0.6 threshold
            "model": "haiku",
            "reasoning": "weak signal",
            "alternatives": [],
            "based_on_patterns": 5,
        })):
            result = hooks_route("fix the login bug", _db_path=tmp_path / "test.db")

        assert result["routing_method"] == "keyword"

    def test_still_returns_valid_dict_when_embed_router_raises(self, tmp_path):
        from cap.harness.hooks import hooks_route

        with patch(self._PATCH_TARGET, MagicMock(side_effect=ImportError("no module"))):
            result = hooks_route("some task", _db_path=tmp_path / "test.db")

        assert "recommended_model" in result
        assert "confidence" in result

    def test_haiku_maps_to_inline_tier(self, tmp_path):
        from cap.harness.hooks import hooks_route

        with patch(self._PATCH_TARGET, self._mock_er({
            "recommended_agent_type": "docs",
            "confidence": 0.75,
            "model": "haiku",
            "reasoning": "docs agent",
            "alternatives": [],
            "based_on_patterns": 6,
        })):
            result = hooks_route("write documentation", _db_path=tmp_path / "test.db")

        assert result["routing_method"] == "embedding"
        assert result["tier"] == "inline"
        assert result["recommended_model"] == "claude-haiku-4-5"

    def test_opus_maps_to_full_tier(self, tmp_path):
        from cap.harness.hooks import hooks_route

        with patch(self._PATCH_TARGET, self._mock_er({
            "recommended_agent_type": "security",
            "confidence": 0.90,
            "model": "opus",
            "reasoning": "security agent",
            "alternatives": [],
            "based_on_patterns": 10,
        })):
            result = hooks_route("audit IAM policies", _db_path=tmp_path / "test.db")

        assert result["routing_method"] == "embedding"
        assert result["tier"] == "full"
        assert result["recommended_model"] == "claude-opus-4-6"

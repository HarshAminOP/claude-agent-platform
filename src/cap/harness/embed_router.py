"""Embedding-based agent router for the CAP harness.

Routes tasks to the best agent type using cosine-similarity search over
historical execution patterns (Mixture-of-Experts equivalent).

Gracefully degrades: if Bedrock / LanceDB / the patterns table are
unavailable, ``route()`` returns None and the caller falls back to
keyword routing.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("cap.harness.embed_router")

# Module-level imports kept here so tests can patch them at a stable path.
# Both are guarded — if a dependency is missing, the router degrades gracefully.
try:
    from cap.harness.vector_patterns import PatternEmbedder as PatternEmbedder  # noqa: PLC0414
except Exception:  # pragma: no cover
    PatternEmbedder = None  # type: ignore[assignment,misc]

try:
    from cap.harness.agentdb import _get_conn as _get_conn  # noqa: PLC0414
except Exception:  # pragma: no cover
    _get_conn = None  # type: ignore[assignment]

# Cheapest-first order used by recommend_model()
_MODEL_COST_ORDER = ["haiku", "sonnet", "opus"]

_DEFAULT_MODELS: dict[str, str] = {
    "dev": "sonnet",
    "devops": "sonnet",
    "security": "opus",
    "code-review": "opus",
    "sre": "sonnet",
    "test": "sonnet",
    "docs": "haiku",
    "optimization": "haiku",
    "aws-architect": "opus",
}


class EmbeddingRouter:
    """Routes tasks to agent types using vector similarity against historical patterns.

    Usage::

        er = EmbeddingRouter()
        result = er.route("deploy the auth service to production")
        if result:
            print(result["recommended_agent_type"], result["confidence"])
    """

    def __init__(self) -> None:
        # Lazy: created on first call to route()
        self._embedder: Optional[object] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, task: str) -> Optional[dict]:
        """Return the best agent type for *task* based on past patterns.

        Algorithm:
        1. Embed *task* and find the top-20 most similar historical patterns.
        2. For each hit, look up ``agent_type``, ``success``, and ``cost_usd``
           from the patterns table in SQLite.
        3. Group by ``agent_type``; compute a weighted score per type::

               score = 0.5 * avg_similarity
                     + 0.3 * success_rate
                     + 0.2 * cost_efficiency

           where ``cost_efficiency = 1.0 - min(1.0, avg_cost / 0.10)``.
        4. Return the highest-scoring type.

        Returns:
            Dict with keys ``recommended_agent_type``, ``confidence``,
            ``model``, ``reasoning``, ``alternatives``, and
            ``based_on_patterns``; or *None* if fewer than 5 patterns were
            found or the embedder is unavailable.
        """
        try:
            if PatternEmbedder is None:
                return None

            if self._embedder is None:
                self._embedder = PatternEmbedder()

            pe = self._embedder
            if not pe.is_available:
                logger.debug("EmbeddingRouter: PatternEmbedder unavailable")
                return None

            results = pe.search_similar(task, limit=20, min_score=0.3)
            if len(results) < 5:
                logger.debug(
                    "EmbeddingRouter: only %d similar patterns (need >=5), skipping",
                    len(results),
                )
                return None

            # ----------------------------------------------------------
            # Enrich hits with pattern metadata from SQLite
            # ----------------------------------------------------------
            if _get_conn is None:
                return None

            conn = _get_conn()
            try:
                type_data: dict[str, dict] = {}
                for r in results:
                    row = conn.execute(
                        "SELECT agent_type, success, cost_usd FROM patterns WHERE id = ?",
                        (r["pattern_id"],),
                    ).fetchone()
                    if not row:
                        continue
                    at = row[0] if not hasattr(row, "__getitem__") else row["agent_type"]
                    s = row[1] if not hasattr(row, "__getitem__") else row["success"]
                    c = row[2] if not hasattr(row, "__getitem__") else row["cost_usd"]
                    # Support both sqlite3.Row and plain tuple
                    try:
                        at = row["agent_type"]
                        s = row["success"]
                        c = row["cost_usd"]
                    except (TypeError, IndexError, KeyError):
                        at, s, c = row[0], row[1], row[2]

                    if not at:
                        continue
                    if at not in type_data:
                        type_data[at] = {"sims": [], "succs": [], "costs": []}
                    type_data[at]["sims"].append(r["score"])
                    type_data[at]["succs"].append(int(s) if s is not None else 1)
                    type_data[at]["costs"].append(float(c) if c is not None else 0.0)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            if not type_data:
                return None

            # ----------------------------------------------------------
            # Score each agent type
            # ----------------------------------------------------------
            scored: list[tuple[str, float, float]] = []  # (agent_type, score, avg_cost)
            for at, data in type_data.items():
                n = len(data["sims"])
                avg_sim = sum(data["sims"]) / n
                success_rate = sum(data["succs"]) / n
                avg_cost = sum(data["costs"]) / n
                cost_eff = 1.0 - min(1.0, avg_cost / 0.10)
                score = 0.5 * avg_sim + 0.3 * success_rate + 0.2 * cost_eff
                scored.append((at, round(score, 4), avg_cost))

            scored.sort(key=lambda x: x[1], reverse=True)
            best_at, best_score, _ = scored[0]

            return {
                "recommended_agent_type": best_at,
                "confidence": best_score,
                "model": self.recommend_model(best_at, task),
                "reasoning": (
                    f"Based on {len(results)} similar patterns, {best_at} scored "
                    f"highest ({best_score:.2f})"
                ),
                "alternatives": [
                    {"agent_type": s[0], "score": s[1]}
                    for s in scored[1:3]
                ],
                "based_on_patterns": len(results),
            }

        except Exception as exc:
            logger.debug("EmbeddingRouter.route failed: %s", exc)
            return None

    def recommend_model(self, agent_type: str, task: str = "") -> str:  # noqa: ARG002
        """Return the cheapest model with >=80% success rate for *agent_type*.

        Falls back to ``_DEFAULT_MODELS[agent_type]`` (or ``"sonnet"``) when
        there is insufficient history.

        Args:
            agent_type: The target agent type (e.g. ``"dev"``).
            task:       The task description (reserved for future context use).

        Returns:
            Model short name such as ``"haiku"``, ``"sonnet"``, or ``"opus"``.
        """
        try:
            if _get_conn is None:
                return _DEFAULT_MODELS.get(agent_type, "sonnet")

            conn = _get_conn()
            try:
                rows = conn.execute(
                    """SELECT model, COUNT(*) AS cnt, AVG(success) AS sr
                       FROM patterns
                       WHERE agent_type = ?
                       GROUP BY model
                       HAVING cnt >= 3""",
                    (agent_type,),
                ).fetchall()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            if not rows:
                return _DEFAULT_MODELS.get(agent_type, "sonnet")

            # Build a lookup: model_name -> success_rate
            model_sr: dict[str, float] = {}
            for row in rows:
                try:
                    m, sr = row["model"], float(row["sr"] or 0.0)
                except (TypeError, KeyError):
                    m, sr = row[0], float(row[2] or 0.0)
                if m:
                    model_sr[m] = sr

            # Pick the cheapest model that meets the 80% threshold
            for model in _MODEL_COST_ORDER:
                if model_sr.get(model, 0.0) >= 0.8:
                    return model

            # No model meets the threshold — return the most-used one
            try:
                best_row = max(rows, key=lambda r: r[1] if not hasattr(r, "__getitem__") else r["cnt"])
                return (
                    best_row["model"]
                    if hasattr(best_row, "__getitem__")
                    else best_row[0]
                ) or _DEFAULT_MODELS.get(agent_type, "sonnet")
            except Exception:
                return _DEFAULT_MODELS.get(agent_type, "sonnet")

        except Exception as exc:
            logger.debug("EmbeddingRouter.recommend_model failed: %s", exc)
            return _DEFAULT_MODELS.get(agent_type, "sonnet")

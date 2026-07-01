"""Hook automation tools for the CAP harness MCP server.

Provides intelligent routing, learning, and feedback hooks that fire
around agent execution.  Each public function maps directly to an MCP
tool registered in harness_server.py.

Storage: two extra tables in platform.db (patterns, trajectories).
All I/O is through the shared PLATFORM_DB_PATH from agent_store; the
tables are created lazily on first use.

All functions are gracefully degraded: if the DB or a dependency is
unavailable the call still returns a valid dict — never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.hooks")

# ---------------------------------------------------------------------------
# DB helpers — reuse the same platform.db as agent_store
# ---------------------------------------------------------------------------

try:
    from cap.harness.agent_store import PLATFORM_DB_PATH
except ImportError:
    PLATFORM_DB_PATH = Path.home() / ".claude-platform" / "data" / "platform.db"

_DDL = """
CREATE TABLE IF NOT EXISTS patterns (
    id TEXT PRIMARY KEY,
    task_type TEXT,
    prompt_hash TEXT,
    prompt_summary TEXT,
    model TEXT,
    agent_type TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    success INTEGER DEFAULT 1,
    output_summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trajectories (
    id TEXT PRIMARY KEY,
    trajectory_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    agent_id TEXT,
    action TEXT,
    result TEXT,
    cost_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patterns_prompt_hash ON patterns(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_patterns_task_type   ON patterns(task_type);
CREATE INDEX IF NOT EXISTS idx_trajectories_tid     ON trajectories(trajectory_id);
"""


def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open platform.db, ensure hooks tables exist, return connection."""
    path = db_path or PLATFORM_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN embedding_id TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    return conn


def _prompt_hash(text: str) -> str:
    """Stable 16-char hex hash of normalized prompt text."""
    normalized = text.strip().lower()[:200]
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Trust helpers — thin wrappers over learning.engine
# ---------------------------------------------------------------------------

def _get_trust(agent_id: str, conn: sqlite3.Connection) -> float:
    """Read trust score from trust_levels; return 0.5 if absent."""
    try:
        row = conn.execute(
            "SELECT trust_score FROM trust_levels WHERE agent_type = ? AND action_type = 'general'",
            (agent_id,),
        ).fetchone()
        return float(row[0]) if row else 0.5
    except sqlite3.OperationalError:
        return 0.5


def _update_trust(agent_id: str, delta: float, conn: sqlite3.Connection) -> float:
    """
    Adjust trust for agent_id by delta, clamped to [0.0, 1.0].
    Creates the row if absent with a 0.5 base.
    """
    try:
        row = conn.execute(
            "SELECT trust_score FROM trust_levels WHERE agent_type = ? AND action_type = 'general'",
            (agent_id,),
        ).fetchone()
        current = float(row[0]) if row else 0.5
        new_score = max(0.0, min(1.0, current + delta))

        if row:
            conn.execute(
                "UPDATE trust_levels SET trust_score = ?, last_updated = ? "
                "WHERE agent_type = ? AND action_type = 'general'",
                (new_score, time.time(), agent_id),
            )
        else:
            conn.execute(
                """INSERT INTO trust_levels
                   (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
                   VALUES (?, 'general', ?, ?, ?, ?)""",
                (agent_id, new_score, 1 if delta > 0 else 0, 0 if delta > 0 else 1, time.time()),
            )
        conn.commit()
        return new_score
    except sqlite3.OperationalError:
        return 0.5


# ---------------------------------------------------------------------------
# Public API — each function becomes one MCP tool
# ---------------------------------------------------------------------------


def hooks_route(
    task_description: str,
    agent_type: Optional[str] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """
    Return model recommendation for a task.

    Uses router.route() to get the complexity tier, then searches
    past patterns for the cheapest successful model on similar prompts.

    Returns:
        recommended_model, tier, confidence, reason, similar_task_cost
    """
    _default = {
        "recommended_model": "claude-sonnet-4-6",
        "tier": "lightweight",
        "confidence": 0.5,
        "reason": "default",
        "similar_task_cost": None,
    }

    try:
        import sqlite3 as _sqlite3
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("hooks_route: db unavailable: %s", exc)
        return _default

    try:
        # --- 1. Embedding-based routing (primary path) ---
        try:
            from cap.harness.embed_router import EmbeddingRouter, _DEFAULT_MODELS

            er = EmbeddingRouter()
            embed_result = er.route(task_description)
            if embed_result and embed_result.get("confidence", 0) >= 0.6:
                # Map short model name to full Claude model ID
                _short_to_full = {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                    "opus": "claude-opus-4-6",
                }
                short_model = embed_result.get("model", "sonnet")
                full_model = _short_to_full.get(short_model, "claude-sonnet-4-6")

                # Derive tier from model
                _model_tier_map = {
                    "haiku": "inline",
                    "sonnet": "lightweight",
                    "opus": "full",
                }
                tier = _model_tier_map.get(short_model, "lightweight")

                return {
                    "recommended_model": full_model,
                    "tier": tier,
                    "confidence": embed_result["confidence"],
                    "reason": embed_result["reasoning"],
                    "similar_task_cost": None,
                    "routing_method": "embedding",
                }
        except Exception as exc:
            logger.debug("hooks_route: embedding router unavailable: %s", exc)

        # --- 2. Tier from router (keyword fallback) ---
        tier = "lightweight"
        try:
            from cap.orchestration.router import route, Tier
            # router.route() needs a routing_decisions table; use the same DB
            decision = route(task_description, conn)
            tier = decision.tier.value  # 'inline' | 'lightweight' | 'full'
        except Exception as exc:
            logger.debug("hooks_route: router unavailable: %s", exc)

        # Map tier -> default model
        tier_model_map = {
            "inline": "claude-haiku-4-5",
            "lightweight": "claude-sonnet-4-6",
            "full": "claude-opus-4-6",
        }
        recommended_model = tier_model_map.get(tier, "claude-sonnet-4-6")

        # --- similarity search in patterns ---
        phash = _prompt_hash(task_description)
        similar_row = conn.execute(
            """SELECT model, cost_usd FROM patterns
               WHERE prompt_hash = ? AND success = 1
               ORDER BY cost_usd ASC
               LIMIT 1""",
            (phash,),
        ).fetchone()

        similar_task_cost: Optional[float] = None
        reason = f"tier={tier} keyword routing"
        confidence = 0.65

        if similar_row:
            past_model = similar_row[0]
            similar_task_cost = similar_row[1]
            recommended_model = past_model
            confidence = 0.85
            reason = f"past similar task succeeded with {past_model} at ${similar_task_cost:.5f}"

        return {
            "recommended_model": recommended_model,
            "tier": tier,
            "confidence": confidence,
            "reason": reason,
            "similar_task_cost": similar_task_cost,
            "routing_method": "keyword",
        }
    except Exception as exc:
        logger.warning("hooks_route: unexpected error: %s", exc)
        return _default
    finally:
        try:
            conn.close()
        except Exception:
            pass


def hooks_pre_task(
    agent_id: str,
    prompt: str,
    _db_path: Optional[Path] = None,
) -> dict:
    """
    Called BEFORE agent_execute.

    Searches knowledge base and patterns table for relevant context and
    similar successful prompts.  Never blocks — returns empty context on
    any failure.

    Returns:
        context, similar_patterns, suggested_system_prompt
    """
    empty = {
        "context": "",
        "similar_patterns": [],
        "suggested_system_prompt": "",
    }

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("hooks_pre_task: db unavailable: %s", exc)
        return empty

    try:
        phash = _prompt_hash(prompt)

        # Search patterns for similar successful prompts (exact hash match first,
        # then prefix match on agent_type as a lighter semantic approximation).
        rows = conn.execute(
            """SELECT prompt_summary, model, output_summary, cost_usd
               FROM patterns
               WHERE (prompt_hash = ? OR agent_type = ?) AND success = 1
               ORDER BY created_at DESC
               LIMIT 5""",
            (phash, agent_id),
        ).fetchall()

        similar_patterns = [
            {
                "prompt_summary": r[0],
                "model": r[1],
                "output_summary": r[2],
                "cost_usd": r[3],
            }
            for r in rows
        ]

        # Build a lightweight KB context snippet from top match output
        context_parts: list[str] = []
        if similar_patterns and similar_patterns[0].get("output_summary"):
            context_parts.append(f"Prior similar task output: {similar_patterns[0]['output_summary'][:400]}")

        # Try knowledge search (optional dependency)
        try:
            from cap.memory.search import search_knowledge  # type: ignore
            kb_hits = search_knowledge(prompt, limit=3)
            for hit in kb_hits:
                snippet = hit.get("content", "")[:300]
                if snippet:
                    context_parts.append(snippet)
        except Exception:
            pass  # KB unavailable — fine

        context = "\n\n".join(context_parts)

        # Suggested system prompt: prepend corrections context if available
        suggested_system_prompt = ""
        try:
            corrections_rows = conn.execute(
                """SELECT baseline_rule FROM correction_patterns
                   WHERE baseline_rule IS NOT NULL AND baseline_rule != ''
                   ORDER BY occurrence_count DESC LIMIT 3""",
            ).fetchall()
            rules = [r[0] for r in corrections_rows if r[0]]
            if rules:
                suggested_system_prompt = "[SYSTEM] Learned rules:\n" + "\n".join(f"- {r}" for r in rules)
        except sqlite3.OperationalError:
            pass

        return {
            "context": context,
            "similar_patterns": similar_patterns,
            "suggested_system_prompt": suggested_system_prompt,
        }
    except Exception as exc:
        logger.warning("hooks_pre_task: unexpected error: %s", exc)
        return empty
    finally:
        try:
            conn.close()
        except Exception:
            pass


def hooks_post_task(
    agent_id: str,
    execution_id: str,
    success: bool,
    output_summary: Optional[str] = None,
    agent_type: Optional[str] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """
    Called AFTER agent_execute.

    Records success/failure to the learning engine, optionally stores the
    output pattern, and updates the agent trust score.

    Returns:
        pattern_stored, trust_updated, new_trust
    """
    pattern_stored = False
    trust_updated = False
    new_trust = 0.5

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("hooks_post_task: db unavailable: %s", exc)
        return {"pattern_stored": False, "trust_updated": False, "new_trust": 0.5}

    try:
        # Record to learning engine (if available)
        try:
            from cap.learning.engine import update_trust as _update_trust_engine
            # get_trust is imported separately to avoid name collision
            new_trust = _update_trust_engine(agent_id, success, conn)
            trust_updated = True
        except Exception as exc:
            logger.debug("hooks_post_task: learning engine unavailable: %s", exc)
            # Fall back to internal helper
            delta = 0.05 if success else -0.10
            new_trust = _update_trust(agent_id, delta, conn)
            trust_updated = True

        # Store output pattern for future hooks_pre_task lookups
        if success and output_summary:
            pattern_id = uuid.uuid4().hex
            prompt_summary = f"agent={agent_id} exec={execution_id}"
            stored_agent_type = agent_type or "dev"
            conn.execute(
                """INSERT OR IGNORE INTO patterns
                   (id, task_type, prompt_hash, prompt_summary, model, agent_type,
                    cost_usd, duration_ms, success, output_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    pattern_id,
                    "post_task",
                    execution_id[:16],  # use execution_id as a proxy hash
                    prompt_summary,
                    None,               # model unknown at this level
                    stored_agent_type,
                    None,               # cost unknown
                    None,               # duration unknown
                    1,
                    output_summary[:500],
                ),
            )
            conn.commit()
            pattern_stored = True

            # Embed the pattern for future vector similarity search
            try:
                from cap.harness.vector_patterns import PatternEmbedder
                embed_text = output_summary[:500]
                if PatternEmbedder().embed_pattern(pattern_id, embed_text):
                    conn.execute(
                        "UPDATE patterns SET embedding_id = ? WHERE id = ?",
                        (pattern_id, pattern_id),
                    )
                    conn.commit()
            except Exception:
                pass  # embedding is best-effort

        return {
            "pattern_stored": pattern_stored,
            "trust_updated": trust_updated,
            "new_trust": round(new_trust, 4),
        }
    except Exception as exc:
        logger.warning("hooks_post_task: unexpected error: %s", exc)
        return {"pattern_stored": False, "trust_updated": False, "new_trust": 0.5}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def hooks_feedback(
    agent_id: str,
    task_hash: str,
    quality: str,
    notes: Optional[str] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """
    Record user feedback for the learning loop.

    quality: 'good' | 'bad' | 'neutral'
    - good:    trust +0.05
    - bad:     trust -0.10, correction pattern recorded
    - neutral: trust +0.01

    Returns:
        recorded, new_trust
    """
    _delta_map = {"good": 0.05, "bad": -0.10, "neutral": 0.01}
    quality = quality.lower().strip()
    if quality not in _delta_map:
        quality = "neutral"
    delta = _delta_map[quality]

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("hooks_feedback: db unavailable: %s", exc)
        return {"recorded": False, "new_trust": 0.5}

    try:
        new_trust = _update_trust(agent_id, delta, conn)

        # Record correction pattern for bad feedback
        if quality == "bad" and notes:
            try:
                from cap.learning.engine import record_correction
                record_correction(
                    what_wrong=f"agent={agent_id} task={task_hash}: {notes[:200]}",
                    what_correct=notes[:200],
                    category="feedback",
                    db=conn,
                )
            except Exception as exc:
                logger.debug("hooks_feedback: record_correction unavailable: %s", exc)
                # Minimal fallback: insert directly
                try:
                    conn.execute(
                        """INSERT INTO correction_patterns
                           (pattern, correction, occurrence_count, first_seen, last_seen)
                           VALUES (?, ?, 1, ?, ?)
                           ON CONFLICT(pattern) DO UPDATE SET
                               occurrence_count = occurrence_count + 1,
                               last_seen = excluded.last_seen""",
                        (
                            f"agent={agent_id}:{notes[:50]}",
                            notes[:200],
                            time.time(),
                            time.time(),
                        ),
                    )
                    conn.commit()
                except Exception:
                    pass

        return {"recorded": True, "new_trust": round(new_trust, 4)}
    except Exception as exc:
        logger.warning("hooks_feedback: unexpected error: %s", exc)
        return {"recorded": False, "new_trust": 0.5}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def hooks_intelligence(
    action: str,
    data: dict,
    _db_path: Optional[Path] = None,
) -> dict:
    """
    Low-level intelligence storage operations.

    action values:
    - pattern_store   : save a successful task pattern
    - pattern_search  : find similar patterns
    - trajectory_start: begin tracking a multi-step task
    - trajectory_step : record one step in a trajectory
    - stats           : return learning statistics
    """
    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("hooks_intelligence: db unavailable: %s", exc)
        return {"error": f"db unavailable: {exc}"}

    try:
        if action == "pattern_store":
            return _intelligence_pattern_store(data, conn)
        elif action == "pattern_search":
            return _intelligence_pattern_search(data, conn)
        elif action == "trajectory_start":
            return _intelligence_trajectory_start(data, conn)
        elif action == "trajectory_step":
            return _intelligence_trajectory_step(data, conn)
        elif action == "stats":
            return _intelligence_stats(conn)
        else:
            return {"error": f"unknown action: {action}"}
    except Exception as exc:
        logger.warning("hooks_intelligence: unexpected error action=%s: %s", action, exc)
        return {"error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# intelligence sub-handlers
# ---------------------------------------------------------------------------

def _intelligence_pattern_store(data: dict, conn: sqlite3.Connection) -> dict:
    pattern_id = uuid.uuid4().hex
    task_type = data.get("task_type", "unknown")
    prompt_summary = str(data.get("prompt_summary", ""))[:500]
    model = data.get("model")
    agent_type = data.get("agent_type")
    cost_usd = data.get("cost")
    duration_ms = data.get("duration")
    phash = _prompt_hash(prompt_summary) if prompt_summary else uuid.uuid4().hex[:16]

    conn.execute(
        """INSERT OR IGNORE INTO patterns
           (id, task_type, prompt_hash, prompt_summary, model, agent_type,
            cost_usd, duration_ms, success, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)""",
        (pattern_id, task_type, phash, prompt_summary, model, agent_type, cost_usd, duration_ms),
    )
    conn.commit()
    return {"stored": True, "pattern_id": pattern_id}


def _intelligence_pattern_search(data: dict, conn: sqlite3.Connection) -> dict:
    query = str(data.get("query", ""))
    limit = int(data.get("limit", 5))
    limit = min(limit, 50)  # cap to prevent abuse
    phash = _prompt_hash(query)

    rows = conn.execute(
        """SELECT id, task_type, prompt_summary, model, agent_type, cost_usd, duration_ms
           FROM patterns
           WHERE prompt_hash = ? AND success = 1
           ORDER BY created_at DESC
           LIMIT ?""",
        (phash, limit),
    ).fetchall()

    # Fallback: text search on prompt_summary when no hash match
    if not rows and query:
        like_term = f"%{query[:50]}%"
        rows = conn.execute(
            """SELECT id, task_type, prompt_summary, model, agent_type, cost_usd, duration_ms
               FROM patterns
               WHERE prompt_summary LIKE ? AND success = 1
               ORDER BY created_at DESC
               LIMIT ?""",
            (like_term, limit),
        ).fetchall()

    results = [
        {
            "id": r[0],
            "task_type": r[1],
            "prompt_summary": r[2],
            "model": r[3],
            "agent_type": r[4],
            "cost_usd": r[5],
            "duration_ms": r[6],
        }
        for r in rows
    ]
    return {"results": results, "count": len(results)}


def _intelligence_trajectory_start(data: dict, conn: sqlite3.Connection) -> dict:
    trajectory_id = data.get("trajectory_id") or uuid.uuid4().hex
    agent_id = data.get("agent_id", "unknown")
    action = data.get("action", "start")

    step_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO trajectories
           (id, trajectory_id, step_index, agent_id, action, result, cost_usd, created_at)
           VALUES (?, ?, 0, ?, ?, NULL, NULL, CURRENT_TIMESTAMP)""",
        (step_id, trajectory_id, agent_id, action),
    )
    conn.commit()
    return {"trajectory_id": trajectory_id, "step_id": step_id, "step_index": 0}


def _intelligence_trajectory_step(data: dict, conn: sqlite3.Connection) -> dict:
    trajectory_id = data.get("trajectory_id")
    if not trajectory_id:
        return {"error": "trajectory_id required"}

    # Determine next step index
    row = conn.execute(
        "SELECT MAX(step_index) FROM trajectories WHERE trajectory_id = ?",
        (trajectory_id,),
    ).fetchone()
    next_index = (row[0] or 0) + 1

    step_id = uuid.uuid4().hex
    agent_id = data.get("agent_id", "unknown")
    action = str(data.get("action", ""))[:1000]
    result = str(data.get("result", ""))[:1000] if data.get("result") is not None else None
    cost_usd = data.get("cost_usd")

    conn.execute(
        """INSERT INTO trajectories
           (id, trajectory_id, step_index, agent_id, action, result, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (step_id, trajectory_id, next_index, agent_id, action, result, cost_usd),
    )
    conn.commit()
    return {"trajectory_id": trajectory_id, "step_id": step_id, "step_index": next_index}


def _intelligence_stats(conn: sqlite3.Connection) -> dict:
    # Total patterns
    total_patterns = 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()
        total_patterns = row[0] if row else 0
    except sqlite3.OperationalError:
        pass

    # Success rate
    success_rate = 0.0
    try:
        row = conn.execute(
            "SELECT AVG(CAST(success AS REAL)) FROM patterns"
        ).fetchone()
        success_rate = float(row[0]) if row and row[0] is not None else 0.0
    except sqlite3.OperationalError:
        pass

    # Avg cost by model
    avg_cost_by_model: dict[str, float] = {}
    try:
        rows = conn.execute(
            """SELECT model, AVG(cost_usd)
               FROM patterns
               WHERE model IS NOT NULL AND cost_usd IS NOT NULL
               GROUP BY model""",
        ).fetchall()
        avg_cost_by_model = {r[0]: round(float(r[1]), 6) for r in rows if r[0]}
    except sqlite3.OperationalError:
        pass

    return {
        "total_patterns": total_patterns,
        "success_rate": round(success_rate, 4),
        "avg_cost_by_model": avg_cost_by_model,
    }

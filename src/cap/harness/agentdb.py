"""AgentDB — higher-level memory operations for learned patterns, reasoning chains,
and semantic routing.

All storage is layered on top of the shared platform.db used by hooks.py.
Two extra tables are introduced here:

  reasoning_bank  — stores step-by-step reasoning chains and their conclusions

Tables from hooks.py that are queried here:
  patterns        — learned task execution patterns
  (knowledge_entries / session_events are read-only, never written)

Each public function maps 1-to-1 onto an MCP tool registered in harness_server.py.
All functions are gracefully degraded: they never raise to the caller.
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

logger = logging.getLogger("cap.harness.agentdb")

# ---------------------------------------------------------------------------
# DB bootstrap — reuse the same platform.db as hooks.py
# ---------------------------------------------------------------------------

try:
    from cap.harness.agent_store import PLATFORM_DB_PATH
except ImportError:
    PLATFORM_DB_PATH = Path.home() / ".claude-platform" / "data" / "platform.db"

# DDL for the new reasoning_bank table (patterns DDL lives in hooks.py)
_REASONING_DDL = """
CREATE TABLE IF NOT EXISTS reasoning_bank (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    task_hash TEXT,
    steps_json TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rb_conclusion ON reasoning_bank(conclusion);
CREATE INDEX IF NOT EXISTS idx_rb_task_hash  ON reasoning_bank(task_hash);
"""

# The patterns DDL from hooks — we repeat it here so agentdb can bootstrap
# a fresh DB without depending on hooks.py being imported first.
_PATTERNS_DDL = """
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
CREATE INDEX IF NOT EXISTS idx_patterns_prompt_hash ON patterns(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_patterns_task_type   ON patterns(task_type);
"""

_PATTERNS_MIGRATE_EMBEDDING_ID = (
    "ALTER TABLE patterns ADD COLUMN embedding_id TEXT"
)


def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (or create) platform.db and ensure agentdb tables exist."""
    path = db_path or PLATFORM_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_PATTERNS_DDL)
    conn.executescript(_REASONING_DDL)
    try:
        conn.execute(_PATTERNS_MIGRATE_EMBEDDING_ID)
        conn.commit()
    except Exception:
        pass  # column already exists
    return conn


def _prompt_hash(text: str) -> str:
    """Stable 16-char hex hash — same algorithm as hooks.py."""
    normalized = text.strip().lower()[:200]
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 1. agentdb_pattern_store
# ---------------------------------------------------------------------------

def agentdb_pattern_store(
    task_type: str,
    prompt_summary: str,
    model: str,
    agent_type: str,
    cost_usd: float,
    duration_ms: int,
    success: bool = True,
    output_summary: Optional[str] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """Store a learned pattern; skips if the same prompt_hash was stored within the last hour.

    Returns:
        {pattern_id: str, deduplicated: bool}
    """
    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_pattern_store: db unavailable: %s", exc)
        return {"error": f"db unavailable: {exc}"}

    try:
        phash = _prompt_hash(prompt_summary)

        # Dedup: skip if same hash exists within the last 3600 seconds
        row = conn.execute(
            """SELECT id FROM patterns
               WHERE prompt_hash = ?
                 AND (unixepoch(created_at) > ? OR created_at > datetime('now', '-1 hour'))
               LIMIT 1""",
            (phash, int(time.time()) - 3600),
        ).fetchone()

        if row:
            conn.close()
            return {"pattern_id": row["id"], "deduplicated": True}

        pattern_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO patterns
               (id, task_type, prompt_hash, prompt_summary, model, agent_type,
                cost_usd, duration_ms, success, output_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                pattern_id,
                task_type,
                phash,
                str(prompt_summary)[:500],
                model,
                agent_type,
                float(cost_usd),
                int(duration_ms),
                1 if success else 0,
                str(output_summary)[:500] if output_summary else None,
            ),
        )
        conn.commit()
        return {"pattern_id": pattern_id, "deduplicated": False}

    except Exception as exc:
        logger.warning("agentdb_pattern_store: unexpected error: %s", exc)
        return {"error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. agentdb_pattern_search
# ---------------------------------------------------------------------------

def agentdb_pattern_search(
    query: str,
    task_type: Optional[str] = None,
    limit: int = 5,
    _db_path: Optional[Path] = None,
) -> list[dict]:
    """Search patterns using vector similarity first, falling back to LIKE.

    1. Attempt cosine-similarity search via PatternEmbedder (LanceDB + Bedrock).
    2. Enrich vector hits with full pattern data from SQLite.
    3. If vector search returns nothing, fall back to hash-match then LIKE query.

    Returns:
        List of {pattern_id, prompt_summary, model, cost_usd, success, created_at}
    """
    limit = min(max(1, limit), 50)
    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_pattern_search: db unavailable: %s", exc)
        return []

    try:
        type_clause = "AND task_type = ?" if task_type else ""
        type_param: list = [task_type] if task_type else []

        # --- 1. Vector similarity search (primary path) ---
        try:
            from cap.harness.vector_patterns import PatternEmbedder
            pe = PatternEmbedder()
            if pe.is_available:
                vector_hits = pe.search_similar(query, limit=limit)
                if vector_hits:
                    pattern_ids = [h["pattern_id"] for h in vector_hits]
                    placeholders = ",".join("?" * len(pattern_ids))
                    rows = conn.execute(
                        f"""SELECT id, prompt_summary, model, cost_usd, success, created_at
                            FROM patterns
                            WHERE id IN ({placeholders}) {type_clause}
                            ORDER BY created_at DESC""",
                        pattern_ids + type_param,
                    ).fetchall()
                    if rows:
                        # Sort by vector score order
                        score_map = {h["pattern_id"]: h["score"] for h in vector_hits}
                        rows_sorted = sorted(rows, key=lambda r: score_map.get(r["id"], 0.0), reverse=True)
                        results = [
                            {
                                "pattern_id": r["id"],
                                "prompt_summary": r["prompt_summary"],
                                "model": r["model"],
                                "cost_usd": r["cost_usd"],
                                "success": bool(r["success"]),
                                "created_at": r["created_at"],
                            }
                            for r in rows_sorted
                        ]
                        try:
                            from cap.harness.retention import record_pattern_use
                            for res in results:
                                record_pattern_use(res["pattern_id"], db=_db_path)
                        except Exception:
                            pass
                        return results
        except Exception as exc:
            logger.debug("agentdb_pattern_search: vector search skipped: %s", exc)

        # --- 2. Hash-match fast path ---
        phash = _prompt_hash(query)
        rows = conn.execute(
            f"""SELECT id, prompt_summary, model, cost_usd, success, created_at
                FROM patterns
                WHERE prompt_hash = ? {type_clause}
                ORDER BY created_at DESC
                LIMIT ?""",
            [phash] + type_param + [limit],
        ).fetchall()

        # --- 3. LIKE fallback ---
        if not rows and query:
            like_term = f"%{query[:80]}%"
            rows = conn.execute(
                f"""SELECT id, prompt_summary, model, cost_usd, success, created_at
                    FROM patterns
                    WHERE prompt_summary LIKE ? {type_clause}
                    ORDER BY created_at DESC
                    LIMIT ?""",
                [like_term] + type_param + [limit],
            ).fetchall()

        results = [
            {
                "pattern_id": r["id"],
                "prompt_summary": r["prompt_summary"],
                "model": r["model"],
                "cost_usd": r["cost_usd"],
                "success": bool(r["success"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        try:
            from cap.harness.retention import record_pattern_use
            for res in results:
                record_pattern_use(res["pattern_id"], db=_db_path)
        except Exception:
            pass
        return results

    except Exception as exc:
        logger.warning("agentdb_pattern_search: unexpected error: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. agentdb_reasoning_store
# ---------------------------------------------------------------------------

def agentdb_reasoning_store(
    agent_id: str,
    reasoning_chain: list[str],
    conclusion: str,
    task_hash: Optional[str] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """Store a reasoning chain (ordered list of steps) with a conclusion.

    Returns:
        {reasoning_id: str}
    """
    if not isinstance(reasoning_chain, list):
        return {"error": "reasoning_chain must be a list of strings"}
    if not conclusion:
        return {"error": "conclusion is required"}

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_reasoning_store: db unavailable: %s", exc)
        return {"error": f"db unavailable: {exc}"}

    try:
        reasoning_id = uuid.uuid4().hex
        steps_json = json.dumps([str(s)[:1000] for s in reasoning_chain])
        conn.execute(
            """INSERT INTO reasoning_bank
               (id, agent_id, task_hash, steps_json, conclusion, created_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                reasoning_id,
                str(agent_id)[:100],
                str(task_hash)[:64] if task_hash else None,
                steps_json,
                str(conclusion)[:2000],
            ),
        )
        conn.commit()
        return {"reasoning_id": reasoning_id}

    except Exception as exc:
        logger.warning("agentdb_reasoning_store: unexpected error: %s", exc)
        return {"error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 4. agentdb_reasoning_recall
# ---------------------------------------------------------------------------

def agentdb_reasoning_recall(
    query: str,
    agent_type: Optional[str] = None,
    limit: int = 3,
    _db_path: Optional[Path] = None,
) -> list[dict]:
    """Search the reasoning bank by conclusion text (LIKE search on conclusion).

    Returns:
        List of {reasoning_id, conclusion, steps (list), agent_id, created_at}
    """
    limit = min(max(1, limit), 20)
    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_reasoning_recall: db unavailable: %s", exc)
        return []

    try:
        like_term = f"%{query[:80]}%"
        agent_clause = "AND agent_id = ?" if agent_type else ""
        agent_param: list = [agent_type] if agent_type else []

        rows = conn.execute(
            f"""SELECT id, conclusion, steps_json, agent_id, created_at
                FROM reasoning_bank
                WHERE conclusion LIKE ? {agent_clause}
                ORDER BY created_at DESC
                LIMIT ?""",
            [like_term] + agent_param + [limit],
        ).fetchall()

        results = []
        for r in rows:
            try:
                steps = json.loads(r["steps_json"])
            except (json.JSONDecodeError, TypeError):
                steps = []
            results.append({
                "reasoning_id": r["id"],
                "conclusion": r["conclusion"],
                "steps": steps,
                "agent_id": r["agent_id"],
                "created_at": r["created_at"],
            })
        return results

    except Exception as exc:
        logger.warning("agentdb_reasoning_recall: unexpected error: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. agentdb_semantic_route
# ---------------------------------------------------------------------------

# Keyword sets per agent type used when no historical patterns are available.
_AGENT_KEYWORDS: dict[str, list[str]] = {
    "dev": ["implement", "code", "build", "feature", "fix", "refactor", "write", "function", "class", "module"],
    "devops": ["deploy", "pipeline", "ci", "cd", "terraform", "helm", "k8s", "kubernetes", "argo", "infra"],
    "security": ["security", "audit", "vulnerability", "cve", "permission", "iam", "rbac", "secret", "encrypt"],
    "sre": ["monitor", "alert", "latency", "slo", "sla", "metric", "trace", "log", "observability", "oncall"],
    "test": ["test", "spec", "unit", "integration", "coverage", "mock", "assert", "pytest", "jest"],
    "docs": ["document", "readme", "wiki", "changelog", "docstring", "comment", "explain"],
    "aws-architect": ["architect", "design", "system", "cloud", "aws", "rds", "s3", "lambda", "vpc", "subnet"],
    "code-review": ["review", "pr", "pull request", "diff", "critique", "quality", "lint"],
    "optimization": ["optimize", "performance", "bottleneck", "profil", "slow", "speed", "cost", "cache"],
}


def agentdb_semantic_route(
    task: str,
    _db_path: Optional[Path] = None,
) -> dict:
    """Recommend the best agent_type for a task based on past pattern success rates.

    Algorithm:
    1. Query patterns grouped by agent_type — compute success_rate and avg_cost.
    2. Score each candidate agent_type by matching task keywords against its
       successful pattern summaries.
    3. If no patterns exist, fall back to keyword heuristics.

    Returns:
        {recommended_agent_type, confidence, based_on_patterns, alternatives}
    """
    _default = {
        "recommended_agent_type": "dev",
        "confidence": 0.3,
        "based_on_patterns": 0,
        "alternatives": [],
    }

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_semantic_route: db unavailable: %s", exc)
        return _default

    try:
        # 1. Aggregate per-agent_type stats from patterns table
        rows = conn.execute(
            """SELECT agent_type,
                      COUNT(*) AS total,
                      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                      AVG(CASE WHEN cost_usd IS NOT NULL THEN cost_usd ELSE 0 END) AS avg_cost
               FROM patterns
               WHERE agent_type IS NOT NULL
               GROUP BY agent_type"""
        ).fetchall()

        if not rows:
            # No history — use keyword heuristics
            conn.close()
            return _keyword_route_fallback(task)

        total_patterns = sum(r["total"] for r in rows)
        agent_stats: dict[str, dict] = {}
        for r in rows:
            atype = r["agent_type"]
            successes = r["successes"] or 0
            total = r["total"] or 1
            agent_stats[atype] = {
                "success_rate": successes / total,
                "avg_cost": float(r["avg_cost"] or 0.0),
                "total": total,
            }

        # 2. Keyword match: fetch successful pattern summaries and score
        task_lower = task.lower()
        task_words = set(task_lower.split())

        agent_match_score: dict[str, float] = {a: 0.0 for a in agent_stats}

        summary_rows = conn.execute(
            """SELECT agent_type, prompt_summary
               FROM patterns
               WHERE success = 1 AND agent_type IS NOT NULL AND prompt_summary IS NOT NULL
               ORDER BY created_at DESC
               LIMIT 200"""
        ).fetchall()

        for sr in summary_rows:
            atype = sr["agent_type"]
            if atype not in agent_match_score:
                continue
            summary_words = set(str(sr["prompt_summary"]).lower().split())
            overlap = len(task_words & summary_words)
            if overlap:
                agent_match_score[atype] += overlap / max(len(task_words), 1)

        # 3. Combined score: 60% keyword match + 40% success rate
        combined: dict[str, float] = {}
        for atype, stats in agent_stats.items():
            kw_score = agent_match_score.get(atype, 0.0)
            # Normalise keyword score against the max
            combined[atype] = 0.6 * kw_score + 0.4 * stats["success_rate"]

        sorted_agents = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        best_agent, best_score = sorted_agents[0]

        # Normalise confidence to [0.3, 0.95]
        max_possible = 0.6 * max(agent_match_score.values(), default=1.0) + 0.4
        confidence = 0.3 + 0.65 * (best_score / max(max_possible, 1e-9))
        confidence = round(min(0.95, max(0.3, confidence)), 3)

        alternatives = [
            {
                "agent_type": a,
                "score": round(s, 4),
                "success_rate": round(agent_stats[a]["success_rate"], 3),
                "avg_cost_usd": round(agent_stats[a]["avg_cost"], 6),
            }
            for a, s in sorted_agents[1:4]
        ]

        return {
            "recommended_agent_type": best_agent,
            "confidence": confidence,
            "based_on_patterns": total_patterns,
            "alternatives": alternatives,
        }

    except Exception as exc:
        logger.warning("agentdb_semantic_route: unexpected error: %s", exc)
        return _default
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _keyword_route_fallback(task: str) -> dict:
    """Pure-keyword fallback when no patterns exist."""
    task_lower = task.lower()
    scores: dict[str, int] = {}
    for atype, keywords in _AGENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in task_lower)
        if hits:
            scores[atype] = hits

    if not scores:
        return {
            "recommended_agent_type": "dev",
            "confidence": 0.3,
            "based_on_patterns": 0,
            "alternatives": [],
        }

    sorted_types = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best, best_hits = sorted_types[0]
    max_kw = max(len(kws) for kws in _AGENT_KEYWORDS.values())
    confidence = round(0.3 + 0.4 * (best_hits / max_kw), 3)
    confidence = min(0.7, confidence)  # cap fallback confidence
    alternatives = [
        {"agent_type": a, "keyword_hits": h}
        for a, h in sorted_types[1:4]
    ]
    return {
        "recommended_agent_type": best,
        "confidence": confidence,
        "based_on_patterns": 0,
        "alternatives": alternatives,
    }


# ---------------------------------------------------------------------------
# 6. agentdb_hierarchical_recall
# ---------------------------------------------------------------------------

_ALL_TIERS = ("patterns", "reasoning", "knowledge", "sessions")


def agentdb_hierarchical_recall(
    query: str,
    tiers: Optional[list[str]] = None,
    _db_path: Optional[Path] = None,
) -> dict:
    """Search across multiple knowledge tiers and return combined results.

    Tiers:
        "patterns"  -> patterns table (prompt_summary LIKE)
        "reasoning" -> reasoning_bank table (conclusion LIKE)
        "knowledge" -> knowledge_entries table (title / content LIKE)
        "sessions"  -> session_events table (content LIKE)

    Returns:
        {patterns: [...], reasoning: [...], knowledge: [...], sessions: [...]}
    """
    active_tiers = set(tiers or _ALL_TIERS) & set(_ALL_TIERS)
    result: dict[str, list] = {t: [] for t in _ALL_TIERS}

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_hierarchical_recall: db unavailable: %s", exc)
        return result

    like_term = f"%{query[:80]}%"

    try:
        # --- patterns tier ---
        if "patterns" in active_tiers:
            try:
                rows = conn.execute(
                    """SELECT id, prompt_summary, model, cost_usd, success, created_at
                       FROM patterns
                       WHERE prompt_summary LIKE ?
                       ORDER BY created_at DESC
                       LIMIT 5""",
                    (like_term,),
                ).fetchall()
                result["patterns"] = [
                    {
                        "pattern_id": r["id"],
                        "prompt_summary": r["prompt_summary"],
                        "model": r["model"],
                        "cost_usd": r["cost_usd"],
                        "success": bool(r["success"]),
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                pass

        # --- reasoning tier ---
        if "reasoning" in active_tiers:
            try:
                rows = conn.execute(
                    """SELECT id, conclusion, steps_json, agent_id, created_at
                       FROM reasoning_bank
                       WHERE conclusion LIKE ?
                       ORDER BY created_at DESC
                       LIMIT 5""",
                    (like_term,),
                ).fetchall()
                for r in rows:
                    try:
                        steps = json.loads(r["steps_json"])
                    except (json.JSONDecodeError, TypeError):
                        steps = []
                    result["reasoning"].append({
                        "reasoning_id": r["id"],
                        "conclusion": r["conclusion"],
                        "steps": steps,
                        "agent_id": r["agent_id"],
                        "created_at": r["created_at"],
                    })
            except sqlite3.OperationalError:
                pass

        # --- knowledge tier ---
        if "knowledge" in active_tiers:
            try:
                rows = conn.execute(
                    """SELECT uuid, title, content, source_path, content_type, workspace
                       FROM knowledge_entries
                       WHERE title LIKE ? OR content LIKE ?
                       ORDER BY id DESC
                       LIMIT 5""",
                    (like_term, like_term),
                ).fetchall()
                result["knowledge"] = [
                    {
                        "uuid": r["uuid"],
                        "title": r["title"],
                        "content_preview": (r["content"] or "")[:200],
                        "source_path": r["source_path"],
                        "content_type": r["content_type"],
                        "workspace": r["workspace"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                pass  # Table does not exist in this DB — silently skip

        # --- sessions tier ---
        if "sessions" in active_tiers:
            try:
                rows = conn.execute(
                    """SELECT id, event_type, content, workspace, timestamp
                       FROM session_events
                       WHERE content LIKE ?
                       ORDER BY timestamp DESC
                       LIMIT 5""",
                    (like_term,),
                ).fetchall()
                result["sessions"] = [
                    {
                        "id": r["id"],
                        "event_type": r["event_type"],
                        "content": (r["content"] or "")[:300],
                        "workspace": r["workspace"],
                        "timestamp": r["timestamp"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                pass  # Table does not exist in this DB — silently skip

        return result

    except Exception as exc:
        logger.warning("agentdb_hierarchical_recall: unexpected error: %s", exc)
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 7. agentdb_stats
# ---------------------------------------------------------------------------

def agentdb_stats(_db_path: Optional[Path] = None) -> dict:
    """Return aggregate statistics from the agentdb tables.

    Returns:
        {total_patterns, total_reasoning_chains, patterns_by_type, success_rate, avg_cost}
    """
    default: dict = {
        "total_patterns": 0,
        "total_reasoning_chains": 0,
        "patterns_by_type": {},
        "success_rate": 0.0,
        "avg_cost": 0.0,
    }

    try:
        conn = _get_conn(_db_path)
    except Exception as exc:
        logger.warning("agentdb_stats: db unavailable: %s", exc)
        return default

    try:
        # Total patterns
        row = conn.execute("SELECT COUNT(*) AS n FROM patterns").fetchone()
        total_patterns = int(row["n"]) if row else 0

        # Patterns by task_type
        rows = conn.execute(
            "SELECT task_type, COUNT(*) AS n FROM patterns GROUP BY task_type"
        ).fetchall()
        patterns_by_type = {
            str(r["task_type"] or "unknown"): int(r["n"]) for r in rows
        }

        # Success rate
        row = conn.execute(
            "SELECT AVG(CAST(success AS REAL)) AS sr FROM patterns"
        ).fetchone()
        success_rate = round(float(row["sr"]), 4) if row and row["sr"] is not None else 0.0

        # Average cost (exclude NULLs)
        row = conn.execute(
            "SELECT AVG(cost_usd) AS ac FROM patterns WHERE cost_usd IS NOT NULL"
        ).fetchone()
        avg_cost = round(float(row["ac"]), 8) if row and row["ac"] is not None else 0.0

        # Total reasoning chains
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM reasoning_bank"
        ).fetchone()
        total_reasoning = int(row["n"]) if row else 0

        return {
            "total_patterns": total_patterns,
            "total_reasoning_chains": total_reasoning,
            "patterns_by_type": patterns_by_type,
            "success_rate": success_rate,
            "avg_cost": avg_cost,
        }

    except Exception as exc:
        logger.warning("agentdb_stats: unexpected error: %s", exc)
        return default
    finally:
        try:
            conn.close()
        except Exception:
            pass

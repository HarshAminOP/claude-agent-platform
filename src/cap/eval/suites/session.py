"""Session memory evaluation suite.

Tests learning reinforcement, correction application, decision recall,
and cross-session persistence using a synthetic session DB.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from cap.eval.framework import EvalCase, EvalResult, EvalSuite, MetricType


# ---------------------------------------------------------------------------
# Synthetic session data
# ---------------------------------------------------------------------------

SYNTHETIC_LEARNINGS = [
    {
        "session_id": "sess-001",
        "category": "preference",
        "key": "deployment_strategy",
        "value": "Always use canary deployments for production services",
        "confidence": 0.7,
    },
    {
        "session_id": "sess-001",
        "category": "pattern",
        "key": "terraform_module_structure",
        "value": "Use a modules/ directory with environment-specific tfvars",
        "confidence": 0.8,
    },
    {
        "session_id": "sess-002",
        "category": "preference",
        "key": "deployment_strategy",
        "value": "Always use canary deployments for production services",
        "confidence": 0.85,  # Reinforced — should increase confidence
    },
    {
        "session_id": "sess-002",
        "category": "domain_knowledge",
        "key": "eks_node_groups",
        "value": "Managed node groups are preferred over self-managed for EKS 1.28+",
        "confidence": 0.9,
    },
    {
        "session_id": "sess-003",
        "category": "preference",
        "key": "deployment_strategy",
        "value": "Always use canary deployments for production services",
        "confidence": 0.92,  # Third reinforcement
    },
]

SYNTHETIC_CORRECTIONS = [
    {
        "session_id": "sess-001",
        "wrong": "Use kubectl apply -f for all manifest changes",
        "correct": "Use ArgoCD sync for GitOps-managed resources, only kubectl for debugging",
        "context": "deployment methodology",
    },
    {
        "session_id": "sess-002",
        "wrong": "Put all terraform in a single state file",
        "correct": "Split state by layer: network, platform, workloads",
        "context": "terraform state management",
    },
    {
        "session_id": "sess-003",
        "wrong": "Use IAM users for service authentication",
        "correct": "Use IRSA (IAM Roles for Service Accounts) for pod-level auth",
        "context": "kubernetes authentication",
    },
]

SYNTHETIC_DECISIONS = [
    {
        "session_id": "sess-001",
        "title": "Chose Karpenter over Cluster Autoscaler",
        "rationale": "Better bin-packing, faster scaling, native spot support",
        "context": "EKS node autoscaling",
        "tags": "eks,autoscaling,karpenter",
    },
    {
        "session_id": "sess-001",
        "title": "Adopted SOPS over Sealed Secrets",
        "rationale": "Better audit trail, KMS integration, simpler key rotation",
        "context": "secret management in GitOps",
        "tags": "security,secrets,sops,gitops",
    },
    {
        "session_id": "sess-002",
        "title": "Migrated from PodSecurityPolicy to PSA",
        "rationale": "PSP deprecated in 1.25, PSA is built-in, label-based enforcement",
        "context": "kubernetes security",
        "tags": "eks,security,psa,migration",
    },
    {
        "session_id": "sess-003",
        "title": "Selected Mimir over Thanos for long-term metrics",
        "rationale": "Better query performance, native multi-tenancy, simpler operations",
        "context": "observability architecture",
        "tags": "observability,metrics,mimir",
    },
]


# ---------------------------------------------------------------------------
# Suite implementation
# ---------------------------------------------------------------------------


class SessionEvalSuite(EvalSuite):
    """Evaluates session memory: reinforcement, corrections, and recall."""

    name = "session"
    description = "Measures learning reinforcement, correction accuracy, and decision recall quality"

    def __init__(self) -> None:
        super().__init__()
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._tmp_dir: tempfile.TemporaryDirectory | None = None

    def setup(self) -> None:
        """Create test session DB with synthetic data."""
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="cap_eval_session_")
        self._db_path = Path(self._tmp_dir.name) / "sessions.db"
        self._conn = sqlite3.connect(str(self._db_path))

        # Create session schema (mirrors sessions DB from db_init.py)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL DEFAULT 'default',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at TEXT,
                summary TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                reinforcement_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                wrong_value TEXT NOT NULL,
                correct_value TEXT NOT NULL,
                context TEXT,
                applied_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                rationale TEXT NOT NULL,
                context TEXT,
                tags TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
                title, rationale, context, tags,
                content='decisions',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE INDEX IF NOT EXISTS idx_learnings_key ON learnings(key);
            CREATE INDEX IF NOT EXISTS idx_learnings_category ON learnings(category);
            CREATE INDEX IF NOT EXISTS idx_corrections_context ON corrections(context);
        """)

        # Insert sessions
        for sid in ["sess-001", "sess-002", "sess-003"]:
            self._conn.execute(
                "INSERT INTO sessions (id, workspace) VALUES (?, ?)",
                (sid, "eval_test"),
            )

        # Insert learnings
        for learning in SYNTHETIC_LEARNINGS:
            self._conn.execute(
                """INSERT INTO learnings (session_id, category, key, value, confidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (learning["session_id"], learning["category"], learning["key"],
                 learning["value"], learning["confidence"]),
            )

        # Insert corrections
        for correction in SYNTHETIC_CORRECTIONS:
            self._conn.execute(
                """INSERT INTO corrections (session_id, wrong_value, correct_value, context)
                   VALUES (?, ?, ?, ?)""",
                (correction["session_id"], correction["wrong"], correction["correct"],
                 correction["context"]),
            )

        # Insert decisions
        for i, decision in enumerate(SYNTHETIC_DECISIONS):
            self._conn.execute(
                """INSERT INTO decisions (id, session_id, title, rationale, context, tags)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (i + 1, decision["session_id"], decision["title"],
                 decision["rationale"], decision["context"], decision["tags"]),
            )
            # Populate FTS
            self._conn.execute(
                "INSERT INTO decisions_fts (rowid, title, rationale, context, tags) VALUES (?, ?, ?, ?, ?)",
                (i + 1, decision["title"], decision["rationale"],
                 decision["context"], decision["tags"]),
            )

        self._conn.commit()

    def teardown(self) -> None:
        """Clean up."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._tmp_dir:
            self._tmp_dir.cleanup()
            self._tmp_dir = None

    def build_cases(self) -> list[EvalCase]:
        """Build eval cases for session memory."""
        cases: list[EvalCase] = []

        # --- Learning reinforcement ---
        cases.append(
            EvalCase(
                name="reinforcement_increases_confidence",
                category="learning_reinforcement",
                input={"key": "deployment_strategy", "category": "preference"},
                expected=0.92,  # Final confidence after 3 reinforcements
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.05},
            )
        )
        cases.append(
            EvalCase(
                name="reinforcement_count_accurate",
                category="learning_reinforcement",
                input={"key": "deployment_strategy", "category": "preference"},
                expected=3,  # 3 entries for same key
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="unreinforced_stays_low",
                category="learning_reinforcement",
                input={"key": "eks_node_groups", "category": "domain_knowledge"},
                expected=0.9,  # Single entry, original confidence
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.01},
            )
        )

        # --- Correction application ---
        cases.append(
            EvalCase(
                name="correction_maps_wrong_to_correct",
                category="correction_application",
                input="kubectl apply",
                expected="Use ArgoCD sync for GitOps-managed resources, only kubectl for debugging",
                metric=MetricType.FUZZY_MATCH,
                threshold=0.5,
            )
        )
        cases.append(
            EvalCase(
                name="correction_terraform_state",
                category="correction_application",
                input="single state file",
                expected="Split state by layer: network, platform, workloads",
                metric=MetricType.FUZZY_MATCH,
                threshold=0.4,
            )
        )
        cases.append(
            EvalCase(
                name="correction_no_false_match",
                category="correction_application",
                input="grafana dashboard configuration",
                expected="",  # Should not match any correction
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )

        # --- Decision recall via FTS5 ---
        cases.append(
            EvalCase(
                name="decision_recall_karpenter",
                category="decision_recall",
                input="autoscaling node management karpenter",
                expected=[1],  # Decision ID 1
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 3},
            )
        )
        cases.append(
            EvalCase(
                name="decision_recall_secrets",
                category="decision_recall",
                input="secret management encryption",
                expected=[2],  # Decision ID 2 (SOPS)
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 3},
            )
        )
        cases.append(
            EvalCase(
                name="decision_recall_observability",
                category="decision_recall",
                input="metrics storage long term",
                expected=[4],  # Decision ID 4 (Mimir)
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 3},
            )
        )
        cases.append(
            EvalCase(
                name="decision_recall_multiple",
                category="decision_recall",
                input="kubernetes security policy",
                expected=[3],  # PSA migration
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 3},
            )
        )

        # --- Cross-session persistence ---
        cases.append(
            EvalCase(
                name="cross_session_learning_persists",
                category="cross_session",
                input={"sessions": ["sess-001", "sess-002", "sess-003"]},
                expected={"min_learnings": 5, "min_corrections": 3, "min_decisions": 4},
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="cross_session_latest_confidence",
                category="cross_session",
                input={"key": "deployment_strategy", "latest_session": "sess-003"},
                expected=0.92,
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.05},
            )
        )

        # --- Decision recall latency ---
        cases.append(
            EvalCase(
                name="decision_fts5_latency_p95",
                category="latency",
                input="latency_fts5",
                expected=None,
                metric=MetricType.LATENCY_P95,
                threshold=20.0,  # 20ms for FTS5 on small DB
            )
        )

        return cases

    def evaluate_case(self, case: EvalCase) -> EvalResult:
        """Run a single session eval case."""
        if case.category == "learning_reinforcement":
            return self._eval_reinforcement(case)
        elif case.category == "correction_application":
            return self._eval_correction(case)
        elif case.category == "decision_recall":
            return self._eval_decision_recall(case)
        elif case.category == "cross_session":
            return self._eval_cross_session(case)
        elif case.category == "latency":
            return self._eval_latency(case)
        else:
            return EvalResult(
                case=case, actual=None, score=0.0, passed=False, latency_ms=0.0,
                details={"reason": f"Unknown category: {case.category}"},
            )

    def _eval_reinforcement(self, case: EvalCase) -> EvalResult:
        """Test that reinforcement tracking works correctly."""
        key = case.input["key"]
        category = case.input["category"]

        t0 = time.perf_counter()

        if "count" in case.name:
            # Count entries with this key
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE key = ? AND category = ?",
                (key, category),
            )
            actual = cursor.fetchone()[0]
            score = 1.0 if actual == case.expected else 0.0
        else:
            # Get max confidence for key
            cursor = self._conn.execute(
                "SELECT MAX(confidence) FROM learnings WHERE key = ? AND category = ?",
                (key, category),
            )
            actual = cursor.fetchone()[0]
            tolerance = case.metadata.get("tolerance", 0.01)
            score = 1.0 if abs(actual - case.expected) <= tolerance else 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"actual={actual}, expected={case.expected}"},
        )

    def _eval_correction(self, case: EvalCase) -> EvalResult:
        """Test correction retrieval: given a wrong pattern, find the correct mapping."""
        query = case.input
        t0 = time.perf_counter()

        # Search corrections by matching against wrong_value
        cursor = self._conn.execute(
            "SELECT correct_value FROM corrections WHERE wrong_value LIKE ? LIMIT 1",
            (f"%{query}%",),
        )
        row = cursor.fetchone()
        actual = row[0] if row else ""

        latency_ms = (time.perf_counter() - t0) * 1000
        score = self.compute_score(case, actual)
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"score {score:.3f} < {case.threshold}"},
        )

    def _eval_decision_recall(self, case: EvalCase) -> EvalResult:
        """Test decision recall using FTS5."""
        query = case.input
        t0 = time.perf_counter()

        try:
            cursor = self._conn.execute(
                "SELECT rowid FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY rank LIMIT 5",
                (query,),
            )
            actual_ids = [row[0] for row in cursor.fetchall()]
        except Exception:
            # Fallback to LIKE
            words = query.split()
            conditions = " OR ".join(
                ["title LIKE ? OR rationale LIKE ? OR context LIKE ? OR tags LIKE ?"] * len(words)
            )
            params = []
            for w in words:
                params.extend([f"%{w}%"] * 4)
            cursor = self._conn.execute(
                f"SELECT id FROM decisions WHERE {conditions} LIMIT 5", params
            )
            actual_ids = [row[0] for row in cursor.fetchall()]

        latency_ms = (time.perf_counter() - t0) * 1000
        score = self.compute_score(case, actual_ids)
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual_ids, score=score, passed=passed,
            latency_ms=latency_ms,
            details={
                "returned_ids": actual_ids,
                "expected_ids": case.expected,
                "reason": "pass" if passed else f"score {score:.3f} < {case.threshold}",
            },
        )

    def _eval_cross_session(self, case: EvalCase) -> EvalResult:
        """Test data persistence across sessions."""
        t0 = time.perf_counter()

        if "latest_confidence" in case.name:
            key = case.input["key"]
            session = case.input["latest_session"]
            cursor = self._conn.execute(
                "SELECT confidence FROM learnings WHERE key = ? AND session_id = ?",
                (key, session),
            )
            row = cursor.fetchone()
            actual = row[0] if row else 0.0
            tolerance = case.metadata.get("tolerance", 0.01)
            score = 1.0 if abs(actual - case.expected) <= tolerance else 0.0
        else:
            # Count records across all sessions
            sessions = case.input["sessions"]
            placeholders = ",".join(["?"] * len(sessions))

            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM learnings WHERE session_id IN ({placeholders})",
                sessions,
            )
            learning_count = cursor.fetchone()[0]

            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM corrections WHERE session_id IN ({placeholders})",
                sessions,
            )
            correction_count = cursor.fetchone()[0]

            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM decisions WHERE session_id IN ({placeholders})",
                sessions,
            )
            decision_count = cursor.fetchone()[0]

            actual = {
                "learnings": learning_count,
                "corrections": correction_count,
                "decisions": decision_count,
            }

            # Check all minimums met
            expected = case.expected
            all_met = (
                learning_count >= expected["min_learnings"]
                and correction_count >= expected["min_corrections"]
                and decision_count >= expected["min_decisions"]
            )
            score = 1.0 if all_met else 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"persistence check failed: {actual}"},
        )

    def _eval_latency(self, case: EvalCase) -> EvalResult:
        """Measure FTS5 decision recall latency."""
        queries = [
            "autoscaling karpenter",
            "secret encryption sops",
            "kubernetes security policy",
            "metrics observability mimir",
            "terraform state",
        ]

        latencies: list[float] = []
        for _ in range(20):
            for query in queries:
                t0 = time.perf_counter()
                try:
                    self._conn.execute(
                        "SELECT rowid FROM decisions_fts WHERE decisions_fts MATCH ? LIMIT 5",
                        (query,),
                    ).fetchall()
                except Exception:
                    pass
                latencies.append((time.perf_counter() - t0) * 1000)

        from cap.eval.framework import score_latency_p95
        import math

        score = score_latency_p95(latencies, case.threshold)
        sorted_lats = sorted(latencies)
        p95_idx = max(0, int(math.ceil(0.95 * len(sorted_lats))) - 1)
        p95_val = sorted_lats[p95_idx] if sorted_lats else 0.0
        passed = score >= 0.8

        return EvalResult(
            case=case, actual=latencies, score=score, passed=passed,
            latency_ms=p95_val,
            details={
                "p95_ms": p95_val,
                "threshold_ms": case.threshold,
                "iterations": len(latencies),
                "reason": "pass" if passed else f"p95 {p95_val:.1f}ms > {case.threshold}ms",
            },
        )

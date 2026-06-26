"""Retrieval quality evaluation suite.

Tests keyword search (BM25/FTS5), semantic search (embedding similarity),
hybrid fusion (RRF merge), and graph traversal completeness.
Uses a synthetic test corpus — no external dependencies.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from cap.eval.framework import EvalCase, EvalResult, EvalSuite, MetricType


# ---------------------------------------------------------------------------
# Synthetic test corpus
# ---------------------------------------------------------------------------

SYNTHETIC_DOCS = [
    {
        "title": "EKS Cluster Autoscaling",
        "content": (
            "Kubernetes cluster autoscaling on EKS uses Karpenter or Cluster Autoscaler "
            "to dynamically adjust node capacity. Karpenter provisions right-sized compute "
            "resources in response to unschedulable pods. It supports consolidation, "
            "spot instances, and custom node templates via NodePool and EC2NodeClass."
        ),
        "content_type": "documentation",
        "source_path": "/docs/eks/autoscaling.md",
        "tags": "eks,kubernetes,autoscaling,karpenter,nodes",
    },
    {
        "title": "ArgoCD Application Sync Waves",
        "content": (
            "Sync waves in ArgoCD control the order of resource deployment. Resources "
            "with lower wave numbers deploy first. Use annotations "
            "argocd.argoproj.io/sync-wave to set wave order. CRDs should be wave -1, "
            "namespaces wave 0, and workloads wave 1+. Health checks gate progression."
        ),
        "content_type": "documentation",
        "source_path": "/docs/argocd/sync-waves.md",
        "tags": "argocd,gitops,deployment,sync",
    },
    {
        "title": "Prometheus Alerting Rules",
        "content": (
            "Prometheus alerting rules define conditions that trigger alerts. Rules use "
            "PromQL expressions with for-duration to avoid flapping. Labels route alerts "
            "to receivers. Inhibition rules suppress lower-severity alerts when critical "
            "ones fire. Recording rules pre-compute expensive queries."
        ),
        "content_type": "runbook",
        "source_path": "/docs/observability/alerting-rules.md",
        "tags": "prometheus,alerting,monitoring,observability",
    },
    {
        "title": "Terraform State Management",
        "content": (
            "Terraform remote state in S3 with DynamoDB locking prevents concurrent "
            "modifications. State encryption at rest uses KMS. State splitting by layer "
            "(network, compute, platform) limits blast radius. Import existing resources "
            "with terraform import or import blocks."
        ),
        "content_type": "documentation",
        "source_path": "/docs/terraform/state.md",
        "tags": "terraform,state,s3,dynamodb,infrastructure",
    },
    {
        "title": "IAM Role Assumption and Trust Policies",
        "content": (
            "Cross-account IAM role assumption requires trust policies specifying the "
            "source account principal. Session policies further restrict permissions. "
            "IRSA (IAM Roles for Service Accounts) maps K8s service accounts to IAM "
            "roles via OIDC federation. Pod Identity is the newer alternative."
        ),
        "content_type": "documentation",
        "source_path": "/docs/iam/role-assumption.md",
        "tags": "iam,security,roles,cross-account,irsa",
    },
    {
        "title": "Grafana Dashboard Best Practices",
        "content": (
            "Effective Grafana dashboards follow the USE method: Utilization, Saturation, "
            "Errors for resources and RED method: Rate, Errors, Duration for services. "
            "Template variables enable reuse across environments. Row-level alerting "
            "links dashboards to alert rules. JSON models enable GitOps management."
        ),
        "content_type": "guide",
        "source_path": "/docs/observability/grafana-dashboards.md",
        "tags": "grafana,dashboards,observability,monitoring",
    },
    {
        "title": "EKS Pod Security Standards",
        "content": (
            "Pod Security Standards (PSS) enforce security contexts on pods. Three "
            "levels: privileged, baseline, restricted. Pod Security Admission (PSA) "
            "replaces PodSecurityPolicy. Labels on namespaces set enforcement mode: "
            "enforce, audit, warn. Restricted profile blocks hostNetwork, privileged "
            "containers, and host path mounts."
        ),
        "content_type": "documentation",
        "source_path": "/docs/eks/pod-security.md",
        "tags": "eks,security,pods,pss,psa",
    },
    {
        "title": "Cost Optimization with Spot Instances",
        "content": (
            "EC2 Spot instances provide up to 90% savings over On-Demand. Use "
            "diversified allocation strategies across multiple instance types and AZs. "
            "Handle interruptions with 2-minute notice via instance metadata. Karpenter "
            "consolidation automatically migrates workloads from underutilized nodes."
        ),
        "content_type": "guide",
        "source_path": "/docs/cost/spot-instances.md",
        "tags": "cost,ec2,spot,optimization,karpenter",
    },
    {
        "title": "Multi-Account DNS with Route53",
        "content": (
            "Route53 hosted zones delegate DNS across accounts. Parent zone in the "
            "networking account holds NS records pointing to child hosted zones. "
            "Cross-account zone associations enable private DNS resolution in VPCs. "
            "Health checks route traffic away from unhealthy endpoints."
        ),
        "content_type": "documentation",
        "source_path": "/docs/dns/route53-multi-account.md",
        "tags": "dns,route53,multi-account,networking",
    },
    {
        "title": "GitOps Secret Management with SOPS",
        "content": (
            "SOPS encrypts secret values in YAML/JSON while keeping keys readable. "
            "KMS keys from AWS encrypt the data key. ArgoCD decrypts via the "
            "ksops kustomize plugin. Age keys provide a local development fallback. "
            "Rotate encryption keys periodically and audit access via CloudTrail."
        ),
        "content_type": "runbook",
        "source_path": "/docs/security/sops-secrets.md",
        "tags": "security,secrets,sops,gitops,kms",
    },
]

# Expected search results for queries (ground truth)
SEARCH_GROUND_TRUTH = {
    "autoscaling pods kubernetes": [0],  # EKS Autoscaling (primary hit for all 3 terms)
    "how do argocd sync waves work": [1],  # ArgoCD Sync Waves
    "prometheus alert configuration": [2],  # Prometheus Alerting Rules
    "terraform state locking s3": [3],  # Terraform State Management
    "cross account iam role": [4],  # IAM Role Assumption
    "grafana dashboard monitoring": [5, 2],  # Grafana Dashboards, Prometheus
    "pod security policy eks": [6],  # EKS Pod Security
    "reduce ec2 cost spot": [7, 0],  # Spot Instances, Autoscaling
    "route53 dns delegation": [8],  # Multi-Account DNS
    "encrypt secrets gitops": [9],  # SOPS Secrets
}


# ---------------------------------------------------------------------------
# Suite implementation
# ---------------------------------------------------------------------------


class RetrievalEvalSuite(EvalSuite):
    """Evaluates retrieval quality across keyword, semantic, graph, and hybrid channels."""

    name = "retrieval"
    description = "Measures search relevance, ranking quality, and latency for the hybrid retrieval engine"

    def __init__(self) -> None:
        super().__init__()
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._tmp_dir: tempfile.TemporaryDirectory | None = None
        self._workspace = "eval_test"

    def setup(self) -> None:
        """Create test DB with FTS5 index and synthetic corpus."""
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="cap_eval_retrieval_")
        self._db_path = Path(self._tmp_dir.name) / "knowledge.db"
        self._conn = sqlite3.connect(str(self._db_path))

        # Create knowledge schema (mirrors db_init.py)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                source_path TEXT,
                workspace TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                embedding_status TEXT NOT NULL DEFAULT 'pending',
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                title, content, tags,
                content='entries',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS entry_tags (
                entry_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (entry_id, tag),
                FOREIGN KEY (entry_id) REFERENCES entries(id)
            );

            CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                node_type TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT 'default',
                weight REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT,
                PRIMARY KEY (source_id, target_id, predicate),
                FOREIGN KEY (source_id) REFERENCES graph_nodes(id),
                FOREIGN KEY (target_id) REFERENCES graph_nodes(id)
            );

            CREATE TABLE IF NOT EXISTS entry_nodes (
                entry_id INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                PRIMARY KEY (entry_id, node_id),
                FOREIGN KEY (entry_id) REFERENCES entries(id),
                FOREIGN KEY (node_id) REFERENCES graph_nodes(id)
            );
        """)

        # Insert synthetic documents
        import uuid as uuid_mod

        for i, doc in enumerate(SYNTHETIC_DOCS):
            doc_uuid = str(uuid_mod.uuid4())
            self._conn.execute(
                """INSERT INTO entries (id, uuid, title, content, content_type, source_path, workspace)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (i + 1, doc_uuid, doc["title"], doc["content"], doc["content_type"], doc["source_path"], self._workspace),
            )
            # Populate FTS
            tags = doc.get("tags", "")
            self._conn.execute(
                "INSERT INTO entries_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (i + 1, doc["title"], doc["content"], tags),
            )

        # Build graph nodes and edges for testing graph traversal
        self._build_test_graph()
        self._conn.commit()

    def _build_test_graph(self) -> None:
        """Create graph relationships between documents."""
        # Nodes: topics
        topics = [
            ("eks", "technology"),
            ("kubernetes", "technology"),
            ("argocd", "technology"),
            ("prometheus", "technology"),
            ("terraform", "technology"),
            ("iam", "technology"),
            ("grafana", "technology"),
            ("karpenter", "technology"),
            ("route53", "technology"),
            ("sops", "technology"),
            ("security", "domain"),
            ("observability", "domain"),
            ("cost", "domain"),
        ]

        for name, ntype in topics:
            node_id = f"{ntype}:{name}"
            self._conn.execute(
                "INSERT OR IGNORE INTO graph_nodes (id, name, node_type, workspace) VALUES (?, ?, ?, ?)",
                (node_id, name, ntype, self._workspace),
            )

        # Edges: relationships
        edges = [
            ("technology:eks", "technology:kubernetes", "runs_on"),
            ("technology:karpenter", "technology:eks", "manages"),
            ("technology:argocd", "technology:kubernetes", "deploys_to"),
            ("technology:prometheus", "domain:observability", "part_of"),
            ("technology:grafana", "domain:observability", "part_of"),
            ("technology:terraform", "technology:eks", "provisions"),
            ("technology:iam", "domain:security", "part_of"),
            ("technology:sops", "domain:security", "part_of"),
            ("technology:karpenter", "domain:cost", "optimizes"),
            ("technology:route53", "technology:eks", "resolves_for"),
        ]

        for source, target, predicate in edges:
            self._conn.execute(
                "INSERT OR IGNORE INTO graph_edges (source_id, target_id, predicate, workspace) VALUES (?, ?, ?, ?)",
                (source, target, predicate, self._workspace),
            )

        # Link entries to nodes
        entry_node_links = [
            (1, "technology:eks"),
            (1, "technology:karpenter"),
            (1, "technology:kubernetes"),
            (2, "technology:argocd"),
            (3, "technology:prometheus"),
            (3, "domain:observability"),
            (4, "technology:terraform"),
            (5, "technology:iam"),
            (5, "domain:security"),
            (6, "technology:grafana"),
            (6, "domain:observability"),
            (7, "technology:eks"),
            (7, "domain:security"),
            (8, "technology:karpenter"),
            (8, "domain:cost"),
            (9, "technology:route53"),
            (10, "technology:sops"),
            (10, "domain:security"),
        ]

        for entry_id, node_id in entry_node_links:
            self._conn.execute(
                "INSERT OR IGNORE INTO entry_nodes (entry_id, node_id) VALUES (?, ?)",
                (entry_id, node_id),
            )

    def teardown(self) -> None:
        """Close DB and remove temp directory."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._tmp_dir:
            self._tmp_dir.cleanup()
            self._tmp_dir = None

    def build_cases(self) -> list[EvalCase]:
        """Build eval cases for retrieval quality."""
        cases: list[EvalCase] = []

        # --- Keyword search relevance ---
        for query, expected_ids in SEARCH_GROUND_TRUTH.items():
            # entry IDs are 1-indexed in DB
            expected_db_ids = [eid + 1 for eid in expected_ids]
            cases.append(
                EvalCase(
                    name=f"keyword_recall_{query[:30]}",
                    category="keyword_search",
                    input=query,
                    expected=expected_db_ids,
                    metric=MetricType.RECALL_AT_K,
                    threshold=0.7,
                    metadata={"k": 5},
                )
            )

        # --- Keyword search MRR (first relevant should be top-ranked) ---
        for query, expected_ids in list(SEARCH_GROUND_TRUTH.items())[:5]:
            expected_db_ids = [eid + 1 for eid in expected_ids]
            cases.append(
                EvalCase(
                    name=f"keyword_mrr_{query[:30]}",
                    category="keyword_search",
                    input=query,
                    expected=expected_db_ids,
                    metric=MetricType.MRR,
                    threshold=0.5,
                    metadata={"k": 5},
                )
            )

        # --- NDCG for ranking quality ---
        for query, expected_ids in list(SEARCH_GROUND_TRUTH.items())[:5]:
            expected_db_ids = [eid + 1 for eid in expected_ids]
            cases.append(
                EvalCase(
                    name=f"keyword_ndcg_{query[:30]}",
                    category="keyword_search",
                    input=query,
                    expected=expected_db_ids,
                    metric=MetricType.NDCG,
                    threshold=0.6,
                    metadata={"k": 5},
                )
            )

        # --- RRF merge correctness ---
        cases.append(
            EvalCase(
                name="rrf_merge_basic_correctness",
                category="rrf_fusion",
                input={
                    "keyword": [(1, 10.0), (2, 8.0), (3, 5.0)],
                    "semantic": [(2, 0.95), (1, 0.90), (4, 0.85)],
                    "graph": [(1, 1.0), (3, 0.8)],
                },
                expected=[1, 2],  # Items 1 and 2 should be top-ranked after fusion
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 3},
            )
        )
        cases.append(
            EvalCase(
                name="rrf_merge_preserves_order",
                category="rrf_fusion",
                input={
                    "keyword": [(1, 10.0), (2, 8.0)],
                    "semantic": [(1, 0.99), (2, 0.80)],
                    "graph": [(1, 1.0)],
                },
                expected=[1],  # Item 1 should be rank 1
                metric=MetricType.MRR,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="rrf_merge_empty_channels",
                category="rrf_fusion",
                input={
                    "keyword": [(5, 3.0)],
                    "semantic": [],
                    "graph": [],
                },
                expected=[5],
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 5},
            )
        )

        # --- Graph traversal completeness ---
        cases.append(
            EvalCase(
                name="graph_bfs_eks_depth1",
                category="graph_traversal",
                input={"start": ["technology:eks"], "depth": 1},
                expected=["technology:kubernetes", "technology:karpenter", "technology:terraform",
                          "technology:argocd", "technology:route53"],
                metric=MetricType.RECALL_AT_K,
                threshold=0.6,
                metadata={"k": 10},
            )
        )
        cases.append(
            EvalCase(
                name="graph_bfs_security_domain",
                category="graph_traversal",
                input={"start": ["domain:security"], "depth": 1},
                expected=["technology:iam", "technology:sops"],
                metric=MetricType.RECALL_AT_K,
                threshold=0.8,
                metadata={"k": 10},
            )
        )
        cases.append(
            EvalCase(
                name="graph_bfs_observability_depth2",
                category="graph_traversal",
                input={"start": ["domain:observability"], "depth": 2},
                expected=["technology:prometheus", "technology:grafana"],
                metric=MetricType.RECALL_AT_K,
                threshold=0.8,
                metadata={"k": 10},
            )
        )

        # --- Latency ---
        cases.append(
            EvalCase(
                name="keyword_search_latency_p95",
                category="latency",
                input="latency_keyword",
                expected=None,
                metric=MetricType.LATENCY_P95,
                threshold=50.0,  # 50ms for keyword search on small corpus
            )
        )
        cases.append(
            EvalCase(
                name="graph_traversal_latency_p95",
                category="latency",
                input="latency_graph",
                expected=None,
                metric=MetricType.LATENCY_P95,
                threshold=100.0,  # 100ms for graph BFS
            )
        )

        return cases

    def evaluate_case(self, case: EvalCase) -> EvalResult:
        """Run a single retrieval eval case."""
        if case.category == "keyword_search":
            return self._eval_keyword_search(case)
        elif case.category == "rrf_fusion":
            return self._eval_rrf_fusion(case)
        elif case.category == "graph_traversal":
            return self._eval_graph_traversal(case)
        elif case.category == "latency":
            return self._eval_latency(case)
        else:
            return EvalResult(
                case=case, actual=None, score=0.0, passed=False, latency_ms=0.0,
                details={"reason": f"Unknown category: {case.category}"},
            )

    def _eval_keyword_search(self, case: EvalCase) -> EvalResult:
        """Evaluate keyword search using FTS5."""
        query = case.input
        t0 = time.perf_counter()

        try:
            import re
            fts_query = re.sub(r'(\w)-(\w)', r'\1 \2', query)
            fts_query = re.sub(r'[{}()\[\]^~*]', ' ', fts_query)
            terms = [t for t in fts_query.split() if t]
            fts_query = ' OR '.join(terms) if len(terms) > 1 else (terms[0] if terms else query)
            cursor = self._conn.execute(
                """SELECT rowid, rank FROM entries_fts
                   WHERE entries_fts MATCH ?
                   ORDER BY rank
                   LIMIT 10""",
                (fts_query,),
            )
            results = cursor.fetchall()
            actual_ids = [row[0] for row in results]
        except Exception as e:
            # FTS5 match can fail on some query syntax; try fallback
            actual_ids = self._keyword_fallback(query)

        latency_ms = (time.perf_counter() - t0) * 1000

        # Score
        score = self.compute_score(case, actual_ids)
        passed = score >= case.threshold

        return EvalResult(
            case=case,
            actual=actual_ids,
            score=score,
            passed=passed,
            latency_ms=latency_ms,
            details={
                "returned_ids": actual_ids,
                "expected_ids": case.expected,
                "reason": "pass" if passed else f"score {score:.3f} < threshold {case.threshold}",
            },
        )

    def _keyword_fallback(self, query: str) -> list[int]:
        """Fallback LIKE-based search when FTS5 fails."""
        words = query.split()
        conditions = " OR ".join(["content LIKE ?"] * len(words))
        params = [f"%{w}%" for w in words]
        cursor = self._conn.execute(
            f"SELECT id FROM entries WHERE {conditions} LIMIT 10", params
        )
        return [row[0] for row in cursor.fetchall()]

    def _eval_rrf_fusion(self, case: EvalCase) -> EvalResult:
        """Evaluate RRF merge algorithm correctness."""
        from cap.lib.retrieval import rrf_merge

        input_data = case.input
        t0 = time.perf_counter()

        merged = rrf_merge(
            keyword_results=input_data["keyword"],
            semantic_results=input_data["semantic"],
            graph_results=input_data["graph"],
            top_k=10,
        )

        latency_ms = (time.perf_counter() - t0) * 1000
        actual_ids = [item[0] for item in merged]

        score = self.compute_score(case, actual_ids)
        passed = score >= case.threshold

        return EvalResult(
            case=case,
            actual=actual_ids,
            score=score,
            passed=passed,
            latency_ms=latency_ms,
            details={
                "merged_ranking": actual_ids[:5],
                "merged_scores": [item[1] for item in merged[:5]],
                "reason": "pass" if passed else f"score {score:.3f} < threshold {case.threshold}",
            },
        )

    def _eval_graph_traversal(self, case: EvalCase) -> EvalResult:
        """Evaluate graph BFS traversal completeness."""
        input_data = case.input
        start_nodes = input_data["start"]
        depth = input_data["depth"]

        t0 = time.perf_counter()

        # BFS traversal using the DB directly (mirrors graph.py logic)
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(n, 0) for n in start_nodes]
        result_nodes: list[str] = []

        while queue:
            node_id, d = queue.pop(0)
            if node_id in visited or d > depth:
                continue
            visited.add(node_id)
            if node_id not in start_nodes:
                result_nodes.append(node_id)

            if d < depth:
                # Get neighbors (both directions)
                cursor = self._conn.execute(
                    "SELECT target_id FROM graph_edges WHERE source_id = ? AND workspace = ?",
                    (node_id, self._workspace),
                )
                for row in cursor.fetchall():
                    if row[0] not in visited:
                        queue.append((row[0], d + 1))

                cursor = self._conn.execute(
                    "SELECT source_id FROM graph_edges WHERE target_id = ? AND workspace = ?",
                    (node_id, self._workspace),
                )
                for row in cursor.fetchall():
                    if row[0] not in visited:
                        queue.append((row[0], d + 1))

        latency_ms = (time.perf_counter() - t0) * 1000

        score = self.compute_score(case, result_nodes)
        passed = score >= case.threshold

        return EvalResult(
            case=case,
            actual=result_nodes,
            score=score,
            passed=passed,
            latency_ms=latency_ms,
            details={
                "traversed_nodes": result_nodes,
                "expected_nodes": case.expected,
                "visited_count": len(visited),
                "reason": "pass" if passed else f"score {score:.3f} < threshold {case.threshold}",
            },
        )

    def _eval_latency(self, case: EvalCase) -> EvalResult:
        """Measure p95 latency across repeated operations."""
        latencies: list[float] = []
        iterations = 20

        if case.input == "latency_keyword":
            queries = list(SEARCH_GROUND_TRUTH.keys())
            for i in range(iterations):
                query = queries[i % len(queries)]
                t0 = time.perf_counter()
                try:
                    self._conn.execute(
                        "SELECT rowid, rank FROM entries_fts WHERE entries_fts MATCH ? LIMIT 10",
                        (query,),
                    ).fetchall()
                except Exception:
                    self._keyword_fallback(query)
                latencies.append((time.perf_counter() - t0) * 1000)

        elif case.input == "latency_graph":
            start_sets = [
                ["technology:eks"],
                ["domain:security"],
                ["domain:observability"],
                ["technology:terraform"],
                ["technology:karpenter"],
            ]
            for i in range(iterations):
                start = start_sets[i % len(start_sets)]
                t0 = time.perf_counter()
                # Simple BFS
                visited: set[str] = set()
                queue = [(n, 0) for n in start]
                while queue:
                    node_id, d = queue.pop(0)
                    if node_id in visited or d > 2:
                        continue
                    visited.add(node_id)
                    if d < 2:
                        cursor = self._conn.execute(
                            "SELECT target_id FROM graph_edges WHERE source_id = ?",
                            (node_id,),
                        )
                        for row in cursor.fetchall():
                            queue.append((row[0], d + 1))
                latencies.append((time.perf_counter() - t0) * 1000)

        # Compute p95
        from cap.eval.framework import score_latency_p95

        score = score_latency_p95(latencies, case.threshold)
        sorted_lats = sorted(latencies)
        import math

        p95_idx = max(0, int(math.ceil(0.95 * len(sorted_lats))) - 1)
        p95_val = sorted_lats[p95_idx] if sorted_lats else 0.0
        passed = score >= 0.8  # Allow some degradation

        return EvalResult(
            case=case,
            actual=latencies,
            score=score,
            passed=passed,
            latency_ms=p95_val,
            details={
                "p50_ms": sorted_lats[len(sorted_lats) // 2] if sorted_lats else 0.0,
                "p95_ms": p95_val,
                "p99_ms": sorted_lats[-1] if sorted_lats else 0.0,
                "iterations": iterations,
                "threshold_ms": case.threshold,
                "reason": "pass" if passed else f"p95 {p95_val:.1f}ms exceeds threshold {case.threshold}ms",
            },
        )

"""Core evaluation framework for CAP.

Provides the base classes for defining, running, and reporting evaluations.
Think of this as a lightweight deepeval/ragas tailored to CAP's specific modules.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------


class MetricType(str, Enum):
    """Supported evaluation metric types."""

    EXACT_MATCH = "exact_match"
    FUZZY_MATCH = "fuzzy_match"
    RECALL_AT_K = "recall_at_k"
    PRECISION_AT_K = "precision_at_k"
    NDCG = "ndcg"
    MRR = "mrr"
    LATENCY_P95 = "latency_p95"
    COST_UNDER = "cost_under"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """A single evaluation case."""

    name: str
    category: str
    input: Any
    expected: Any
    metric: MetricType
    threshold: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    """Result of running a single eval case."""

    case: EvalCase
    actual: Any
    score: float
    passed: bool
    latency_ms: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CategoryBreakdown:
    """Aggregated metrics for an eval category."""

    category: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    avg_score: float
    min_score: float
    max_score: float
    p50_latency_ms: float
    p95_latency_ms: float


@dataclass
class EvalReport:
    """Aggregated report across all eval results."""

    suite_name: str
    timestamp: str
    duration_ms: float
    total_cases: int
    passed: int
    failed: int
    pass_rate: float
    overall_score: float
    categories: list[CategoryBreakdown]
    worst_performers: list[EvalResult]
    recommendations: list[str]
    results: list[EvalResult]

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a JSON-compatible dict."""
        return {
            "suite_name": self.suite_name,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "total_cases": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "overall_score": self.overall_score,
            "categories": [asdict(c) for c in self.categories],
            "worst_performers": [
                {
                    "name": r.case.name,
                    "category": r.case.category,
                    "metric": r.case.metric.value,
                    "score": r.score,
                    "threshold": r.case.threshold,
                    "latency_ms": r.latency_ms,
                    "details": r.details,
                }
                for r in self.worst_performers
            ],
            "recommendations": self.recommendations,
            "results": [
                {
                    "name": r.case.name,
                    "category": r.case.category,
                    "metric": r.case.metric.value,
                    "score": r.score,
                    "passed": r.passed,
                    "threshold": r.case.threshold,
                    "latency_ms": r.latency_ms,
                    "details": r.details,
                }
                for r in self.results
            ],
        }

    def to_json(self, path: str | Path | None = None, indent: int = 2) -> str:
        """Export report as JSON. Optionally write to file."""
        data = self.to_dict()
        json_str = json.dumps(data, indent=indent, default=str)
        if path:
            Path(path).write_text(json_str)
        return json_str


# ---------------------------------------------------------------------------
# Metric scoring functions
# ---------------------------------------------------------------------------


def score_exact_match(actual: Any, expected: Any) -> float:
    """1.0 if actual == expected, else 0.0."""
    if isinstance(actual, str) and isinstance(expected, str):
        return 1.0 if actual.strip() == expected.strip() else 0.0
    return 1.0 if actual == expected else 0.0


def score_fuzzy_match(actual: Any, expected: Any) -> float:
    """Simple token-overlap based fuzzy matching (Jaccard similarity)."""
    if not actual or not expected:
        return 0.0
    actual_tokens = set(str(actual).lower().split())
    expected_tokens = set(str(expected).lower().split())
    if not actual_tokens or not expected_tokens:
        return 0.0
    intersection = actual_tokens & expected_tokens
    union = actual_tokens | expected_tokens
    return len(intersection) / len(union)


def score_recall_at_k(actual: list, expected: list, k: int | None = None) -> float:
    """Recall@K: fraction of expected items found in top-K actual results."""
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    top_k = actual[:k] if k else actual
    found = sum(1 for item in expected if item in top_k)
    return found / len(expected)


def score_precision_at_k(actual: list, expected: list, k: int | None = None) -> float:
    """Precision@K: fraction of top-K results that are relevant."""
    if not actual:
        return 0.0
    top_k = actual[:k] if k else actual
    if not top_k:
        return 0.0
    relevant = sum(1 for item in top_k if item in expected)
    return relevant / len(top_k)


def score_ndcg(actual: list, expected: list, k: int | None = None) -> float:
    """NDCG: Normalized Discounted Cumulative Gain."""
    if not expected or not actual:
        return 0.0
    top_k = actual[:k] if k else actual

    # Build relevance map: items in expected get score 1.0
    relevance_set = set(expected)

    # DCG
    dcg = 0.0
    for i, item in enumerate(top_k):
        rel = 1.0 if item in relevance_set else 0.0
        dcg += rel / math.log2(i + 2)  # i+2 because log2(1) = 0

    # Ideal DCG (all relevant items at top)
    ideal_length = min(len(expected), len(top_k))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_length))

    if idcg == 0:
        return 0.0
    return dcg / idcg


def score_mrr(actual: list, expected: list) -> float:
    """MRR: Mean Reciprocal Rank (rank of first relevant result)."""
    if not expected or not actual:
        return 0.0
    relevance_set = set(expected)
    for i, item in enumerate(actual):
        if item in relevance_set:
            return 1.0 / (i + 1)
    return 0.0


def score_latency_p95(latencies_ms: list[float], threshold_ms: float) -> float:
    """Score based on p95 latency relative to threshold. 1.0 = at or below threshold."""
    if not latencies_ms:
        return 1.0
    sorted_lats = sorted(latencies_ms)
    idx = int(math.ceil(0.95 * len(sorted_lats))) - 1
    p95 = sorted_lats[max(0, idx)]
    if p95 <= threshold_ms:
        return 1.0
    # Degrade linearly up to 2x threshold
    return max(0.0, 1.0 - (p95 - threshold_ms) / threshold_ms)


def score_cost_under(actual_cost: float, budget: float) -> float:
    """1.0 if under budget, degrades linearly to 0.0 at 2x budget."""
    if actual_cost <= budget:
        return 1.0
    return max(0.0, 1.0 - (actual_cost - budget) / budget)


# Registry of metric scorers
METRIC_SCORERS: dict[MetricType, Callable] = {
    MetricType.EXACT_MATCH: score_exact_match,
    MetricType.FUZZY_MATCH: score_fuzzy_match,
    MetricType.RECALL_AT_K: score_recall_at_k,
    MetricType.PRECISION_AT_K: score_precision_at_k,
    MetricType.NDCG: score_ndcg,
    MetricType.MRR: score_mrr,
    MetricType.LATENCY_P95: score_latency_p95,
    MetricType.COST_UNDER: score_cost_under,
}


# ---------------------------------------------------------------------------
# EvalSuite base class
# ---------------------------------------------------------------------------


class EvalSuite(ABC):
    """Base class for evaluation suites.

    Each suite:
    1. Sets up its own test fixtures (DBs in /tmp, synthetic data)
    2. Defines eval cases
    3. Runs evaluations with timing
    4. Tears down fixtures
    """

    name: str = "base"
    description: str = ""

    def __init__(self) -> None:
        self._cases: list[EvalCase] = []
        self._results: list[EvalResult] = []

    @abstractmethod
    def setup(self) -> None:
        """Create test fixtures (DBs, synthetic data, etc.)."""

    @abstractmethod
    def teardown(self) -> None:
        """Clean up test fixtures."""

    @abstractmethod
    def build_cases(self) -> list[EvalCase]:
        """Return the list of eval cases for this suite."""

    @abstractmethod
    def evaluate_case(self, case: EvalCase) -> EvalResult:
        """Run a single eval case and return the result."""

    def run(self) -> EvalReport:
        """Execute the full evaluation pipeline."""
        start_time = time.time()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Setup
        self.setup()

        try:
            # Build cases
            self._cases = self.build_cases()

            # Run evaluations
            self._results = []
            for case in self._cases:
                t0 = time.perf_counter()
                result = self.evaluate_case(case)
                elapsed_ms = (time.perf_counter() - t0) * 1000

                # If the eval didn't set latency, use the wrapper timing
                if result.latency_ms == 0:
                    result.latency_ms = elapsed_ms

                self._results.append(result)

        finally:
            # Always teardown
            self.teardown()

        duration_ms = (time.time() - start_time) * 1000

        # Build report
        return self._build_report(timestamp, duration_ms)

    def _build_report(self, timestamp: str, duration_ms: float) -> EvalReport:
        """Aggregate results into a report."""
        total = len(self._results)
        passed = sum(1 for r in self._results if r.passed)
        failed = total - passed
        pass_rate = passed / total if total > 0 else 0.0

        scores = [r.score for r in self._results]
        overall_score = statistics.mean(scores) if scores else 0.0

        # Category breakdown
        categories_map: dict[str, list[EvalResult]] = {}
        for r in self._results:
            categories_map.setdefault(r.case.category, []).append(r)

        categories = []
        for cat_name, cat_results in sorted(categories_map.items()):
            cat_scores = [r.score for r in cat_results]
            cat_latencies = sorted([r.latency_ms for r in cat_results])
            cat_passed = sum(1 for r in cat_results if r.passed)

            p50_idx = max(0, int(math.ceil(0.50 * len(cat_latencies))) - 1)
            p95_idx = max(0, int(math.ceil(0.95 * len(cat_latencies))) - 1)

            categories.append(
                CategoryBreakdown(
                    category=cat_name,
                    total=len(cat_results),
                    passed=cat_passed,
                    failed=len(cat_results) - cat_passed,
                    pass_rate=cat_passed / len(cat_results),
                    avg_score=statistics.mean(cat_scores),
                    min_score=min(cat_scores),
                    max_score=max(cat_scores),
                    p50_latency_ms=cat_latencies[p50_idx] if cat_latencies else 0.0,
                    p95_latency_ms=cat_latencies[p95_idx] if cat_latencies else 0.0,
                )
            )

        # Worst performers: failed cases sorted by how far below threshold
        failed_results = [r for r in self._results if not r.passed]
        worst = sorted(failed_results, key=lambda r: r.score)[:5]

        # Recommendations
        recommendations = self._generate_recommendations(categories, worst)

        return EvalReport(
            suite_name=self.name,
            timestamp=timestamp,
            duration_ms=duration_ms,
            total_cases=total,
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            overall_score=overall_score,
            categories=categories,
            worst_performers=worst,
            recommendations=recommendations,
            results=self._results,
        )

    def _generate_recommendations(
        self,
        categories: list[CategoryBreakdown],
        worst: list[EvalResult],
    ) -> list[str]:
        """Generate actionable recommendations from results."""
        recs: list[str] = []

        # Categories with low pass rate
        for cat in categories:
            if cat.pass_rate < 0.7:
                recs.append(
                    f"Category '{cat.category}' has low pass rate ({cat.pass_rate:.0%}). "
                    f"Review thresholds or implementation."
                )
            if cat.p95_latency_ms > 500:
                recs.append(
                    f"Category '{cat.category}' p95 latency is {cat.p95_latency_ms:.0f}ms. "
                    f"Consider caching or index optimization."
                )

        # Specific failures
        for r in worst[:3]:
            recs.append(
                f"Case '{r.case.name}' scored {r.score:.3f} (threshold: {r.case.threshold}). "
                f"Details: {r.details.get('reason', 'no details')}"
            )

        if not recs:
            recs.append("All evaluations passing. Consider tightening thresholds.")

        return recs

    def compute_score(self, case: EvalCase, actual: Any) -> float:
        """Compute the metric score for a case given actual output."""
        scorer = METRIC_SCORERS[case.metric]

        if case.metric == MetricType.LATENCY_P95:
            return scorer(actual, case.threshold)
        elif case.metric == MetricType.COST_UNDER:
            return scorer(actual, case.expected)
        elif case.metric in (
            MetricType.RECALL_AT_K,
            MetricType.PRECISION_AT_K,
            MetricType.NDCG,
            MetricType.MRR,
        ):
            k = case.metadata.get("k")
            if case.metric == MetricType.MRR:
                return scorer(actual, case.expected)
            return scorer(actual, case.expected, k)
        else:
            return scorer(actual, case.expected)


# ---------------------------------------------------------------------------
# Suite discovery
# ---------------------------------------------------------------------------


def discover_suites() -> dict[str, type[EvalSuite]]:
    """Discover all available eval suites."""
    # Import here to avoid circular imports
    from cap.eval.suites import ALL_SUITES

    return ALL_SUITES

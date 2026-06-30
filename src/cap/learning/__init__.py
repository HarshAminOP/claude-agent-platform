"""
CAP Self-Learning System.

Modules:
- engine: Core learning engine (routing decisions, outcomes, corrections, thresholds)
- trust: Progressive trust manager (Bayesian trust levels, autonomy decisions)
- feedback: Retrieval feedback loop (boost used results, decay unused)
"""

from cap.learning.engine import (
    CORRECTION_THRESHOLD,
    RoutingDecision,
    RoutingRecord,
    auto_generate_baseline,
    get_learned_thresholds,
    get_trust,
    record_correction,
    record_outcome,
    record_routing,
    retrieval_feedback,
    update_trust,
)
from cap.learning.feedback import (
    apply_adjustments,
    compute_relevance_adjustments,
    record_retrieval,
    record_usage,
)
from cap.learning.trust import (
    AUTO_APPROVE_THRESHOLD,
    DENY_THRESHOLD,
    TrustManager,
)

__all__ = [
    # engine
    "CORRECTION_THRESHOLD",
    "RoutingDecision",
    "RoutingRecord",
    "auto_generate_baseline",
    "get_learned_thresholds",
    "get_trust",
    "record_correction",
    "record_outcome",
    "record_routing",
    "retrieval_feedback",
    "update_trust",
    # trust
    "AUTO_APPROVE_THRESHOLD",
    "DENY_THRESHOLD",
    "TrustManager",
    # feedback
    "apply_adjustments",
    "compute_relevance_adjustments",
    "record_retrieval",
    "record_usage",
]

"""
CAP Reliability Layer — circuit breakers, dead-letter queue, cascade detection.

Provides fault tolerance primitives for the orchestration dispatch loop:
- CircuitBreaker: per-agent-type circuit breaker (CLOSED/OPEN/HALF_OPEN)
- Dead-Letter Queue: stores tasks that exhausted all retries for user review
- Cascade Detection: detects systemic failures across multiple agents

Reference: CAP System Design Section 16C.
"""

from .circuit_breaker import CircuitBreaker
from .dlq import enqueue_dead_letter, list_dlq, retry_task, dismiss_task, cleanup_expired
from .cascade import detect_cascade, handle_cascade, get_failure_pattern

__all__ = [
    "CircuitBreaker",
    "enqueue_dead_letter",
    "list_dlq",
    "retry_task",
    "dismiss_task",
    "cleanup_expired",
    "detect_cascade",
    "handle_cascade",
    "get_failure_pattern",
]

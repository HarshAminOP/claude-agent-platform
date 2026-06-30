"""
Failure Quorum for CAP orchestration.

When multiple agents are working on a task and some fail, the quorum
mechanism determines if the task is genuinely blocked (2/3+ agents
report failure) vs. partially successful.

Reference: CAP System Design Section 9 — Failure Handling.

Cascade:
  1. Agent fails -> retry once with same model
  2. Second failure -> upgrade model (sonnet -> opus)
  3. Third failure -> check failure quorum
  4. Quorum (2/3 agents agree task is blocked) -> escalate to PO
  5. Non-quorum -> report partial results + what failed
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_QUORUM_THRESHOLD = 2 / 3


@dataclass
class AgentResult:
    """Result from a single agent's execution."""

    agent_type: str
    status: str  # success, failed, partial
    outputs: list[str] = field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 1

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "status": self.status,
            "outputs": self.outputs,
            "error": self.error,
            "attempts": self.attempts,
        }


@dataclass
class QuorumSummary:
    """Aggregated summary of agent results for escalation."""

    total_agents: int
    succeeded: list[AgentResult]
    failed: list[AgentResult]
    quorum_reached: bool
    failure_ratio: float
    common_errors: list[str]

    def to_dict(self) -> dict:
        return {
            "total_agents": self.total_agents,
            "succeeded_count": len(self.succeeded),
            "failed_count": len(self.failed),
            "quorum_reached": self.quorum_reached,
            "failure_ratio": round(self.failure_ratio, 2),
            "common_errors": self.common_errors,
            "succeeded_agents": [r.agent_type for r in self.succeeded],
            "failed_agents": [r.agent_type for r in self.failed],
        }


def check_failure_quorum(
    results: list[AgentResult],
    threshold: float = DEFAULT_QUORUM_THRESHOLD,
) -> bool:
    """
    Determine if a failure quorum has been reached.

    If 2/3 or more agents report failure, the task is genuinely blocked.

    Args:
        results: List of AgentResult from all agents in the orchestration.
        threshold: Fraction of agents that must fail for quorum (default 0.67).

    Returns:
        True if failure quorum is reached (task is blocked).
    """
    if not results:
        return False

    total = len(results)
    failures = sum(1 for r in results if r.failed)

    if total < 2:
        # Need at least 2 agents to form a quorum
        return False

    ratio = failures / total
    quorum_met = ratio >= threshold

    if quorum_met:
        logger.warning(
            "Failure quorum reached: %d/%d agents failed (%.0f%% >= %.0f%% threshold)",
            failures,
            total,
            ratio * 100,
            threshold * 100,
        )
    else:
        logger.info(
            "Failure quorum NOT reached: %d/%d agents failed (%.0f%% < %.0f%% threshold)",
            failures,
            total,
            ratio * 100,
            threshold * 100,
        )

    return quorum_met


def aggregate_results(results: list[AgentResult]) -> QuorumSummary:
    """
    Aggregate agent results into a structured summary.

    Categorizes successes and failures, extracts common error patterns,
    and computes the failure ratio.

    Args:
        results: List of AgentResult from all agents.

    Returns:
        QuorumSummary with categorized results and analysis.
    """
    succeeded = [r for r in results if r.succeeded]
    failed = [r for r in results if r.failed]
    total = len(results)

    failure_ratio = len(failed) / total if total > 0 else 0.0

    # Extract common error patterns
    common_errors = _extract_common_errors(failed)

    quorum_reached = check_failure_quorum(results)

    return QuorumSummary(
        total_agents=total,
        succeeded=succeeded,
        failed=failed,
        quorum_reached=quorum_reached,
        failure_ratio=failure_ratio,
        common_errors=common_errors,
    )


def _extract_common_errors(failed_results: list[AgentResult]) -> list[str]:
    """
    Extract common error messages from failed results.

    Groups similar errors and returns deduplicated list.
    """
    if not failed_results:
        return []

    errors: list[str] = []
    seen_patterns: set[str] = set()

    for result in failed_results:
        if not result.error:
            continue
        # Normalize: take first 100 chars as a pattern key
        pattern = result.error[:100].lower().strip()
        if pattern not in seen_patterns:
            seen_patterns.add(pattern)
            errors.append(result.error)

    return errors


def escalate_to_user(summary: QuorumSummary) -> str:
    """
    Format a quorum failure summary for PO escalation.

    Produces a clear, concise message explaining what succeeded,
    what failed, and why the task is blocked.

    Args:
        summary: QuorumSummary from aggregate_results().

    Returns:
        Formatted string suitable for displaying to the Product Owner.
    """
    lines = [
        "TASK BLOCKED — Failure quorum reached",
        f"  {summary.total_agents} agents dispatched, "
        f"{len(summary.failed)} failed ({summary.failure_ratio:.0%})",
        "",
    ]

    if summary.succeeded:
        lines.append("Succeeded:")
        for r in summary.succeeded:
            output_preview = r.outputs[0][:100] if r.outputs else "(no output)"
            lines.append(f"  - {r.agent_type}: {output_preview}")
        lines.append("")

    if summary.failed:
        lines.append("Failed:")
        for r in summary.failed:
            error_preview = r.error[:150] if r.error else "(no error detail)"
            lines.append(f"  - {r.agent_type} (attempts={r.attempts}): {error_preview}")
        lines.append("")

    if summary.common_errors:
        lines.append("Common blocking reasons:")
        for err in summary.common_errors[:3]:
            lines.append(f"  * {err[:200]}")
        lines.append("")

    lines.append("Action required: resolve the blocking issue or provide alternative direction.")

    return "\n".join(lines)

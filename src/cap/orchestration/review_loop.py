"""
Review Loop for CAP orchestration.

Implements iterative review: implementation is sent to reviewers,
findings are collected, and if critical issues are found the implementation
is sent back for revision — up to max_iterations.

Reference: CAP System Design Section 9 — REVIEW_LOOP_MAX = 3.

Flow:
  1. Send implementation result to reviewer agents
  2. Collect findings (each reviewer produces a list of findings with severity)
  3. If any CRITICAL findings exist: send back to implementer with findings
  4. Repeat until: no critical findings OR max_iterations reached
  5. Return FinalResult with all iterations tracked
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 3


@dataclass
class Finding:
    """A single review finding from a reviewer agent."""

    reviewer: str
    severity: str  # critical, high, medium, low
    description: str
    file_path: Optional[str] = None
    line: Optional[int] = None
    suggestion: Optional[str] = None

    @property
    def is_critical(self) -> bool:
        return self.severity in ("critical", "high")

    def to_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "severity": self.severity,
            "description": self.description,
            "file_path": self.file_path,
            "line": self.line,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        return cls(
            reviewer=data["reviewer"],
            severity=data["severity"],
            description=data["description"],
            file_path=data.get("file_path"),
            line=data.get("line"),
            suggestion=data.get("suggestion"),
        )


@dataclass
class IterationRecord:
    """Record of a single review iteration."""

    iteration: int
    findings: list[Finding]
    critical_count: int
    passed: bool
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "findings": [f.to_dict() for f in self.findings],
            "critical_count": self.critical_count,
            "passed": self.passed,
            "timestamp": self.timestamp,
        }


@dataclass
class FinalResult:
    """Final result of the review loop."""

    implementation: dict[str, Any]
    iterations: list[IterationRecord]
    passed: bool
    total_iterations: int
    remaining_findings: list[Finding]

    @property
    def max_iterations_reached(self) -> bool:
        return not self.passed and self.total_iterations >= DEFAULT_MAX_ITERATIONS

    def to_dict(self) -> dict:
        return {
            "implementation": self.implementation,
            "iterations": [i.to_dict() for i in self.iterations],
            "passed": self.passed,
            "total_iterations": self.total_iterations,
            "remaining_findings": [f.to_dict() for f in self.remaining_findings],
            "max_iterations_reached": self.max_iterations_reached,
        }


class ReviewLoop:
    """
    Iterative review loop that sends implementation to reviewers
    and routes critical findings back to the implementer for revision.

    Args:
        review_fn: Async callable (implementation, reviewer_type) -> list[Finding].
                   Dispatches a reviewer agent and parses its output into findings.
        revise_fn: Async callable (implementation, findings) -> revised implementation dict.
                   Dispatches the implementer agent with findings to produce a revision.
        max_iterations: Maximum number of review-revise cycles (default 3).
    """

    def __init__(
        self,
        review_fn: Callable[[dict, str], Awaitable[list[Finding]]],
        revise_fn: Callable[[dict, list[Finding]], Awaitable[dict]],
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ):
        self.review_fn = review_fn
        self.revise_fn = revise_fn
        self.max_iterations = max_iterations
        self.iterations: list[IterationRecord] = []

    async def run(
        self,
        implementation_result: dict[str, Any],
        reviewers: list[str],
    ) -> FinalResult:
        """
        Execute the review loop.

        Args:
            implementation_result: The initial implementation output from the dev agent.
            reviewers: List of reviewer agent types (e.g., ["code-review", "security"]).

        Returns:
            FinalResult with the final implementation and all iteration records.
        """
        current_impl = implementation_result

        for iteration_num in range(1, self.max_iterations + 1):
            logger.info(
                "Review loop iteration %d/%d with reviewers: %s",
                iteration_num,
                self.max_iterations,
                reviewers,
            )

            # Collect findings from all reviewers
            all_findings = await self._collect_reviews(current_impl, reviewers)

            # Count critical findings
            critical_findings = [f for f in all_findings if f.is_critical]
            critical_count = len(critical_findings)

            # Record this iteration
            passed = critical_count == 0
            record = IterationRecord(
                iteration=iteration_num,
                findings=all_findings,
                critical_count=critical_count,
                passed=passed,
            )
            self.iterations.append(record)

            logger.info(
                "Iteration %d: %d findings (%d critical) — %s",
                iteration_num,
                len(all_findings),
                critical_count,
                "PASSED" if passed else "NEEDS REVISION",
            )

            # If no critical findings, we're done
            if passed:
                return FinalResult(
                    implementation=current_impl,
                    iterations=self.iterations,
                    passed=True,
                    total_iterations=iteration_num,
                    remaining_findings=[f for f in all_findings if not f.is_critical],
                )

            # If this is the last iteration, don't attempt revision
            if iteration_num >= self.max_iterations:
                logger.warning(
                    "Max iterations (%d) reached with %d critical findings remaining",
                    self.max_iterations,
                    critical_count,
                )
                return FinalResult(
                    implementation=current_impl,
                    iterations=self.iterations,
                    passed=False,
                    total_iterations=iteration_num,
                    remaining_findings=critical_findings,
                )

            # Send back to implementer with findings for revision
            logger.info(
                "Sending %d critical findings back to implementer for revision",
                critical_count,
            )
            current_impl = await self.revise_fn(current_impl, critical_findings)

        # Should not reach here, but handle gracefully
        return FinalResult(
            implementation=current_impl,
            iterations=self.iterations,
            passed=False,
            total_iterations=self.max_iterations,
            remaining_findings=[],
        )

    async def _collect_reviews(
        self,
        implementation: dict[str, Any],
        reviewers: list[str],
    ) -> list[Finding]:
        """
        Dispatch all reviewers and collect their findings.

        Reviewers are dispatched sequentially to avoid context conflicts.
        Each reviewer receives the current implementation state.
        """
        all_findings: list[Finding] = []

        for reviewer_type in reviewers:
            try:
                findings = await self.review_fn(implementation, reviewer_type)
                all_findings.extend(findings)
                logger.debug(
                    "Reviewer '%s' produced %d findings",
                    reviewer_type,
                    len(findings),
                )
            except Exception as e:
                logger.error(
                    "Reviewer '%s' failed: %s — treating as no findings",
                    reviewer_type,
                    e,
                )
                # A failed reviewer does not block the loop;
                # it simply produces no findings for this iteration.

        return all_findings

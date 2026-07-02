"""Coordination engine — executes TaskDAG plans with ordering, parallelism, and agent communication.

Takes a TaskDAG (from cap.orchestration.dag) and executes it by:
- Respecting dependency ordering (a step only runs when all its deps are COMPLETED)
- Running independent steps in parallel (asyncio.gather, capped by max_parallel semaphore)
- Feeding completed step outputs into dependent steps as context
- Publishing findings to SharedState after each step
- Gracefully handling failures (failed steps cascade skips to dependents)
- Tracking budget exhaustion (stops remaining steps when ConverseExecutor reports budget error)
- Synthesising a final coherent response from all step outputs (template-based, no extra LLM call)

DB persistence:
- Updates task_steps.state (pending → running → completed/failed/skipped)
- Updates task_steps.started_at, completed_at, result_json

Note: The public type alias ``TaskPlan = TaskDAG`` is provided so callers that
      construct plans independently can use either name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from cap.orchestration.dag import StepState, TaskDAG, TaskStep
from cap.lib.agent_context import SharedState
from cap.db import get_db

logger = logging.getLogger("cap.lib.coordination_engine")

# Public alias so callers may write ``TaskPlan`` without importing the dag module.
TaskPlan = TaskDAG

# Budget-related error substrings returned by ConverseExecutor
_BUDGET_ERROR_MARKERS = (
    "budget exceeded",
    "budget paused",
    "per-agent cap exceeded",
    "daily budget exceeded",
)


def _is_budget_error(error: Optional[str]) -> bool:
    """Return True when a ConverseExecutor error string indicates budget exhaustion."""
    if not error:
        return False
    lower = error.lower()
    return any(marker in lower for marker in _BUDGET_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Result of executing a single step within a TaskPlan."""

    step_id: str
    agent_type: str
    status: str  # "completed" | "failed" | "skipped"
    response: Optional[str] = None
    error: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    output_summary: str = ""  # Brief summary forwarded to dependent steps as context


@dataclass
class CoordinationResult:
    """Result of executing an entire TaskPlan."""

    workflow_id: str
    status: str  # "completed" | "partial" | "failed"
    steps: list[StepResult]
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    final_response: Optional[str] = None  # Synthesised response from all steps
    errors: list[str] = field(default_factory=list)

    @property
    def completed_steps(self) -> list[StepResult]:
        """All steps that finished with status 'completed'."""
        return [s for s in self.steps if s.status == "completed"]

    @property
    def failed_steps(self) -> list[StepResult]:
        """All steps that finished with status 'failed'."""
        return [s for s in self.steps if s.status == "failed"]


# ---------------------------------------------------------------------------
# CoordinationEngine
# ---------------------------------------------------------------------------


class CoordinationEngine:
    """Executes multi-step TaskDAG plans with agent coordination.

    Handles:
    - Dependency ordering: a step only becomes eligible when all deps are COMPLETED.
    - Parallel execution: independent steps run concurrently via asyncio.gather,
      bounded by a semaphore (max_parallel).
    - Output passing: completed step output_summary is prepended to dependent steps
      as context so each agent benefits from prior results.
    - Shared state: each completed step stores its key findings under
      ``step.{step_id}.result`` in SharedState.
    - Bus publishing: each completed step publishes to
      ``status.{agent_type}.{step_id}`` and ``findings.{agent_type}`` in SharedState.
    - Failure cascading: when a step fails, all transitive dependents are marked
      SKIPPED immediately so the loop terminates without wasted work.
    - Budget awareness: a budget error from ConverseExecutor causes all remaining
      pending/ready steps to be skipped.
    - Response synthesis: combines step outputs into a final coherent response using
      a template (not an additional LLM call — kept cheap).
    - DB persistence: updates task_steps rows (state, started_at, completed_at,
      result_json) throughout execution.
    """

    def __init__(
        self,
        executor: object,  # ConverseExecutor
        bus: Optional[object] = None,  # AgentBus — not used directly; SharedState carries messages
        shared: Optional[SharedState] = None,
        max_parallel: int = 3,
        db_path: Optional[str] = None,
    ) -> None:
        """Initialise CoordinationEngine.

        Args:
            executor: ConverseExecutor instance for running individual agents.
            bus: Optional AgentBus (currently reserved; communication uses SharedState).
            shared: SharedState instance for cross-agent data sharing. A transient
                in-memory SharedState is created per plan when None is provided.
            max_parallel: Maximum steps to execute concurrently. Protects against
                budget runaway and connection exhaustion. Default 3.
            db_path: Override SQLite path for testing. Defaults to CAP_HOME/data/platform.db.
        """
        if executor is None:
            raise ValueError("executor must be a ConverseExecutor instance")
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")

        self._executor = executor
        self._bus = bus
        self._shared = shared
        self._max_parallel = max_parallel
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_plan(
        self,
        plan: TaskPlan,
        workspace: str = "",
        on_step_complete: Optional[Callable[[int, int, str], None]] = None,
    ) -> CoordinationResult:
        """Execute a TaskPlan with proper coordination.

        Algorithm:
        1. Validate the plan (cycle detection).
        2. Initialise a fresh SharedState for this execution when none was provided.
        3. Loop until the DAG is complete or no more steps can progress:
           a. Find steps whose dependencies are all COMPLETED (get_ready_steps).
           b. Execute up to max_parallel ready steps concurrently.
           c. Record each result, update SharedState, publish to bus topics.
           d. On failure, cascade SKIPPED to all transitive dependents.
           e. On budget error, skip all remaining pending/ready steps.
        4. Synthesise a final response from completed steps.
        5. Persist final state to DB.
        6. Return CoordinationResult.

        Args:
            plan: TaskDAG with steps, descriptions, agent_types, and depends_on lists.
            workspace: Working directory passed to each agent step. Defaults to "".
            on_step_complete: Optional callback invoked after each step finishes
                (completed, failed, or skipped). Signature:
                ``on_step_complete(steps_done: int, total_steps: int, step_description: str)``
                where steps_done is the cumulative count of finished steps.

        Returns:
            CoordinationResult with per-step outcomes, aggregated cost, and
            a synthesised final_response.
        """
        workflow_id = getattr(plan, "workflow_id", None) or f"coord-{uuid.uuid4().hex[:12]}"
        start_wall = time.monotonic()

        # Validate: reject cyclic plans immediately.
        cycle = plan.detect_cycle()
        if cycle:
            return CoordinationResult(
                workflow_id=workflow_id,
                status="failed",
                steps=[],
                errors=[f"cycle detected in plan: {' -> '.join(cycle)}"],
            )

        # Ensure a SharedState exists for this execution.
        shared = self._shared
        if shared is None:
            shared = SharedState(session_id=workflow_id, db_path=self._db_path)

        # Semaphore limits concurrent agent executions.
        sem = asyncio.Semaphore(self._max_parallel)

        # Mutable tracking structures.
        completed_results: dict[str, StepResult] = {}
        all_step_results: list[StepResult] = []
        errors: list[str] = []
        budget_exhausted = False

        # Persist initial "pending" rows to task_steps table.
        self._upsert_steps_pending(plan, workflow_id)

        while not plan.is_complete():
            if budget_exhausted:
                # Mark all remaining PENDING/READY as skipped.
                for step in plan.steps.values():
                    if step.state in (StepState.PENDING, StepState.READY):
                        step.state = StepState.SKIPPED
                        sr = StepResult(
                            step_id=step.id,
                            agent_type=step.agent_type,
                            status="skipped",
                            error="budget exhausted — preceding step exhausted daily limit",
                        )
                        all_step_results.append(sr)
                        self._persist_step(step.id, workflow_id, "skipped", None, sr)
                break

            ready = plan.get_ready_steps()
            if not ready:
                # No steps are ready and the DAG is not complete — some steps must
                # still be RUNNING in the loop (shouldn't happen in single-thread
                # async, but guard against it to avoid an infinite spin).
                logger.warning(
                    "coordination_engine: no ready steps but DAG not complete "
                    "(workflow=%s). Remaining states: %s",
                    workflow_id,
                    {s.id: s.state.value for s in plan.steps.values()},
                )
                break

            # Execute the batch of ready steps concurrently.
            async def _run_step_guarded(step: TaskStep) -> StepResult:
                """Execute one step under the concurrency semaphore."""
                async with sem:
                    return await self._execute_step(
                        step=step,
                        plan=plan,
                        completed_results=completed_results,
                        workspace=workspace,
                        shared=shared,
                        workflow_id=workflow_id,
                    )

            batch_results: list[StepResult] = await asyncio.gather(
                *[_run_step_guarded(s) for s in ready],
                return_exceptions=False,
            )

            for sr in batch_results:
                all_step_results.append(sr)

                if sr.status == "completed":
                    completed_results[sr.step_id] = sr
                    # Mark DAG step as COMPLETED so dependents become ready.
                    dag_step = plan.steps[sr.step_id]
                    dag_step.state = StepState.COMPLETED

                elif sr.status == "failed":
                    errors.append(f"step {sr.step_id} ({sr.agent_type}): {sr.error}")
                    dag_step = plan.steps[sr.step_id]
                    dag_step.state = StepState.FAILED

                    # Cascade SKIPPED to all transitive dependents.
                    skipped_ids = plan.mark_failed_dependents(sr.step_id)
                    for sid in skipped_ids:
                        skipped_step = plan.steps[sid]
                        skip_sr = StepResult(
                            step_id=sid,
                            agent_type=skipped_step.agent_type,
                            status="skipped",
                            error=f"dependency {sr.step_id} failed",
                        )
                        all_step_results.append(skip_sr)
                        self._persist_step(sid, workflow_id, "skipped", None, skip_sr)

                    # Check whether the failure was a budget error.
                    if _is_budget_error(sr.error):
                        budget_exhausted = True

                # Notify caller of progress after each step finishes.
                if on_step_complete is not None:
                    try:
                        steps_done = sum(
                            1 for s in plan.steps.values()
                            if s.state in (StepState.COMPLETED, StepState.FAILED, StepState.SKIPPED)
                        )
                        step_desc = plan.steps[sr.step_id].description if sr.step_id in plan.steps else sr.step_id
                        on_step_complete(steps_done, len(plan.steps), step_desc)
                    except Exception as _cb_exc:
                        logger.debug("coordination_engine: on_step_complete callback error: %s", _cb_exc)

        # Determine overall status.
        total_cost = sum(sr.cost_usd for sr in all_step_results)
        total_duration = int((time.monotonic() - start_wall) * 1000)
        n_completed = sum(1 for sr in all_step_results if sr.status == "completed")
        n_failed = sum(1 for sr in all_step_results if sr.status == "failed")
        n_total = len(plan.steps)

        if n_failed == 0 and n_completed == n_total:
            overall_status = "completed"
        elif n_completed == 0:
            overall_status = "failed"
        else:
            overall_status = "partial"

        final_response = await self._synthesize_response(plan, completed_results)

        return CoordinationResult(
            workflow_id=workflow_id,
            status=overall_status,
            steps=all_step_results,
            total_cost_usd=total_cost,
            total_duration_ms=total_duration,
            final_response=final_response,
            errors=errors,
        )

    async def get_plan_status(self, workflow_id: str) -> Optional[dict]:
        """Return current execution status of a plan from the DB.

        Reads task_steps rows for the given workflow_id and returns a summary
        dict with per-state counts and a list of step records.

        Args:
            workflow_id: Workflow identifier as stored in task_steps.workflow_id.

        Returns:
            Dict with keys ``workflow_id``, ``steps`` (list), ``counts`` (dict),
            or ``None`` when no rows exist for this workflow_id.
        """
        db = get_db(self._db_path)
        try:
            rows = db.execute(
                "SELECT id, description, agent_type, state, started_at, completed_at, result_json "
                "FROM task_steps WHERE workflow_id = ? ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
        finally:
            db.close()

        if not rows:
            return None

        step_records = []
        counts: dict[str, int] = {}
        for row in rows:
            state = row["state"]
            counts[state] = counts.get(state, 0) + 1
            record: dict = {
                "id": row["id"],
                "description": row["description"],
                "agent_type": row["agent_type"],
                "state": state,
            }
            if row["started_at"] is not None:
                record["started_at"] = row["started_at"]
            if row["completed_at"] is not None:
                record["completed_at"] = row["completed_at"]
            if row["result_json"]:
                try:
                    record["result"] = json.loads(row["result_json"])
                except json.JSONDecodeError:
                    record["result"] = None
            step_records.append(record)

        return {
            "workflow_id": workflow_id,
            "steps": step_records,
            "counts": counts,
        }

    # ------------------------------------------------------------------
    # Internal execution helpers
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        step: TaskStep,
        plan: TaskPlan,
        completed_results: dict[str, StepResult],
        workspace: str,
        shared: SharedState,
        workflow_id: str,
    ) -> StepResult:
        """Execute a single step with context assembled from completed dependencies.

        Marks the step RUNNING in the DAG, calls ConverseExecutor.execute(), then
        records the outcome in SharedState and the DB.

        Args:
            step: The TaskStep to execute.
            plan: Full TaskDAG (used to look up dependency steps for context).
            completed_results: Map of step_id → StepResult for all completed steps.
            workspace: Working directory passed to the executor.
            shared: SharedState to publish findings into.
            workflow_id: Workflow identifier for DB rows.

        Returns:
            StepResult with status "completed" or "failed".
        """
        step.state = StepState.RUNNING
        started_at = time.time()
        self._persist_step_start(step.id, workflow_id, started_at)

        context = self._build_step_context(step, completed_results)
        agent_id = f"{step.agent_type}-{step.id}-{uuid.uuid4().hex[:8]}"

        logger.info(
            "coordination_engine: starting step=%s agent=%s workflow=%s",
            step.id,
            step.agent_type,
            workflow_id,
        )

        # ConverseExecutor.execute() is synchronous — run it in the thread pool
        # so it doesn't block the event loop while the other parallel steps run.
        try:
            loop = asyncio.get_event_loop()
            conv_result = await loop.run_in_executor(
                None,
                lambda: self._executor.execute(
                    agent_id=agent_id,
                    agent_type=step.agent_type,
                    prompt=step.description,
                    model=None,  # auto-selected per agent_type
                    max_tokens=8192,
                    context=context if context else None,
                ),
            )
        except Exception as exc:
            logger.exception(
                "coordination_engine: executor raised for step=%s: %s", step.id, exc
            )
            sr = StepResult(
                step_id=step.id,
                agent_type=step.agent_type,
                status="failed",
                error=f"executor exception: {exc}",
                cost_usd=0.0,
                duration_ms=int((time.time() - started_at) * 1000),
                output_summary="",
            )
            self._persist_step(step.id, workflow_id, "failed", started_at, sr)
            return sr

        duration_ms = conv_result.duration_ms
        cost_usd = conv_result.total_cost_usd

        if conv_result.error:
            logger.warning(
                "coordination_engine: step=%s failed: %s", step.id, conv_result.error
            )
            sr = StepResult(
                step_id=step.id,
                agent_type=step.agent_type,
                status="failed",
                error=conv_result.error,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                output_summary="",
            )
            self._persist_step(step.id, workflow_id, "failed", started_at, sr)
            return sr

        response_text = conv_result.response or ""
        output_summary = self._extract_summary(response_text, max_chars=500)

        logger.info(
            "coordination_engine: step=%s completed in %dms cost=$%.4f",
            step.id,
            duration_ms,
            cost_usd,
        )

        sr = StepResult(
            step_id=step.id,
            agent_type=step.agent_type,
            status="completed",
            response=response_text,
            error=None,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            output_summary=output_summary,
        )

        # Publish to SharedState so other agents (and bus subscribers) can read it.
        await self._publish_to_shared_state(step, sr, shared, workflow_id)
        self._persist_step(step.id, workflow_id, "completed", started_at, sr)

        return sr

    def _build_step_context(
        self,
        step: TaskStep,
        completed_results: dict[str, StepResult],
    ) -> str:
        """Build context string from completed dependency outputs.

        For each dependency that has a completed StepResult, its output_summary
        is included under a labelled heading. Steps with no completed dependencies
        receive an empty string so their prompt is unchanged.

        Args:
            step: The step whose context is being built.
            completed_results: Map of step_id → StepResult for completed steps.

        Returns:
            Multi-section context string, or "" when no dependency results exist.
        """
        sections: list[str] = []
        for dep_id in step.depends_on:
            dep_result = completed_results.get(dep_id)
            if dep_result and dep_result.output_summary:
                sections.append(
                    f"### Output from {dep_result.agent_type} (step {dep_id})\n"
                    f"{dep_result.output_summary}"
                )

        return "\n\n".join(sections)

    async def _synthesize_response(
        self,
        plan: TaskPlan,
        results: dict[str, StepResult],
    ) -> str:
        """Synthesise a final coherent response from all step results.

        Uses a template-based approach (no additional LLM call) to combine
        completed step outputs in topological order. Steps on the critical path
        are highlighted.

        Args:
            plan: TaskDAG used to determine critical path and step order.
            results: Map of step_id → StepResult for completed steps.

        Returns:
            Formatted string summarising all completed step outputs.
        """
        if not results:
            return "No steps completed successfully."

        critical_path_ids = set(plan.critical_path())

        # Visit in topological order (critical path first, then others alphabetically).
        ordered_ids = sorted(
            results.keys(),
            key=lambda sid: (
                0 if sid in critical_path_ids else 1,
                sid,
            ),
        )

        sections: list[str] = []
        for sid in ordered_ids:
            sr = results[sid]
            step = plan.steps.get(sid)
            description = step.description if step else sid
            marker = " [critical path]" if sid in critical_path_ids else ""
            header = f"## {sr.agent_type.upper()} — {description}{marker}"
            body = sr.response or sr.output_summary or "(no output)"
            sections.append(f"{header}\n\n{body}")

        total_cost = sum(sr.cost_usd for sr in results.values())
        footer = (
            f"\n---\n"
            f"Steps completed: {len(results)} / {len(plan.steps)} | "
            f"Total cost: ${total_cost:.4f}"
        )

        return "\n\n".join(sections) + footer

    # ------------------------------------------------------------------
    # SharedState / Bus publishing
    # ------------------------------------------------------------------

    async def _publish_to_shared_state(
        self,
        step: TaskStep,
        sr: StepResult,
        shared: SharedState,
        workflow_id: str,
    ) -> None:
        """Publish step completion data to SharedState.

        Writes three keys:
        - ``step.{step_id}.result`` — full StepResult dict
        - ``status.{agent_type}.{step_id}`` — lightweight status envelope
        - ``findings.{agent_type}`` — latest finding for this agent_type

        Args:
            step: The completed TaskStep.
            sr: The StepResult.
            shared: SharedState instance.
            workflow_id: Current workflow ID for the envelope.
        """
        step_payload: dict = {
            "step_id": sr.step_id,
            "agent_type": sr.agent_type,
            "status": sr.status,
            "output_summary": sr.output_summary,
            "cost_usd": sr.cost_usd,
            "duration_ms": sr.duration_ms,
            "workflow_id": workflow_id,
            "published_at": time.time(),
        }

        try:
            await shared.set(
                f"step.{sr.step_id}.result",
                step_payload,
                publisher=sr.agent_type,
            )
            await shared.set(
                f"status.{sr.agent_type}.{sr.step_id}",
                {"status": sr.status, "workflow_id": workflow_id, "at": time.time()},
                publisher=sr.agent_type,
            )
            await shared.set(
                f"findings.{sr.agent_type}",
                step_payload,
                publisher=sr.agent_type,
            )
        except Exception as exc:
            # SharedState publishing must not abort the coordination loop.
            logger.warning(
                "coordination_engine: SharedState publish failed for step=%s: %s",
                sr.step_id,
                exc,
            )

    # ------------------------------------------------------------------
    # DB persistence helpers
    # ------------------------------------------------------------------

    def _upsert_steps_pending(self, plan: TaskPlan, workflow_id: str) -> None:
        """Insert all plan steps into task_steps with state='pending'.

        Uses INSERT OR IGNORE so re-running a plan on an existing workflow_id
        does not overwrite rows that are already in progress.

        Args:
            plan: TaskDAG whose steps are to be inserted.
            workflow_id: Workflow identifier for the rows.
        """
        db = get_db(self._db_path)
        try:
            for step in plan.steps.values():
                db.execute(
                    """
                    INSERT OR IGNORE INTO task_steps
                        (id, workflow_id, description, agent_type, depends_on, state)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        step.id,
                        workflow_id,
                        step.description,
                        step.agent_type,
                        json.dumps(step.depends_on),
                    ),
                )
            db.commit()
        except Exception as exc:
            logger.warning("coordination_engine: failed to upsert pending steps: %s", exc)
        finally:
            db.close()

    def _persist_step_start(self, step_id: str, workflow_id: str, started_at: float) -> None:
        """Mark a step as 'running' and record started_at.

        Args:
            step_id: Step primary key.
            workflow_id: Workflow identifier (used to scope the UPDATE).
            started_at: Unix timestamp when execution began.
        """
        db = get_db(self._db_path)
        try:
            db.execute(
                "UPDATE task_steps SET state = 'running', started_at = ? "
                "WHERE id = ? AND workflow_id = ?",
                (started_at, step_id, workflow_id),
            )
            db.commit()
        except Exception as exc:
            logger.warning("coordination_engine: persist_step_start failed step=%s: %s", step_id, exc)
        finally:
            db.close()

    def _persist_step(
        self,
        step_id: str,
        workflow_id: str,
        state: str,
        started_at: Optional[float],
        sr: StepResult,
    ) -> None:
        """Persist the final state of a step to task_steps.

        Writes state, completed_at, and result_json. started_at is set only
        when provided (skipped steps may never have had a started_at).

        Args:
            step_id: Step primary key.
            workflow_id: Workflow identifier for the WHERE clause.
            state: Terminal state string ("completed", "failed", or "skipped").
            started_at: Unix timestamp when execution began, or None for skipped steps.
            sr: StepResult to serialise into result_json.
        """
        completed_at = time.time()
        result_json = json.dumps({
            "status": sr.status,
            "error": sr.error,
            "output_summary": sr.output_summary,
            "cost_usd": sr.cost_usd,
            "duration_ms": sr.duration_ms,
        })

        db = get_db(self._db_path)
        try:
            if started_at is not None:
                db.execute(
                    "UPDATE task_steps SET state = ?, started_at = ?, completed_at = ?, result_json = ? "
                    "WHERE id = ? AND workflow_id = ?",
                    (state, started_at, completed_at, result_json, step_id, workflow_id),
                )
            else:
                db.execute(
                    "UPDATE task_steps SET state = ?, completed_at = ?, result_json = ? "
                    "WHERE id = ? AND workflow_id = ?",
                    (state, completed_at, result_json, step_id, workflow_id),
                )
            db.commit()
        except Exception as exc:
            logger.warning("coordination_engine: persist_step failed step=%s: %s", step_id, exc)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_summary(text: str, max_chars: int = 500) -> str:
        """Extract a concise summary from a response for use as context.

        Takes the first ``max_chars`` characters of the response and appends
        an ellipsis when truncated.

        Args:
            text: Full response text from an agent step.
            max_chars: Maximum length of the summary.

        Returns:
            Truncated summary string.
        """
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + " ..."

"""
DAG Executor for CAP orchestration.

Executes a TaskDAG to completion with maximum parallelism while
respecting dependency ordering. Integrates with ContextThread to
pass dependency outputs as context frames to downstream steps.

Reference: CAP System Design Section 18 — Parallel DAG Executor.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable, Optional

from .dag import TaskDAG, TaskStep, StepState
from .context import ContextThread, ContextFrame

logger = logging.getLogger(__name__)


class DAGExecutor:
    """
    Execute a task DAG with maximum parallelism while respecting dependencies.

    Args:
        dag: The TaskDAG to execute.
        dispatch_fn: Async callable (step, dep_context) -> result dict.
                     This is the agent dispatch function provided by the orchestrator.
        context_thread: ContextThread for inter-agent context passing.
        max_concurrency: Maximum number of steps to run in parallel.
        on_step_complete: Optional callback(step, result) fired after each step.
    """

    def __init__(
        self,
        dag: TaskDAG,
        dispatch_fn: Callable[[TaskStep, dict[str, Any]], Awaitable[dict]],
        context_thread: Optional[ContextThread] = None,
        max_concurrency: int = 5,
        on_step_complete: Optional[Callable[[TaskStep, dict], None]] = None,
    ):
        self.dag = dag
        self.dispatch_fn = dispatch_fn
        self.context_thread = context_thread or ContextThread()
        self.max_concurrency = max_concurrency
        self.on_step_complete = on_step_complete
        self.results: dict[str, dict] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def execute(self) -> dict[str, dict]:
        """
        Run the DAG to completion with maximum parallelism.

        Returns:
            Dict mapping step_id -> result for all executed steps.
        """
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

        while not self.dag.is_complete():
            # Mark dependents of failed steps as SKIPPED
            self.dag.mark_failed_dependents()

            # Get steps that are ready to run
            ready = self.dag.get_ready_steps()
            if not ready:
                # If nothing is ready and DAG isn't complete, we're stuck
                # (all remaining steps depend on failed/skipped steps)
                pending = [
                    s for s in self.dag.steps.values()
                    if s.state in (StepState.PENDING, StepState.READY)
                ]
                if not pending:
                    break
                # Mark remaining pending as skipped (unreachable)
                for s in pending:
                    s.state = StepState.SKIPPED
                break

            # Dispatch all ready steps in parallel (respecting concurrency limit)
            tasks = []
            for step in ready:
                step.state = StepState.RUNNING

                # Build dependency context from completed predecessors
                dep_context = {
                    dep_id: self.results[dep_id]
                    for dep_id in step.depends_on
                    if dep_id in self.results
                }
                tasks.append(self._run_step(step, dep_context))

            # Await all parallel steps
            await asyncio.gather(*tasks)

        return self.results

    async def _run_step(self, step: TaskStep, dep_context: dict[str, Any]) -> None:
        """
        Dispatch a single agent step with dependency context.

        Handles:
        - Concurrency limiting via semaphore
        - Context frame creation from completed dependencies
        - Result recording and state transitions
        - Failure handling (marks step FAILED, does not raise)
        """
        async with self._semaphore:
            # Build context frame for this step from dependencies
            frame = ContextFrame(
                agent_type=step.agent_type,
                task=step.description,
            )
            frame.mark_running()

            # Populate prior outputs from dependency results
            prior_summaries = []
            for dep_id, dep_result in dep_context.items():
                dep_step = self.dag.steps.get(dep_id)
                if dep_step:
                    prior_summaries.append(
                        f"[{dep_step.agent_type}:{dep_id}] "
                        f"{dep_result.get('summary', str(dep_result.get('status', '')))}"
                    )

            # Add context frame to thread
            self.context_thread.add_frame(frame)

            try:
                start_time = time.time()
                result = await self.dispatch_fn(step, dep_context)
                elapsed_ms = int((time.time() - start_time) * 1000)

                # Determine outcome
                status = result.get("status", "success")
                if status in ("failed", "timeout", "circuit_open", "error"):
                    step.state = StepState.FAILED
                    step.result = result
                    frame.mark_failed(result.get("error", status))
                    logger.warning(
                        "Step %s (%s) failed after %dms: %s",
                        step.id, step.agent_type, elapsed_ms, result.get("error", status),
                    )
                else:
                    step.state = StepState.COMPLETED
                    step.result = result
                    outputs = []
                    if "summary" in result:
                        outputs.append(result["summary"])
                    if "output" in result:
                        outputs.append(str(result["output"]))
                    frame.mark_completed(
                        outputs=outputs or [f"completed in {elapsed_ms}ms"],
                        artifacts=result.get("artifacts", []),
                    )
                    logger.info(
                        "Step %s (%s) completed in %dms",
                        step.id, step.agent_type, elapsed_ms,
                    )

                self.results[step.id] = result

            except Exception as e:
                step.state = StepState.FAILED
                step.result = {"status": "failed", "error": str(e)}
                frame.mark_failed(str(e))
                self.results[step.id] = step.result
                logger.error(
                    "Step %s (%s) raised exception: %s",
                    step.id, step.agent_type, e,
                )

            # Fire completion callback if provided
            if self.on_step_complete:
                try:
                    self.on_step_complete(step, self.results.get(step.id, {}))
                except Exception as cb_err:
                    logger.warning("on_step_complete callback error: %s", cb_err)

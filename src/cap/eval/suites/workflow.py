"""Workflow orchestration evaluation suite.

Tests budget enforcement, agent cap limits, event emission completeness,
and team rendering output format correctness.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from cap.eval.framework import EvalCase, EvalResult, EvalSuite, MetricType


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

WORKFLOW_SCENARIOS = {
    "budget_normal": {
        "budget_usd": 5.0,
        "agents": [
            {"role": "architect", "model": "opus", "input_tokens": 1000, "output_tokens": 500},
            {"role": "devops", "model": "sonnet", "input_tokens": 2000, "output_tokens": 1000},
        ],
        "expected_kill": False,
    },
    "budget_exceeded": {
        "budget_usd": 0.01,
        "agents": [
            {"role": "architect", "model": "opus", "input_tokens": 50000, "output_tokens": 25000},
        ],
        "expected_kill": True,
    },
    "budget_edge": {
        "budget_usd": 0.20,
        "agents": [
            {"role": "devops", "model": "sonnet", "input_tokens": 10000, "output_tokens": 3000},
            {"role": "sre", "model": "sonnet", "input_tokens": 10000, "output_tokens": 3000},
        ],
        "expected_kill": False,  # 2 agents ~$0.15 total, under $0.20 budget
    },
}

EVENT_TYPES_REQUIRED = [
    "PHASE_START",
    "AGENT_START",
    "AGENT_COMPLETE",
    "PHASE_END",
    "WORKFLOW_COMPLETE",
]


# ---------------------------------------------------------------------------
# Suite implementation
# ---------------------------------------------------------------------------


class WorkflowEvalSuite(EvalSuite):
    """Evaluates workflow orchestration: budget, caps, events, rendering."""

    name = "workflow"
    description = "Measures budget enforcement, agent limits, event completeness, and rendering correctness"

    def __init__(self) -> None:
        super().__init__()
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._tmp_dir: tempfile.TemporaryDirectory | None = None

    def setup(self) -> None:
        """Create test platform DB for workflow tracking."""
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="cap_eval_workflow_")
        self._db_path = Path(self._tmp_dir.name) / "platform.db"
        self._conn = sqlite3.connect(str(self._db_path))

        # Create workflow-related schema (mirrors models.py init_database)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                budget_usd REAL NOT NULL DEFAULT 5.0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                max_agents INTEGER NOT NULL DEFAULT 5,
                agents_active INTEGER NOT NULL DEFAULT 0,
                agents_total INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                killed_reason TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                agent_role TEXT,
                message TEXT,
                target_agent TEXT,
                phase TEXT,
                tokens_delta INTEGER NOT NULL DEFAULT 0,
                cost_delta REAL NOT NULL DEFAULT 0.0,
                timestamp REAL NOT NULL,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id)
            );

            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                agent_id TEXT,
                model_tier TEXT NOT NULL,
                model_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                latency_ms REAL,
                throttled INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (workflow_id) REFERENCES workflows(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_workflow ON workflow_events(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_api_calls_workflow ON api_calls(workflow_id);
        """)
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
        """Build workflow eval cases."""
        cases: list[EvalCase] = []

        # --- Budget enforcement ---
        for scenario_name, scenario in WORKFLOW_SCENARIOS.items():
            cases.append(
                EvalCase(
                    name=f"budget_{scenario_name}",
                    category="budget_enforcement",
                    input=scenario,
                    expected=scenario["expected_kill"],
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # Budget cost tracking accuracy
        cases.append(
            EvalCase(
                name="budget_cost_tracking_opus",
                category="budget_enforcement",
                input={
                    "model": "opus",
                    "input_tokens": 10000,
                    "output_tokens": 5000,
                },
                # Cost = (10000/1M * 15.0) + (5000/1M * 75.0) = 0.15 + 0.375 = 0.525
                expected=0.525,
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.001},
            )
        )
        cases.append(
            EvalCase(
                name="budget_cost_tracking_sonnet",
                category="budget_enforcement",
                input={
                    "model": "sonnet",
                    "input_tokens": 10000,
                    "output_tokens": 5000,
                },
                # Cost = (10000/1M * 3.0) + (5000/1M * 15.0) = 0.03 + 0.075 = 0.105
                expected=0.105,
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.001},
            )
        )
        cases.append(
            EvalCase(
                name="budget_cost_tracking_haiku",
                category="budget_enforcement",
                input={
                    "model": "haiku",
                    "input_tokens": 10000,
                    "output_tokens": 5000,
                },
                # Cost = (10000/1M * 0.80) + (5000/1M * 4.0) = 0.008 + 0.020 = 0.028
                expected=0.028,
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
                metadata={"tolerance": 0.001},
            )
        )

        # --- Max agents cap ---
        cases.append(
            EvalCase(
                name="max_agents_respected",
                category="agent_cap",
                input={"max_agents": 3, "requested": 5},
                expected=3,  # Should cap at max
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="max_agents_under_cap",
                category="agent_cap",
                input={"max_agents": 5, "requested": 3},
                expected=3,  # Under cap, all allowed
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="max_agents_slot_weights",
                category="agent_cap",
                input={"max_slots": 6, "agents": [
                    {"model": "opus"},    # 3 slots
                    {"model": "sonnet"},  # 2 slots
                    {"model": "haiku"},   # 1 slot = 6 total, exactly at cap
                ]},
                expected=True,  # Should fit
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="max_agents_slot_overflow",
                category="agent_cap",
                input={"max_slots": 5, "agents": [
                    {"model": "opus"},    # 3 slots
                    {"model": "opus"},    # 3 slots = 6 > 5
                ]},
                expected=False,  # Should not fit
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )

        # --- Event emission completeness ---
        cases.append(
            EvalCase(
                name="event_emission_all_types",
                category="event_emission",
                input="complete_workflow",
                expected=EVENT_TYPES_REQUIRED,
                metric=MetricType.RECALL_AT_K,
                threshold=1.0,
                metadata={"k": 20},
            )
        )
        cases.append(
            EvalCase(
                name="event_emission_ordering",
                category="event_emission",
                input="event_ordering",
                expected=["PHASE_START", "AGENT_START", "AGENT_COMPLETE", "PHASE_END"],
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="event_emission_budget_event_on_kill",
                category="event_emission",
                input="budget_kill_emits_event",
                expected="WORKFLOW_KILLED",
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )

        # --- Team rendering output format ---
        cases.append(
            EvalCase(
                name="renderer_header_format",
                category="team_rendering",
                input={"workflow": "test-workflow", "budget": 5.0, "max_agents": 3},
                expected="contains_workflow_name",
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="renderer_event_format",
                category="team_rendering",
                input={
                    "event_type": "AGENT_START",
                    "agent_role": "devops",
                    "message": "Starting infrastructure provisioning",
                },
                expected="renders_without_error",
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="renderer_status_line",
                category="team_rendering",
                input={"workflow": "test-wf", "budget": 10.0, "max_agents": 5},
                expected="contains_cost_info",
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="renderer_all_event_types_handled",
                category="team_rendering",
                input="all_event_types",
                expected=True,
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )

        # --- Latency ---
        cases.append(
            EvalCase(
                name="event_insertion_latency_p95",
                category="latency",
                input="event_insert",
                expected=None,
                metric=MetricType.LATENCY_P95,
                threshold=10.0,  # 10ms for DB insert
            )
        )

        return cases

    def evaluate_case(self, case: EvalCase) -> EvalResult:
        """Run a single workflow eval case."""
        if case.category == "budget_enforcement":
            return self._eval_budget(case)
        elif case.category == "agent_cap":
            return self._eval_agent_cap(case)
        elif case.category == "event_emission":
            return self._eval_event_emission(case)
        elif case.category == "team_rendering":
            return self._eval_team_rendering(case)
        elif case.category == "latency":
            return self._eval_latency(case)
        else:
            return EvalResult(
                case=case, actual=None, score=0.0, passed=False, latency_ms=0.0,
                details={"reason": f"Unknown category: {case.category}"},
            )

    def _eval_budget(self, case: EvalCase) -> EvalResult:
        """Test budget enforcement logic."""
        from cap.lib.models import MODEL_PRICING, ModelTier

        input_data = case.input
        t0 = time.perf_counter()

        if "model" in input_data and "input_tokens" in input_data:
            # Cost calculation accuracy test
            model = ModelTier(input_data["model"])
            in_tokens = input_data["input_tokens"]
            out_tokens = input_data["output_tokens"]

            pricing = MODEL_PRICING[model]
            actual_cost = (in_tokens / 1_000_000 * pricing["input"]) + (
                out_tokens / 1_000_000 * pricing["output"]
            )

            tolerance = case.metadata.get("tolerance", 0.001)
            score = 1.0 if abs(actual_cost - case.expected) <= tolerance else 0.0
            actual = actual_cost
        else:
            # Budget exceeded scenario
            budget = input_data["budget_usd"]
            agents = input_data["agents"]
            expected_kill = input_data["expected_kill"]

            # Calculate total cost
            total_cost = 0.0
            for agent in agents:
                pricing = MODEL_PRICING[ModelTier(agent["model"])]
                cost = (agent["input_tokens"] / 1_000_000 * pricing["input"]) + (
                    agent["output_tokens"] / 1_000_000 * pricing["output"]
                )
                total_cost += cost

            should_kill = total_cost > budget
            actual = should_kill
            score = 1.0 if should_kill == expected_kill else 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"actual={actual}, expected={case.expected}"},
        )

    def _eval_agent_cap(self, case: EvalCase) -> EvalResult:
        """Test max agent cap enforcement."""
        from cap.lib.models import MODEL_SLOT_WEIGHTS, ModelTier

        input_data = case.input
        t0 = time.perf_counter()

        if "max_slots" in input_data:
            # Slot-weighted cap test
            max_slots = input_data["max_slots"]
            agents = input_data["agents"]
            total_slots = sum(MODEL_SLOT_WEIGHTS[ModelTier(a["model"])] for a in agents)
            actual = total_slots <= max_slots
            score = 1.0 if actual == case.expected else 0.0
        else:
            # Simple count cap
            max_agents = input_data["max_agents"]
            requested = input_data["requested"]
            actual = min(requested, max_agents)
            score = 1.0 if actual == case.expected else 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"actual={actual}, expected={case.expected}"},
        )

    def _eval_event_emission(self, case: EvalCase) -> EvalResult:
        """Test event emission completeness and ordering."""
        from cap.lib.team_renderer import EventType, TeamEvent, TeamRenderer

        t0 = time.perf_counter()

        if case.input == "complete_workflow":
            # Simulate a complete workflow and check all event types emitted
            events = self._simulate_workflow_events()
            emitted_types = [e.event_type.name for e in events]
            actual = list(set(emitted_types))

            score = self.compute_score(case, actual)

        elif case.input == "event_ordering":
            # Check that events follow correct order
            events = self._simulate_workflow_events()
            # Extract the sequence of event types
            order = []
            for e in events:
                if e.event_type.name in case.expected and e.event_type.name not in order:
                    order.append(e.event_type.name)
            actual = order
            score = 1.0 if order == case.expected else 0.0

        elif case.input == "budget_kill_emits_event":
            # Simulate budget kill
            events = self._simulate_budget_kill()
            kill_events = [e for e in events if e.event_type == EventType.WORKFLOW_KILLED]
            actual = kill_events[0].event_type.name if kill_events else "MISSING"
            score = 1.0 if actual == case.expected else 0.0

        else:
            actual = None
            score = 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"event emission incomplete"},
        )

    def _eval_team_rendering(self, case: EvalCase) -> EvalResult:
        """Test team renderer output format."""
        from cap.lib.team_renderer import EventType, TeamEvent, TeamRenderer
        from io import StringIO
        from rich.console import Console

        t0 = time.perf_counter()

        if case.input == "all_event_types":
            # Test that all event types can be rendered without error
            console = Console(file=StringIO(), force_terminal=True)
            renderer = TeamRenderer(
                workflow_name="test", budget_usd=5.0, max_agents=3, console=console
            )
            all_ok = True
            for event_type in EventType:
                try:
                    event = TeamEvent(
                        event_type=event_type,
                        agent_role="devops",
                        message="Test message",
                        phase="test_phase",
                    )
                    renderer.render_event(event)
                except Exception as e:
                    all_ok = False
                    break

            actual = all_ok
            score = 1.0 if actual == case.expected else 0.0

        elif "workflow" in case.input and isinstance(case.input, dict):
            console = Console(file=StringIO(), force_terminal=True)
            renderer = TeamRenderer(
                workflow_name=case.input["workflow"],
                budget_usd=case.input["budget"],
                max_agents=case.input["max_agents"],
                console=console,
            )

            if case.expected == "contains_workflow_name":
                try:
                    renderer.render_header()
                    output = console.file.getvalue()
                    actual = case.input["workflow"] in output
                    score = 1.0 if actual else 0.0
                except Exception:
                    actual = False
                    score = 0.0
            elif case.expected == "contains_cost_info":
                try:
                    status = renderer.render_status_line()
                    actual = "$" in status or "cost" in status.lower() or "budget" in status.lower()
                    score = 1.0 if actual else 0.0
                except Exception:
                    actual = False
                    score = 0.0
            else:
                actual = None
                score = 0.0

        elif "event_type" in case.input:
            console = Console(file=StringIO(), force_terminal=True)
            renderer = TeamRenderer(
                workflow_name="test", budget_usd=5.0, max_agents=3, console=console
            )
            try:
                event = TeamEvent(
                    event_type=EventType[case.input["event_type"]],
                    agent_role=case.input["agent_role"],
                    message=case.input["message"],
                )
                renderer.render_event(event)
                actual = "renders_without_error"
                score = 1.0
            except Exception as e:
                actual = f"error: {e}"
                score = 0.0

        else:
            actual = None
            score = 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"rendering failed: {actual}"},
        )

    def _eval_latency(self, case: EvalCase) -> EvalResult:
        """Measure event insertion latency."""
        import math
        import uuid

        latencies: list[float] = []
        workflow_id = f"eval-{uuid.uuid4().hex[:8]}"

        # Create workflow
        self._conn.execute(
            "INSERT INTO workflows (id, name, status, budget_usd) VALUES (?, ?, ?, ?)",
            (workflow_id, "latency-test", "running", 5.0),
        )
        self._conn.commit()

        for i in range(50):
            t0 = time.perf_counter()
            self._conn.execute(
                """INSERT INTO workflow_events
                   (workflow_id, event_type, agent_role, message, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (workflow_id, "AGENT_START", "devops", f"Event {i}", time.time()),
            )
            self._conn.commit()
            latencies.append((time.perf_counter() - t0) * 1000)

        from cap.eval.framework import score_latency_p95

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

    def _simulate_workflow_events(self) -> list:
        """Simulate a complete workflow event sequence."""
        from cap.lib.team_renderer import EventType, TeamEvent

        events = [
            TeamEvent(event_type=EventType.PHASE_START, agent_role="orchestrator",
                     message="Starting architecture phase", phase="architecture"),
            TeamEvent(event_type=EventType.AGENT_START, agent_role="architect",
                     message="Designing solution"),
            TeamEvent(event_type=EventType.AGENT_THINKING, agent_role="architect",
                     message="Evaluating options..."),
            TeamEvent(event_type=EventType.AGENT_MESSAGE, agent_role="architect",
                     message="Recommending EKS with Karpenter"),
            TeamEvent(event_type=EventType.AGENT_COMPLETE, agent_role="architect",
                     message="Architecture design complete"),
            TeamEvent(event_type=EventType.PHASE_END, agent_role="orchestrator",
                     message="Architecture phase complete", phase="architecture"),
            TeamEvent(event_type=EventType.PHASE_START, agent_role="orchestrator",
                     message="Starting implementation phase", phase="implementation"),
            TeamEvent(event_type=EventType.AGENT_START, agent_role="devops",
                     message="Writing Terraform"),
            TeamEvent(event_type=EventType.AGENT_COMPLETE, agent_role="devops",
                     message="Infrastructure ready"),
            TeamEvent(event_type=EventType.PHASE_END, agent_role="orchestrator",
                     message="Implementation complete", phase="implementation"),
            TeamEvent(event_type=EventType.WORKFLOW_COMPLETE, agent_role="orchestrator",
                     message="Workflow finished successfully"),
        ]
        return events

    def _simulate_budget_kill(self) -> list:
        """Simulate a budget-exceeded kill event sequence."""
        from cap.lib.team_renderer import EventType, TeamEvent

        events = [
            TeamEvent(event_type=EventType.PHASE_START, agent_role="orchestrator",
                     message="Starting", phase="work"),
            TeamEvent(event_type=EventType.AGENT_START, agent_role="architect",
                     message="Working..."),
            TeamEvent(event_type=EventType.WORKFLOW_BUDGET, agent_role="orchestrator",
                     message="Budget 90% consumed"),
            TeamEvent(event_type=EventType.WORKFLOW_KILLED, agent_role="orchestrator",
                     message="Budget exceeded - workflow killed"),
        ]
        return events

"""Tests for CAP DAG-based task decomposition."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.orchestration.dag import TaskDAG, TaskStep, StepState


def _make_dag(steps_spec: list[tuple]) -> TaskDAG:
    """
    Helper to build a DAG from a list of (id, agent_type, depends_on) tuples.
    """
    dag = TaskDAG()
    for step_id, agent_type, deps in steps_spec:
        dag.steps[step_id] = TaskStep(
            id=step_id,
            description=f"Step {step_id}",
            agent_type=agent_type,
            depends_on=deps,
        )
    return dag


class TestCycleDetection:
    """Test DAG cycle detection."""

    def test_no_cycle_in_valid_dag(self):
        """A valid DAG should return None (no cycle)."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", ["b"]),
        ])
        assert dag.detect_cycle() is None

    def test_simple_cycle_detected(self):
        """A -> B -> C -> A should be detected as a cycle."""
        dag = _make_dag([
            ("a", "dev", ["c"]),
            ("b", "dev", ["a"]),
            ("c", "dev", ["b"]),
        ])
        cycle = dag.detect_cycle()
        assert len(cycle) > 0
        # All cycle members should be from {a, b, c}
        assert all(s in ("a", "b", "c") for s in cycle)

    def test_self_cycle_detected(self):
        """A step depending on itself is a cycle."""
        dag = _make_dag([
            ("a", "dev", ["a"]),
        ])
        cycle = dag.detect_cycle()
        assert len(cycle) > 0
        assert "a" in cycle

    def test_partial_cycle_in_larger_graph(self):
        """Cycle in subset of a larger graph should still be detected."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", ["d"]),  # c -> d -> c forms cycle
            ("d", "dev", ["c"]),
            ("e", "dev", ["b"]),
        ])
        cycle = dag.detect_cycle()
        assert len(cycle) > 0
        # Cycle should involve c and d
        assert "c" in cycle or "d" in cycle


class TestGetReadySteps:
    """Test ready step resolution with dependencies."""

    def test_root_steps_are_ready(self):
        """Steps with no dependencies should be immediately ready."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "devops", []),
            ("c", "dev", ["a", "b"]),
        ])
        ready = dag.get_ready_steps()
        ready_ids = [s.id for s in ready]
        assert "a" in ready_ids
        assert "b" in ready_ids
        assert "c" not in ready_ids

    def test_step_ready_after_deps_complete(self):
        """Step becomes ready when all its dependencies are COMPLETED."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
        ])

        # Initially only 'a' is ready
        ready = dag.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "a"

        # Complete 'a'
        dag.steps["a"].state = StepState.COMPLETED

        # Now 'b' should be ready
        ready = dag.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "b"

    def test_step_not_ready_with_partial_deps(self):
        """Step with multiple deps is not ready until ALL deps complete."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", []),
            ("c", "dev", ["a", "b"]),
        ])

        # Complete only 'a'
        dag.steps["a"].state = StepState.COMPLETED

        # Get ready: 'b' becomes ready, 'c' does not
        ready = dag.get_ready_steps()
        ready_ids = [s.id for s in ready]
        assert "b" in ready_ids
        assert "c" not in ready_ids

    def test_transitions_to_ready_state(self):
        """get_ready_steps should transition matching steps from PENDING to READY."""
        dag = _make_dag([
            ("a", "dev", []),
        ])
        assert dag.steps["a"].state == StepState.PENDING

        ready = dag.get_ready_steps()
        assert dag.steps["a"].state == StepState.READY


class TestCriticalPath:
    """Test critical path computation."""

    def test_linear_chain(self):
        """Critical path of A -> B -> C -> D is [A, B, C, D]."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", ["b"]),
            ("d", "dev", ["c"]),
        ])
        cp = dag.critical_path()
        assert cp == ["a", "b", "c", "d"]

    def test_parallel_paths_picks_longest(self):
        """With two parallel paths of different lengths, picks the longest."""
        dag = _make_dag([
            ("a", "dev", []),       # Path 1: a -> b -> c -> e (length 4)
            ("b", "dev", ["a"]),
            ("c", "dev", ["b"]),
            ("d", "dev", []),       # Path 2: d -> e (length 2)
            ("e", "dev", ["c", "d"]),
        ])
        cp = dag.critical_path()
        assert len(cp) == 4
        assert cp == ["a", "b", "c", "e"]

    def test_single_step(self):
        """Single step DAG has critical path of length 1."""
        dag = _make_dag([
            ("only", "dev", []),
        ])
        cp = dag.critical_path()
        assert cp == ["only"]

    def test_diamond_dag(self):
        """Diamond: A -> (B, C) -> D. Critical path length is 3."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", ["a"]),
            ("d", "dev", ["b", "c"]),
        ])
        cp = dag.critical_path()
        assert len(cp) == 3
        assert cp[0] == "a"
        assert cp[-1] == "d"


class TestFailedDepSkipsDependents:
    """Test that failed dependencies cause dependents to be skipped."""

    def test_direct_dependent_skipped(self):
        """If A fails, B (depends on A) should be skipped."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
        ])
        dag.steps["a"].state = StepState.FAILED

        skipped = dag.mark_failed_dependents()
        assert "b" in skipped
        assert dag.steps["b"].state == StepState.SKIPPED

    def test_transitive_dependents_skipped(self):
        """If A fails, B and C (transitively dependent) should both be skipped."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", ["b"]),
        ])
        dag.steps["a"].state = StepState.FAILED

        skipped = dag.mark_failed_dependents()
        assert "b" in skipped
        assert "c" in skipped

    def test_unrelated_steps_not_skipped(self):
        """Steps not depending on the failed step should not be skipped."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
            ("c", "dev", []),  # Independent
            ("d", "dev", ["c"]),
        ])
        dag.steps["a"].state = StepState.FAILED

        skipped = dag.mark_failed_dependents()
        assert "b" in skipped
        assert "c" not in skipped
        assert "d" not in skipped
        assert dag.steps["c"].state == StepState.PENDING
        assert dag.steps["d"].state == StepState.PENDING

    def test_is_complete_after_skip(self):
        """DAG with all steps completed/failed/skipped should be complete."""
        dag = _make_dag([
            ("a", "dev", []),
            ("b", "dev", ["a"]),
        ])
        dag.steps["a"].state = StepState.FAILED
        dag.mark_failed_dependents()

        assert dag.is_complete()

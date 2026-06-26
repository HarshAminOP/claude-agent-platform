"""Eval suites for CAP components.

Each suite is self-contained: creates its own test fixtures in /tmp,
runs evaluations, and tears down after.
"""

from cap.eval.suites.retrieval import RetrievalEvalSuite
from cap.eval.suites.session import SessionEvalSuite
from cap.eval.suites.security import SecurityEvalSuite
from cap.eval.suites.workflow import WorkflowEvalSuite

ALL_SUITES = {
    "retrieval": RetrievalEvalSuite,
    "session": SessionEvalSuite,
    "security": SecurityEvalSuite,
    "workflow": WorkflowEvalSuite,
}

__all__ = [
    "ALL_SUITES",
    "RetrievalEvalSuite",
    "SessionEvalSuite",
    "SecurityEvalSuite",
    "WorkflowEvalSuite",
]

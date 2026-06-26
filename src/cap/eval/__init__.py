"""CAP Evaluation Framework.

Measures quality, performance, and reliability of platform components.
Separate from unit tests — this is for statistical evaluation with scored metrics.
"""

from cap.eval.framework import EvalCase, EvalResult, EvalReport, EvalSuite

__all__ = ["EvalCase", "EvalResult", "EvalReport", "EvalSuite"]

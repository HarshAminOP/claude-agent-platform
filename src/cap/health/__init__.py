"""
CAP Health Monitoring — agent health inference, token estimation, failure prediction.

Provides:
- AgentHealthMonitor: tracks agent health and predicts failure risk
- HealthState: enum of possible agent health states

Reference: CAP System Design Section 16C.
"""

from .monitor import AgentHealthMonitor, HealthState

__all__ = [
    "AgentHealthMonitor",
    "HealthState",
]

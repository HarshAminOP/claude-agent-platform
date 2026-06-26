"""Workflow hooks — intercepts Claude Code workflow lifecycle to emit team events.

This module provides hooks that workflow scripts can call to emit rich team
conversation events instead of bare status updates.

Usage in a Claude Code workflow script (.js):
    The orchestrator calls these via the workflow-engine MCP tool workflow_signal
    with structured message payloads that the WorkflowObserver translates into
    team conversation.

Usage from Python (for cap's own workflows):
    from cap.lib.workflow_hooks import TeamSignaler

    signaler = TeamSignaler(db_path, workflow_id)
    signaler.says("architect", "Proposing 3-tier architecture with EKS + RDS")
    signaler.concern("security", "architect", "IAM role is overly permissive")
    signaler.handoff("devops", "sre", "Helm chart ready, need alerting rules for /health")
    signaler.done("devops", "Terraform plan: +14 resources")
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class TeamSignaler:
    """Emit team conversation events to the workflow_events table.

    These events are picked up by WorkflowObserver and rendered as
    team dialogue to the user.
    """

    def __init__(self, db_path: Path, workflow_id: str):
        self.db_path = db_path
        self.workflow_id = workflow_id

    def _emit(self, event_type: str, agent_id: str, message: str, phase: str = "", tokens_delta: int = 0):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """INSERT INTO workflow_events (workflow_id, event_type, agent_id, phase, message, tokens_delta, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.workflow_id, event_type, agent_id, phase, message, tokens_delta,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()

    def phase(self, name: str):
        """Signal a new phase starting."""
        self._emit("phase_start", "", "", phase=name)

    def start(self, agent_role: str, task: str, phase: str = ""):
        """Agent begins work."""
        self._emit("agent_start", agent_role, task, phase=phase)

    def says(self, agent_role: str, message: str, target: str = ""):
        """Agent says something to the team or a specific member.

        message format: "what they want to communicate"
        target: another agent role (empty = broadcast to team)
        """
        payload = json.dumps({"type": "message", "target": target, "text": message})
        self._emit("agent_message", agent_role, payload)

    def concern(self, agent_role: str, target: str, message: str):
        """Agent raises a concern about another agent's work."""
        payload = json.dumps({"type": "concern", "target": target, "text": message})
        self._emit("agent_concern", agent_role, payload)

    def handoff(self, from_agent: str, to_agent: str, message: str):
        """Agent hands work to another agent."""
        payload = json.dumps({"type": "handoff", "target": to_agent, "text": message})
        self._emit("agent_handoff", from_agent, payload)

    def acknowledge(self, agent_role: str, message: str = "acknowledged"):
        """Agent acknowledges received work/concern."""
        self._emit("agent_acknowledge", agent_role, message)

    def done(self, agent_role: str, summary: str, tokens_used: int = 0):
        """Agent completed their work."""
        self._emit("agent_end", agent_role, summary, tokens_delta=tokens_used)

    def fail(self, agent_role: str, error: str):
        """Agent failed."""
        self._emit("agent_fail", agent_role, error)

    def complete(self, summary: str = "All phases complete"):
        """Workflow completed successfully."""
        self._emit("workflow_complete", "system", summary)

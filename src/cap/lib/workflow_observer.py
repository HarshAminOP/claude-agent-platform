"""Workflow Observer — bridges workflow events to team conversation renderer.

Watches workflow_events in platform.db and translates them into TeamRenderer
events so the user sees their engineering team working in real-time.

Used by: Claude Code workflow scripts (via phase()/agent() callbacks),
         `cap workflow watch` CLI command,
         session hooks (auto-render when a workflow starts).
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .team_renderer import TeamRenderer, TeamEvent, EventType

logger = logging.getLogger("cap.workflow_observer")

AGENT_ROLE_MAP = {
    "aws-architect": "architect",
    "architect": "architect",
    "devops": "devops",
    "dev": "dev",
    "security": "security",
    "sre": "sre",
    "cicd": "cicd",
    "test": "test",
    "optimization": "optimization",
    "code-review": "code-review",
    "docs": "docs",
    "teacher": "teacher",
}

PHASE_VERBS = {
    "design": "designing",
    "architecture": "designing architecture for",
    "implement": "implementing",
    "implementation": "implementing",
    "review": "reviewing",
    "verify": "verifying",
    "test": "testing",
    "synthesize": "synthesizing findings from",
    "security": "running security review on",
    "deploy": "deploying",
}


def _infer_role(agent_id: str) -> str:
    """Extract role from agent_id like 'review:security' or 'architect:design'."""
    if not agent_id:
        return "dev"
    parts = agent_id.split(":")
    base = parts[0].lower().replace("_", "-")
    return AGENT_ROLE_MAP.get(base, base)


def _parse_json_message(message: str) -> dict | None:
    """Try to parse a JSON-encoded message payload."""
    if not message or not message.startswith("{"):
        return None
    try:
        return json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return None


def _humanize_message(event_type: str, phase: str, agent_id: str, message: str) -> tuple[EventType, str, str]:
    """Convert raw workflow event into (EventType, message, target_agent)."""

    if event_type == "phase_start":
        return EventType.PHASE_START, phase or "Working", ""

    elif event_type == "phase_end":
        return EventType.PHASE_END, f"Phase complete: {phase}", ""

    elif event_type == "agent_start":
        verb = "starting work"
        if phase:
            phase_lower = phase.lower()
            for key, v in PHASE_VERBS.items():
                if key in phase_lower:
                    verb = v
                    break
            else:
                verb = f"working on {phase.lower()}"
        if message:
            verb = message
        return EventType.AGENT_START, verb, ""

    elif event_type == "agent_message":
        payload = _parse_json_message(message)
        if payload:
            text = payload.get("text", message)
            target = payload.get("target", "")
            return EventType.AGENT_MESSAGE, text, target
        return EventType.AGENT_MESSAGE, message, ""

    elif event_type == "agent_concern":
        payload = _parse_json_message(message)
        if payload:
            text = payload.get("text", message)
            target = payload.get("target", "")
            return EventType.AGENT_CONCERN, text, target
        return EventType.AGENT_CONCERN, message, ""

    elif event_type == "agent_handoff":
        payload = _parse_json_message(message)
        if payload:
            text = payload.get("text", message)
            target = payload.get("target", "")
            return EventType.AGENT_HANDOFF, text, target
        return EventType.AGENT_HANDOFF, message, ""

    elif event_type == "agent_acknowledge":
        return EventType.AGENT_ACKNOWLEDGE, message or "acknowledged", ""

    elif event_type == "agent_end":
        return EventType.AGENT_COMPLETE, message or "done", ""

    elif event_type == "agent_fail":
        return EventType.AGENT_FAIL, message or "failed", ""

    elif event_type == "error":
        return EventType.AGENT_FAIL, message or "encountered an error", ""

    elif event_type == "workflow_complete":
        return EventType.WORKFLOW_COMPLETE, message or "All phases complete", ""

    else:
        return EventType.AGENT_THINKING, message or event_type, ""


class WorkflowObserver:
    """Observes a running workflow and renders it as team conversation."""

    def __init__(self, db_path: Path, workflow_id: str, renderer: TeamRenderer = None):
        self.db_path = db_path
        self.workflow_id = workflow_id
        self._last_event_id = 0
        self._stop = threading.Event()

        if renderer is None:
            wf = self._get_workflow_info()
            self.renderer = TeamRenderer(
                workflow_name=wf.get("name", workflow_id),
                budget_usd=wf.get("budget_tokens", 500000) * 15 / 1_000_000,
                max_agents=wf.get("max_agents", 15),
            )
        else:
            self.renderer = renderer

    def _get_workflow_info(self) -> dict:
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT name, budget_tokens, max_agents FROM workflows WHERE id = ?",
            (self.workflow_id,)
        ).fetchone()
        conn.close()
        if row:
            return {"name": row[0], "budget_tokens": row[1], "max_agents": row[2]}
        return {}

    def _poll_events(self) -> list[dict]:
        """Fetch new events since last check."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            """SELECT id, event_type, agent_id, phase, message, tokens_delta, timestamp
               FROM workflow_events
               WHERE workflow_id = ? AND id > ?
               ORDER BY id""",
            (self.workflow_id, self._last_event_id)
        ).fetchall()
        conn.close()

        events = []
        for row in rows:
            self._last_event_id = row[0]
            events.append({
                "id": row[0],
                "event_type": row[1],
                "agent_id": row[2] or "",
                "phase": row[3] or "",
                "message": row[4] or "",
                "tokens_delta": row[5] or 0,
                "timestamp": row[6],
            })
        return events

    def _check_workflow_status(self) -> str:
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT status, tokens_used FROM workflows WHERE id = ?",
            (self.workflow_id,)
        ).fetchone()
        conn.close()
        if row:
            self.renderer.cost_so_far = row[1] * 15 / 1_000_000
            return row[0]
        return "unknown"

    def render_event(self, raw_event: dict):
        """Convert a raw DB event to a team event and render it."""
        event_type, message, target_agent = _humanize_message(
            raw_event["event_type"],
            raw_event["phase"],
            raw_event["agent_id"],
            raw_event["message"],
        )
        role = _infer_role(raw_event["agent_id"])

        team_event = TeamEvent(
            event_type=event_type,
            agent_role=role,
            message=message,
            target_agent=target_agent,
            phase=raw_event["phase"],
            tokens_delta=raw_event["tokens_delta"],
        )
        self.renderer.render_event(team_event)

    def watch(self, poll_interval: float = 2.0):
        """Watch workflow and render events in real-time. Blocks until complete."""
        self.renderer.render_header()
        rendered_terminal = False

        while not self._stop.is_set():
            events = self._poll_events()
            for event in events:
                self.render_event(event)
                if event["event_type"] in ("workflow_complete", "workflow_killed"):
                    rendered_terminal = True

            status = self._check_workflow_status()
            if status in ("completed", "failed", "killed"):
                if not rendered_terminal:
                    if status == "killed":
                        self.renderer.render_event(TeamEvent(
                            EventType.WORKFLOW_KILLED, "system", "Workflow killed"
                        ))
                    else:
                        self.renderer.render_event(TeamEvent(
                            EventType.WORKFLOW_COMPLETE, "system", f"Workflow {status}"
                        ))
                break

            self._stop.wait(poll_interval)

    def watch_async(self, poll_interval: float = 2.0) -> threading.Thread:
        """Start watching in a background thread. Returns the thread."""
        t = threading.Thread(target=self.watch, args=(poll_interval,), daemon=True)
        t.start()
        return t

    def stop(self):
        """Stop the observer."""
        self._stop.set()


def observe_workflow(db_path: Path, workflow_id: str):
    """Convenience: observe a workflow with default settings."""
    observer = WorkflowObserver(db_path, workflow_id)
    observer.watch()

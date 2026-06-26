"""Team conversation renderer for workflow progress.

Renders workflow agent activity as a simulated team conversation — like watching
a Slack channel or standup. Each workflow event is streamed immediately to stderr
so it doesn't interfere with tool stdout.

Usage::

    renderer = TeamRenderer("new-service-deployment", budget_usd=5.0, max_agents=12)
    renderer.render_header()
    renderer.render_event(TeamEvent(EventType.PHASE_START, "architect", "", phase="Design"))
    renderer.render_event(TeamEvent(EventType.AGENT_START, "architect", "Analysing requirements..."))
    ...
    renderer.render_event(TeamEvent(EventType.WORKFLOW_COMPLETE, "system", ""))

Run the built-in demo::

    python -m cap.lib.team_renderer
"""

import time
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Rich is a declared dependency (pyproject.toml >=13.0).  We keep a plain-text
# fallback anyway so the module stays importable in stripped environments (e.g.
# Lambda layers, minimal CI images) where the wheel was excluded.
# ---------------------------------------------------------------------------
try:
    from rich.console import Console as _RichConsole

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """All event types emitted by a workflow engine."""

    PHASE_START = "phase_start"
    PHASE_END = "phase_end"
    AGENT_START = "agent_start"
    AGENT_THINKING = "agent_thinking"
    AGENT_MESSAGE = "agent_message"        # agent broadcasts to team
    AGENT_HANDOFF = "agent_handoff"        # agent passes work to another
    AGENT_CONCERN = "agent_concern"        # agent raises a concern / finding
    AGENT_ACKNOWLEDGE = "agent_acknowledge"  # agent acknowledges input
    AGENT_COMPLETE = "agent_complete"
    AGENT_FAIL = "agent_fail"
    WORKFLOW_BUDGET = "workflow_budget"    # incremental token/cost update
    WORKFLOW_COMPLETE = "workflow_complete"
    WORKFLOW_KILLED = "workflow_killed"


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass
class TeamEvent:
    """A single event emitted during workflow execution.

    Attributes:
        event_type:    One of the :class:`EventType` values.
        agent_role:    The role emitting the event (e.g. ``"architect"``).
        message:       Human-readable description of what happened.
        target_agent:  Recipient role when the event is directed at a specific
                       agent (empty string = broadcast to the whole team).
        phase:         Phase name, required for ``PHASE_START`` events.
        tokens_delta:  Incremental token count consumed since last budget event.
                       The renderer converts this to a cost estimate in USD.
        timestamp:     Unix timestamp; auto-populated when not provided.
    """

    event_type: EventType
    agent_role: str
    message: str
    target_agent: str = ""
    phase: str = ""
    tokens_delta: int = 0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Style tables
# ---------------------------------------------------------------------------

AGENT_COLORS: dict[str, str] = {
    "architect": "blue",
    "devops": "green",
    "security": "red",
    "sre": "yellow",
    "dev": "cyan",
    "code-review": "magenta",
    "test": "white",
    "optimization": "bright_yellow",
    "docs": "dim",
    "cicd": "bright_green",
    "system": "grey50",
}

AGENT_ICONS: dict[str, str] = {
    "architect": "🏗",
    "devops": "⚙",
    "security": "🔒",
    "sre": "📊",
    "dev": "💻",
    "code-review": "👁",
    "test": "🧪",
    "optimization": "⚡",
    "docs": "📝",
    "cicd": "🚀",
    "system": "🤖",
}

# Approximate cost per token (input+output blended, USD).  Used only to
# convert raw token deltas reported by WORKFLOW_BUDGET events into a dollar
# figure for the status line.  The caller can set renderer.cost_so_far
# directly for exact accounting.
_COST_PER_TOKEN_USD: float = 15.0 / 1_000_000


# ---------------------------------------------------------------------------
# Plain-text fallback console
# ---------------------------------------------------------------------------


class _PlainConsole:
    """Minimal stderr writer used when Rich is not installed."""

    def print(self, text: str = "", **_kwargs) -> None:  # noqa: A003
        import re
        import sys

        # Strip Rich markup tags like [bold], [green], [/], etc.
        clean = re.sub(r"\[/?[^\[\]]*\]", "", text)
        print(clean, file=sys.stderr)

    def rule(self, title: str = "", **_kwargs) -> None:
        import sys

        title_clean = title.strip()
        # Same stripping as above
        import re

        title_clean = re.sub(r"\[/?[^\[\]]*\]", "", title_clean)
        bar = "━" * max(0, (72 - len(title_clean) - 2) // 2)
        print(f"{bar} {title_clean} {bar}", file=sys.stderr)


# ---------------------------------------------------------------------------
# TeamRenderer
# ---------------------------------------------------------------------------

_PHASE_BAR_WIDTH = 54  # total width of phase header rule


class TeamRenderer:
    """Render workflow events as a team conversation.

    All output goes to *stderr* so it does not pollute tool stdout.  Each
    :meth:`render_event` call writes immediately — there is no internal buffer.

    Parameters:
        workflow_name: Human-readable workflow identifier shown in the header.
        budget_usd:    Hard budget cap in USD (display only; enforcement is
                       the caller's responsibility).
        max_agents:    Maximum concurrent agents (display only).
        console:       Optional Rich ``Console`` instance.  When ``None`` a
                       ``Console(stderr=True)`` is created.  Pass a custom
                       instance to redirect output in tests.
    """

    def __init__(
        self,
        workflow_name: str,
        budget_usd: float,
        max_agents: int,
        console: object | None = None,
    ) -> None:
        self.workflow_name = workflow_name
        self.budget_usd = budget_usd
        self.max_agents = max_agents

        # State
        self.cost_so_far: float = 0.0
        self.agents_active: int = 0
        self.agents_total: int = 0
        self.current_phase: str = ""
        self.events: list[TeamEvent] = []
        self.start_time: float = time.time()

        # Console
        if console is not None:
            self._console = console
        elif _RICH_AVAILABLE:
            self._console = _RichConsole(stderr=True, highlight=False)
        else:
            self._console = _PlainConsole()  # pragma: no cover

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_header(self) -> None:
        """Print the workflow banner.  Call once before the first event."""
        self._console.rule(
            f"[bold]Workflow: {self.workflow_name}[/bold]",
            style="bright_blue",
        )
        self._console.print(
            f"  Budget: [bold]${self.budget_usd:.2f}[/bold] │ "
            f"Max Agents: [bold]{self.max_agents}[/bold] │ "
            f"Status: [green]running[/green]"
        )
        self._console.print()

    def render_event(self, event: TeamEvent) -> None:  # noqa: C901  (acceptable cyclomatic complexity for a dispatch table)
        """Render *event* immediately to the console.

        The event is also appended to :attr:`events` for later inspection.
        """
        self.events.append(event)

        color = AGENT_COLORS.get(event.agent_role, "white")
        icon = AGENT_ICONS.get(event.agent_role, "•")
        agent_label = f"[{color}]\\[{event.agent_role.title()}][/{color}]"

        et = event.event_type

        # ── Phase boundaries ────────────────────────────────────────────
        if et == EventType.PHASE_START:
            self.current_phase = event.phase
            pad = "─" * max(1, _PHASE_BAR_WIDTH - len(event.phase) - 2)
            self._console.print()
            self._console.print(f"┌─ [bold]Phase: {event.phase}[/bold] {pad}")
            self._console.print("│")

        elif et == EventType.PHASE_END:
            self._console.print("│")

        # ── Agent lifecycle ─────────────────────────────────────────────
        elif et == EventType.AGENT_START:
            self.agents_active += 1
            self.agents_total += 1
            self._console.print(f"│  {agent_label} {event.message}")

        elif et == EventType.AGENT_THINKING:
            self._console.print(f"│  {agent_label} [dim]{event.message}[/dim]")

        elif et == EventType.AGENT_COMPLETE:
            self.agents_active = max(0, self.agents_active - 1)
            self._console.print(f"│  {agent_label} [green]✓[/green] {event.message}")

        elif et == EventType.AGENT_FAIL:
            self.agents_active = max(0, self.agents_active - 1)
            self._console.print(f"│  {agent_label} [red]✗[/red] {event.message}")

        # ── Agent communication ─────────────────────────────────────────
        elif et == EventType.AGENT_MESSAGE:
            self._render_speech(agent_label, event.target_agent, event.message, prefix="→")

        elif et == EventType.AGENT_HANDOFF:
            self._render_speech(agent_label, event.target_agent, event.message, prefix="→")

        elif et == EventType.AGENT_CONCERN:
            self._render_speech(
                agent_label,
                event.target_agent,
                f"⚠ {event.message}",
                prefix="→",
            )

        elif et == EventType.AGENT_ACKNOWLEDGE:
            self._console.print(f"│  {agent_label} [dim]{event.message}[/dim]")

        # ── Budget updates ───────────────────────────────────────────────
        elif et == EventType.WORKFLOW_BUDGET:
            if event.tokens_delta:
                self.cost_so_far += event.tokens_delta * _COST_PER_TOKEN_USD
            pct = (self.cost_so_far / self.budget_usd * 100) if self.budget_usd else 0.0
            if pct > 80:
                self._console.print(
                    f"│  [yellow]⚡ Budget: "
                    f"${self.cost_so_far:.2f}/${self.budget_usd:.2f} "
                    f"({pct:.0f}%)[/yellow]"
                )

        # ── Workflow terminal states ─────────────────────────────────────
        elif et == EventType.WORKFLOW_COMPLETE:
            elapsed = time.time() - self.start_time
            mins, secs = divmod(int(elapsed), 60)
            duration = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            self._console.print("│")
            self._console.print(f"└─ [bold green]Complete[/bold green] {'─' * 45}")
            self._console.print(
                f"   Duration: [bold]{duration}[/bold] │ "
                f"Cost: [bold]${self.cost_so_far:.2f}[/bold] │ "
                f"Agents: [bold]{self.agents_total}[/bold] │ "
                f"Status: [green]✓[/green]"
            )
            self._console.print()

        elif et == EventType.WORKFLOW_KILLED:
            elapsed = time.time() - self.start_time
            mins, secs = divmod(int(elapsed), 60)
            duration = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            self._console.print("│")
            self._console.print(f"└─ [bold red]KILLED[/bold red] {'─' * 47}")
            self._console.print(
                f"   Duration: [bold]{duration}[/bold] │ "
                f"Cost: [bold]${self.cost_so_far:.2f}[/bold] │ "
                f"Reason: {event.message}"
            )
            self._console.print()

    def render_status_line(self) -> str:
        """Return a compact one-line status string suitable for a progress bar."""
        elapsed = time.time() - self.start_time
        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        return (
            f"[{self.current_phase or 'idle'}] "
            f"⏱ {duration} │ "
            f"💰 ${self.cost_so_far:.2f}/${self.budget_usd:.2f} │ "
            f"👥 {self.agents_active} active / {self.agents_total} total"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render_speech(
        self,
        agent_label: str,
        target_role: str,
        message: str,
        prefix: str = "→",
    ) -> None:
        """Render a quoted speech bubble from an agent to a target (or team)."""
        if target_role:
            target_color = AGENT_COLORS.get(target_role, "white")
            target_str = f"{prefix} [{target_color}][bold]{target_role.title()}[/bold][/{target_color}]"
        else:
            target_str = f"{prefix} Team"

        indent = " " * 17  # aligns continuation lines under the opening quote
        lines = _wrap_message(message, width=58)

        if len(lines) == 1:
            self._console.print(f'│  {agent_label} {target_str}: "{lines[0]}"')
        else:
            self._console.print(f'│  {agent_label} {target_str}: "{lines[0]}')
            for line in lines[1:-1]:
                self._console.print(f"│  {indent}{line}")
            self._console.print(f'│  {indent}{lines[-1]}"')


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _wrap_message(msg: str, width: int = 60) -> list[str]:
    """Word-wrap *msg* to *width* characters, returning a list of lines.

    Preserves single words that exceed *width* (no hard break inside tokens).
    """
    words = msg.split()
    if not words:
        return [""]

    lines: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        needed = len(word) + (1 if current else 0)
        if current and current_len + needed > width:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += needed

    if current:
        lines.append(" ".join(current))

    return lines


# ---------------------------------------------------------------------------
# Demo / smoke-test
# ---------------------------------------------------------------------------


def demo_workflow() -> None:
    """Drive the renderer with a synthetic new-service-deployment workflow.

    Run with::

        python -m cap.lib.team_renderer
    """
    renderer = TeamRenderer("new-service-deployment", budget_usd=5.0, max_agents=12)
    renderer.render_header()

    events: list[TeamEvent] = [
        # ── Architecture Design ────────────────────────────────────────
        TeamEvent(EventType.PHASE_START, "architect", "", phase="Architecture Design"),
        TeamEvent(
            EventType.AGENT_START, "architect",
            "Starting architecture design for payment-service...",
        ),
        TeamEvent(
            EventType.AGENT_MESSAGE, "architect",
            "Proposed: EKS deployment, 3 replicas, ALB ingress, DynamoDB backend. "
            "Estimated $180/mo.",
            target_agent="",
        ),
        TeamEvent(EventType.AGENT_START, "security", "reviewing architecture proposal..."),
        TeamEvent(
            EventType.AGENT_CONCERN, "security",
            "DynamoDB table needs encryption at rest enabled. IAM role is too broad "
            "— needs resource-level conditions.",
            target_agent="architect",
        ),
        TeamEvent(EventType.AGENT_ACKNOWLEDGE, "architect", "acknowledged, revising IAM scope..."),
        TeamEvent(EventType.AGENT_COMPLETE, "architect", "Architecture approved (2 revisions)"),
        TeamEvent(EventType.PHASE_END, "architect", ""),
        # ── Implementation ─────────────────────────────────────────────
        TeamEvent(EventType.PHASE_START, "devops", "", phase="Implementation"),
        TeamEvent(EventType.AGENT_START, "devops", "picked up: Terraform modules + Helm chart"),
        TeamEvent(EventType.AGENT_START, "sre", "picked up: alerting rules + dashboards"),
        TeamEvent(
            EventType.AGENT_HANDOFF, "devops",
            "Helm chart ready. Service exposes :8080/health and :8080/metrics. "
            "Need alerts for p99 > 500ms.",
            target_agent="sre",
        ),
        TeamEvent(EventType.AGENT_ACKNOWLEDGE, "sre", "acknowledged, creating alert rules..."),
        TeamEvent(
            EventType.AGENT_COMPLETE, "devops",
            "Terraform plan: +14 resources, 0 changes, 0 destroys",
        ),
        TeamEvent(
            EventType.AGENT_COMPLETE, "sre",
            "3 alerts configured: latency, error_rate, saturation",
        ),
        TeamEvent(EventType.PHASE_END, "sre", ""),
        # ── Review ─────────────────────────────────────────────────────
        TeamEvent(EventType.PHASE_START, "code-review", "", phase="Review"),
        TeamEvent(EventType.AGENT_START, "code-review", "reviewing all changes..."),
        TeamEvent(
            EventType.AGENT_CONCERN, "code-review",
            "Helm values.yaml: resource limits missing for sidecar container.",
            target_agent="devops",
        ),
        TeamEvent(EventType.AGENT_ACKNOWLEDGE, "devops", "fixing..."),
        TeamEvent(EventType.AGENT_COMPLETE, "code-review", "All clear"),
        TeamEvent(EventType.AGENT_START, "security", "final security review..."),
        TeamEvent(EventType.AGENT_COMPLETE, "security", "Approved — no issues remaining"),
        TeamEvent(EventType.PHASE_END, "security", ""),
        # ── Done ───────────────────────────────────────────────────────
        TeamEvent(EventType.WORKFLOW_COMPLETE, "system", "All phases complete"),
    ]

    for event in events:
        renderer.render_event(event)
        time.sleep(0.15)


if __name__ == "__main__":
    demo_workflow()

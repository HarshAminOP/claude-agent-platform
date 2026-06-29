"""Real-time status dashboard — terminal TUI.

Shows live progress of workflows, active agents, budget usage, blockers,
and backlog status. Uses Rich Live for auto-refreshing display.

Usage:
    cap dashboard           # start interactive TUI
    cap dashboard --once    # render once and exit (for piping)
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box


class Dashboard:
    def __init__(self, data_dir: Path, poll_interval: float = 2.0):
        self.data_dir = data_dir
        self.poll_interval = poll_interval
        self.console = Console(stderr=True)
        self._running = False

    def _open_db(self, name: str) -> Optional[sqlite3.Connection]:
        path = self.data_dir / name
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def render_once(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=1),
        )

        # Header
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        layout["header"].update(Panel(
            f"[bold cyan]CAP Dashboard[/bold cyan]  [dim]{now}[/dim]",
            box=box.MINIMAL,
        ))

        # Left panel: workflows + agents
        left_content = self._render_workflows()
        layout["left"].update(left_content)

        # Right panel: budget + backlog
        right_content = self._render_sidebar()
        layout["right"].update(right_content)

        # Footer
        layout["footer"].update(Panel(
            "[dim]q: quit  r: refresh  k: kill workflow[/dim]",
            box=box.MINIMAL,
        ))

        return layout

    def _render_workflows(self) -> Panel:
        db = self._open_db("platform.db")
        if not db:
            return Panel("[dim]No workflow data[/dim]", title="Workflows")

        # Active workflows
        rows = db.execute(
            """SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, max_agents, started_at
               FROM workflows WHERE status = 'running'
               ORDER BY started_at DESC LIMIT 5"""
        ).fetchall()

        if not rows:
            # Show recent completed
            rows = db.execute(
                """SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, max_agents, started_at
                   FROM workflows ORDER BY started_at DESC LIMIT 5"""
            ).fetchall()

        table = Table(title="Workflows", box=box.SIMPLE_HEAD, show_edge=False, expand=True)
        table.add_column("ID", style="dim", width=14)
        table.add_column("Name", max_width=25)
        table.add_column("Status", width=10)
        table.add_column("Budget", justify="right", width=8)
        table.add_column("Agents", justify="right", width=8)
        table.add_column("ETA", justify="right", width=8)

        for row in rows:
            wf_id, name, status, budget, tokens_used, agents, max_agents, started = row
            pct = round(tokens_used / max(budget, 1) * 100)
            pct_color = "green" if pct < 60 else ("yellow" if pct < 90 else "red")

            # ETA estimate based on current rate
            eta = "—"
            if status == "running" and started and tokens_used > 0:
                try:
                    start_dt = datetime.fromisoformat(started)
                    elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
                    rate = tokens_used / max(elapsed, 1)
                    remaining_tokens = budget - tokens_used
                    if rate > 0:
                        eta_seconds = remaining_tokens / rate
                        if eta_seconds < 60:
                            eta = f"{int(eta_seconds)}s"
                        else:
                            eta = f"{eta_seconds/60:.1f}m"
                except (ValueError, TypeError):
                    pass

            status_color = {"running": "green", "completed": "blue", "killed": "red", "failed": "red"}.get(status, "white")
            table.add_row(
                wf_id[:14],
                (name or "—")[:25],
                f"[{status_color}]{status}[/{status_color}]",
                f"[{pct_color}]{pct}%[/{pct_color}]",
                f"{agents}/{max_agents}",
                eta,
            )

        # Recent events
        events = db.execute(
            """SELECT we.event_type, we.agent_id, we.message, we.timestamp, w.name
               FROM workflow_events we
               JOIN workflows w ON w.id = we.workflow_id
               ORDER BY we.timestamp DESC LIMIT 8"""
        ).fetchall()

        events_table = Table(title="Recent Events", box=box.SIMPLE_HEAD, show_edge=False, expand=True)
        events_table.add_column("Time", width=8)
        events_table.add_column("Type", width=12)
        events_table.add_column("Agent", width=10)
        events_table.add_column("Message", max_width=40)

        for ev in events:
            ev_type, agent_id, message, timestamp, _ = ev
            time_str = (timestamp or "")[-8:] if timestamp else "—"
            events_table.add_row(
                time_str,
                ev_type or "—",
                (agent_id or "—")[:10],
                (message or "—")[:40],
            )

        db.close()

        from rich.console import Group
        return Panel(Group(table, events_table), title="Active Work", border_style="cyan")

    def _render_sidebar(self) -> Panel:
        sections = []

        # Budget section
        db = self._open_db("platform.db")
        if db:
            now = datetime.now(timezone.utc)
            period = now.strftime("%Y-%m")
            row = db.execute(
                "SELECT SUM(total_cost_usd) FROM budget_ledger WHERE period = ?",
                (period,)
            ).fetchone()
            spend = row[0] or 0.0 if row else 0.0
            db.close()
            sections.append(f"[bold]Budget[/bold]  ${spend:.4f}")
        else:
            sections.append("[dim]Budget: n/a[/dim]")

        # Backlog section
        bl_db = self._open_db("backlog.db")
        if bl_db:
            try:
                stats_rows = bl_db.execute(
                    "SELECT status, COUNT(*) FROM backlog_tasks GROUP BY status"
                ).fetchall()
                stats = {r[0]: r[1] for r in stats_rows}
                total = sum(stats.values())
                done = stats.get("done", 0)
                in_progress = stats.get("in_progress", 0)
                blocked = stats.get("blocked", 0)
                pct = round(done / max(total, 1) * 100)
                sections.append(
                    f"\n[bold]Backlog[/bold]  {pct}% done\n"
                    f"  Total: {total}  Done: [green]{done}[/green]\n"
                    f"  Active: [yellow]{in_progress}[/yellow]  Blocked: [{'red' if blocked else 'dim'}]{blocked}[/{'red' if blocked else 'dim'}]"
                )

                # Pending decisions
                pending = bl_db.execute(
                    "SELECT COUNT(*) FROM decision_cards WHERE status = 'pending'"
                ).fetchone()
                if pending and pending[0] > 0:
                    sections.append(f"\n[bold yellow]⚠ {pending[0]} pending decision(s)[/bold yellow]")

                # Blocking conflicts
                blocking = bl_db.execute(
                    "SELECT COUNT(*) FROM conflicts WHERE status = 'escalated' AND severity = 'blocking'"
                ).fetchone()
                if blocking and blocking[0] > 0:
                    sections.append(f"[bold red]🛑 {blocking[0]} blocking conflict(s)[/bold red]")
            except Exception:
                sections.append("\n[dim]Backlog: error reading[/dim]")
            bl_db.close()
        else:
            sections.append("\n[dim]Backlog: n/a[/dim]")

        # Sessions
        sess_db = self._open_db("sessions.db")
        if sess_db:
            try:
                active = sess_db.execute(
                    "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
                ).fetchone()[0]
                learnings = sess_db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
                sections.append(f"\n[bold]Memory[/bold]  {active} sessions  {learnings} learnings")
            except Exception:
                pass
            sess_db.close()

        return Panel("\n".join(sections), title="Status", border_style="green")

    def run(self):
        """Start the live dashboard."""
        self._running = True
        try:
            with Live(self.render_once(), console=self.console, refresh_per_second=1, screen=True) as live:
                while self._running:
                    time.sleep(self.poll_interval)
                    live.update(self.render_once())
        except KeyboardInterrupt:
            pass

    def stop(self):
        self._running = False

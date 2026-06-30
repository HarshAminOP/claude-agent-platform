"""CAP CLI commands for orchestration — health, dlq, resume, status.

Provides direct CLI access to the orchestration layer without
requiring the MCP server to be running.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(stderr=True)


def _get_db() -> sqlite3.Connection:
    """Get the CAP database connection."""
    from cap.db import get_db, migrate
    db_path = os.environ.get(
        "CAP_ORCHESTRATOR_DB",
        os.path.expanduser("~/.cap/cap.db"),
    )
    db = get_db(db_path)
    migrate(db)
    return db


def _status_color(status: str) -> str:
    mapping = {
        "healthy": "green",
        "degraded": "yellow",
        "unhealthy": "red",
        "unknown": "dim",
        "CLOSED": "green",
        "OPEN": "red",
        "HALF_OPEN": "yellow",
        "pending": "dim",
        "completed": "green",
        "failed": "red",
        "running": "cyan",
        "skipped": "yellow",
    }
    color = mapping.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _disk_usage(path: str) -> str:
    """Return human-readable size of a file or directory."""
    p = Path(path)
    if not p.exists():
        return "0 B"
    if p.is_file():
        size = p.stat().st_size
    else:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@click.command("health")
@click.option("--agent-type", "-a", default=None, help="Filter to specific agent type")
def health(agent_type: str | None):
    """Agent health dashboard — state, failure rate, circuit breakers, DLQ, disk."""
    from cap.health.monitor import AgentHealthMonitor, HealthState
    from cap.reliability.circuit_breaker import CircuitBreaker
    from cap.reliability.dlq import list_dlq
    from cap.cost.tracker import CostTracker

    db = _get_db()
    monitor = AgentHealthMonitor(db)

    agent_types = ["dev", "devops", "security", "sre", "code-review", "test", "docs", "explore"]
    if agent_type:
        agent_types = [agent_type]

    table = Table(title="Agent Health Dashboard", box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Agent Type", style="bold")
    table.add_column("State")
    table.add_column("Fail Rate", justify="right")
    table.add_column("Avg Time", justify="right")
    table.add_column("Circuit", width=10)

    for at in agent_types:
        health_state = monitor.infer_health(at)
        cb = CircuitBreaker(at, db)
        cb_state = cb.get_state()

        # Get baseline stats
        baseline = db.execute(
            "SELECT failure_rate, sample_count, avg_duration FROM agent_health_baselines WHERE agent_type = ?",
            (at,),
        ).fetchone()

        failure_rate = f"{baseline[0]:.1%}" if baseline and baseline[0] is not None else "--"
        avg_duration = f"{baseline[2]:.0f}ms" if baseline and baseline[2] is not None else "--"

        table.add_row(
            at,
            _status_color(health_state.value),
            failure_rate,
            avg_duration,
            _status_color(cb_state),
        )

    console.print(table)

    # DLQ count
    dlq_items = list_dlq(db)
    dlq_count = len(dlq_items)
    dlq_color = "red" if dlq_count > 0 else "green"
    console.print(f"\n[bold]DLQ:[/bold] [{dlq_color}]{dlq_count} tasks[/{dlq_color}]")

    # Disk usage
    db_path = os.environ.get("CAP_ORCHESTRATOR_DB", os.path.expanduser("~/.cap/cap.db"))
    cap_dir = os.path.dirname(db_path)
    disk = _disk_usage(cap_dir)

    # Budget check
    try:
        tracker = CostTracker(db)
        budget = tracker.budget_check()
        mode_color = {"online": "green", "degraded": "yellow", "offline": "red"}.get(budget["mode"], "white")
        console.print(
            f"[bold]Budget:[/bold] ${budget['spent_today_usd']:.4f} / ${budget['daily_cap_usd']:.2f} "
            f"([{mode_color}]{budget['mode']}[/{mode_color}])"
        )
    except Exception:
        pass

    console.print(f"[bold]Disk:[/bold] {disk} ({cap_dir})")


@click.group("dlq", invoke_without_command=True)
@click.pass_context
def dlq(ctx):
    """Dead-letter queue — list, retry, dismiss failed tasks."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(dlq_list)


@dlq.command("list")
def dlq_list():
    """List failed tasks in the dead-letter queue."""
    from cap.reliability.dlq import list_dlq as _list_dlq

    db = _get_db()
    items = _list_dlq(db)

    if not items:
        console.print("[green]Dead-letter queue is empty.[/green]")
        return

    table = Table(title=f"Dead Letter Queue ({len(items)} tasks)", box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Task ID", style="dim", width=16)
    table.add_column("Agent", width=12)
    table.add_column("Description", max_width=50)
    table.add_column("Last Error", max_width=35)
    table.add_column("Workflow", style="dim", width=16)
    table.add_column("Age")

    now = time.time()
    for item in items:
        age_seconds = now - item["created_at"]
        if age_seconds < 3600:
            age = f"{int(age_seconds / 60)}m ago"
        elif age_seconds < 86400:
            age = f"{int(age_seconds / 3600)}h ago"
        else:
            age = f"{int(age_seconds / 86400)}d ago"

        table.add_row(
            item["task_id"][:16],
            item["agent_type"],
            item["task_description"][:50],
            item["last_error"][:35],
            (item["workflow_id"] or "--")[:16],
            age,
        )

    console.print(table)
    console.print("\n[dim]Use: cap dlq retry <id>, cap dlq dismiss <id>, cap dlq retry-all[/dim]")


@dlq.command("retry")
@click.argument("task_id")
def dlq_retry(task_id: str):
    """Retry a specific failed task by ID."""
    from cap.reliability.dlq import retry_task

    db = _get_db()
    result = retry_task(task_id, db)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise SystemExit(1)
    console.print(f"[green]Task {task_id} marked for retry.[/green]")
    console.print(f"  Agent: {result.get('agent_type', '--')}")
    console.print(f"  Description: {result.get('description', '--')[:80]}")


@dlq.command("dismiss")
@click.argument("task_id")
def dlq_dismiss(task_id: str):
    """Dismiss a specific failed task by ID (will not be retried)."""
    from cap.reliability.dlq import dismiss_task

    db = _get_db()
    success = dismiss_task(task_id, db)
    if success:
        console.print(f"[green]Task {task_id} dismissed.[/green]")
    else:
        console.print(f"[red]Task {task_id} not found or already processed.[/red]")
        raise SystemExit(1)


@dlq.command("retry-all")
def dlq_retry_all():
    """Retry all pending tasks in the dead-letter queue."""
    from cap.reliability.dlq import list_dlq as _list_dlq, retry_task

    db = _get_db()
    items = _list_dlq(db)

    if not items:
        console.print("[green]Dead-letter queue is empty — nothing to retry.[/green]")
        return

    retried = 0
    errors = 0
    for item in items:
        result = retry_task(item["task_id"], db)
        if "error" in result:
            errors += 1
        else:
            retried += 1

    console.print(f"[green]Retried {retried} task(s).[/green]")
    if errors:
        console.print(f"[yellow]{errors} task(s) could not be retried.[/yellow]")


@click.command("resume")
@click.argument("workflow_id")
def resume(workflow_id: str):
    """Resume a workflow from its last checkpoint."""
    from cap.orchestration.checkpoint import resume_from_checkpoint
    from cap.orchestration.dag import StepState

    db = _get_db()

    try:
        dag, context_thread = resume_from_checkpoint(workflow_id, db)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    # Compute state summary
    states: dict[str, int] = {}
    for step in dag.steps.values():
        state_name = step.state.value
        states[state_name] = states.get(state_name, 0) + 1

    console.print(Panel(
        f"[bold]Workflow:[/bold] {workflow_id}\n"
        f"[bold]Status:[/bold]   Resumed from checkpoint\n"
        f"[bold]Steps:[/bold]    {len(dag.steps)} total\n\n"
        f"  Completed: [green]{states.get('completed', 0)}[/green]\n"
        f"  Pending:   [cyan]{states.get('pending', 0)}[/cyan]\n"
        f"  Failed:    [red]{states.get('failed', 0)}[/red]\n"
        f"  Skipped:   [yellow]{states.get('skipped', 0)}[/yellow]",
        title="Workflow Resumed",
        box=box.ROUNDED,
    ))

    # Show steps detail
    table = Table(title="Steps", box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("ID", style="dim", width=18)
    table.add_column("Agent", width=12)
    table.add_column("State")
    table.add_column("Description", max_width=50)
    table.add_column("Depends On", max_width=25)

    for step_id, step in dag.steps.items():
        deps = ", ".join(d[:12] for d in step.depends_on) if step.depends_on else "--"
        table.add_row(
            step_id[:18],
            step.agent_type,
            _status_color(step.state.value),
            step.description[:50],
            deps,
        )

    console.print(table)
    console.print(
        f"\n[dim]To execute: use cap_execute via MCP or run the orchestrator server.[/dim]"
    )


@click.command("orchestrator-status")
def orchestrator_status():
    """Overall orchestration system status."""
    from cap.orchestration.checkpoint import list_checkpoints
    from cap.reliability.dlq import list_dlq
    from cap.health.monitor import AgentHealthMonitor
    from cap.reliability.circuit_breaker import CircuitBreaker

    db = _get_db()

    # Checkpoints summary
    all_checkpoints = list_checkpoints(db)
    running = [c for c in all_checkpoints if c["phase"] == "running"]
    planned = [c for c in all_checkpoints if c["phase"] == "planned"]
    completed = [c for c in all_checkpoints if c["phase"] == "completed"]
    failed = [c for c in all_checkpoints if c["phase"] == "failed"]

    # DLQ count
    dlq_items = list_dlq(db)

    # Circuit breaker status
    agent_types = ["dev", "devops", "security", "sre", "code-review", "test", "docs", "explore"]
    open_breakers = []
    for at in agent_types:
        cb = CircuitBreaker(at, db)
        state = cb.get_state()
        if state in ("OPEN", "HALF_OPEN"):
            open_breakers.append(f"{at}={state}")

    # Health
    monitor = AgentHealthMonitor(db)
    unhealthy = []
    for at in agent_types:
        h = monitor.infer_health(at)
        if h.value == "unhealthy":
            unhealthy.append(at)

    console.print(Panel(
        f"[bold]Workflows[/bold]\n"
        f"  Running:     [cyan]{len(running)}[/cyan]\n"
        f"  Planned:     [dim]{len(planned)}[/dim]\n"
        f"  Completed:   [green]{len(completed)}[/green]\n"
        f"  Failed:      [red]{len(failed)}[/red]\n"
        f"\n[bold]Reliability[/bold]\n"
        f"  Dead letters: [{'red' if dlq_items else 'green'}]{len(dlq_items)}[/{'red' if dlq_items else 'green'}]\n"
        f"  Open breakers: [{'red' if open_breakers else 'green'}]{', '.join(open_breakers) if open_breakers else 'none'}[/{'red' if open_breakers else 'green'}]\n"
        f"  Unhealthy agents: [{'red' if unhealthy else 'green'}]{', '.join(unhealthy) if unhealthy else 'none'}[/{'red' if unhealthy else 'green'}]",
        title="Orchestration Status",
        box=box.ROUNDED,
    ))

    # Show active workflows if any
    if running or planned:
        table = Table(title="Active Workflows", box=box.SIMPLE_HEAD, show_edge=False)
        table.add_column("Workflow ID", style="bold")
        table.add_column("Phase")
        table.add_column("Created")

        for cp in (running + planned)[:10]:
            import time as _time
            age = time.time() - cp["created_at"]
            if age < 3600:
                age_str = f"{int(age / 60)}m ago"
            elif age < 86400:
                age_str = f"{int(age / 3600)}h ago"
            else:
                age_str = f"{int(age / 86400)}d ago"

            table.add_row(
                cp["workflow_id"][:20],
                _status_color(cp["phase"]),
                age_str,
            )

        console.print(table)

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

try:
    from cap.lib.embeddings import EmbeddingClient
except Exception:  # noqa: BLE001 — optional dep; may be absent in test environments
    EmbeddingClient = None  # type: ignore[assignment,misc]


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
    console.print(f"[red]resume is not available: checkpoint module removed[/red]")
    raise SystemExit(1)


@click.command("doctor")
def doctor():
    """Comprehensive platform health diagnostics."""
    ok = click.style("✔", fg="green", bold=True)
    warn = click.style("!", fg="yellow", bold=True)
    err = click.style("✘", fg="red", bold=True)

    cap_home = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))
    data_dir = cap_home / "data"
    cap_db_path = Path(os.path.expanduser("~/.cap/cap.db"))

    click.echo(click.style("\n=== cap doctor ===", bold=True))

    # ── 1. Knowledge DB health ─────────────────────────────────────────────────
    click.echo(click.style("\n1. Knowledge DB", bold=True))
    knowledge_db = data_dir / "knowledge.db"
    if not knowledge_db.exists():
        click.echo(f"  {err} knowledge.db not found at {knowledge_db}")
    else:
        size_bytes = knowledge_db.stat().st_size
        size_kb = size_bytes / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        click.echo(f"  {ok} knowledge.db exists  ({size_str})")
        try:
            import sqlite3
            conn = sqlite3.connect(str(knowledge_db))
            conn.execute("PRAGMA busy_timeout=2000")
            total = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
            failed_emb = conn.execute(
                "SELECT COUNT(*) FROM knowledge_entries WHERE embedding_status = 'failed'"
            ).fetchone()[0]
            conn.close()
            click.echo(f"  {ok} Entries: {total}")
            if failed_emb == 0:
                click.echo(f"  {ok} Failed embeddings: 0")
            else:
                click.echo(f"  {warn} Failed embeddings: {failed_emb}")
        except Exception as exc:
            click.echo(f"  {err} Cannot query knowledge.db: {exc}")

    # ── 2. Embedder health ─────────────────────────────────────────────────────
    click.echo(click.style("\n2. Embedder health", bold=True))
    if EmbeddingClient is None:
        click.echo(f"  {err} EmbeddingClient not available (cap.lib.embeddings could not be imported)")
    else:
        try:
            client = EmbeddingClient()
            avail = client.is_available
            if avail is True:
                click.echo(f"  {ok} EmbeddingClient available (last call succeeded)")
            elif avail is False:
                click.echo(f"  {err} EmbeddingClient unavailable (Bedrock init failed — check AWS credentials/model access)")
            else:
                click.echo(f"  {warn} EmbeddingClient imported (availability unknown — no calls made yet)")
                if client._client is None:
                    click.echo(f"  {err} Bedrock client did not initialise (no AWS credentials?)")
                else:
                    click.echo(f"  {ok} Bedrock client initialised (run `cap embed` to confirm connectivity)")
        except Exception as exc:
            click.echo(f"  {err} Embedder check failed: {exc}")

    # ── 3. MCP server registration ─────────────────────────────────────────────
    click.echo(click.style("\n3. MCP server registration", bold=True))
    expected_servers = [
        "cap-knowledge", "cap-session", "cap-fleet",
        "cap-workflow-engine", "cap-diagram", "cap-backlog",
        "cap-ast", "cap-code-intel", "cap-orchestrator",
    ]
    claude_json_path = Path.home() / ".claude.json"
    configured_servers: set[str] = set()
    if claude_json_path.exists():
        try:
            data = json.loads(claude_json_path.read_text())
            configured_servers = set(data.get("mcpServers", {}).keys())
        except Exception:
            pass

    for srv in expected_servers:
        if srv in configured_servers:
            click.echo(f"  {ok} {srv}")
        else:
            click.echo(f"  {err} {srv}  (not registered — run `cap init` to fix)")

    extra = configured_servers - set(expected_servers)
    if extra:
        click.echo(f"  {ok} Additional servers: {', '.join(sorted(extra))}")

    # ── 4. Learning health ─────────────────────────────────────────────────────
    click.echo(click.style("\n4. Learning health", bold=True))
    if not cap_db_path.exists():
        click.echo(f"  {warn} cap.db not found at {cap_db_path} (run `cap init` first)")
    else:
        try:
            conn = sqlite3.connect(str(cap_db_path))
            conn.execute("PRAGMA busy_timeout=2000")

            trust_rows = conn.execute(
                "SELECT agent_type, action_type, trust_score, success_count, failure_count "
                "FROM trust_levels ORDER BY trust_score DESC LIMIT 5"
            ).fetchall()
            routing_count = conn.execute(
                "SELECT COUNT(*) FROM routing_decisions"
            ).fetchone()[0]
            outcomes_count = conn.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE outcome IS NOT NULL"
            ).fetchone()[0]
            conn.close()

            if trust_rows:
                click.echo(f"  {ok} Top 5 agent trust scores:")
                for row in trust_rows:
                    agent, action, score, succ, fail = row
                    color = "green" if score >= 0.7 else ("yellow" if score >= 0.4 else "red")
                    score_str = click.style(f"{score:.2f}", fg=color)
                    click.echo(f"       {agent}/{action}: {score_str}  (ok={succ} fail={fail})")
            else:
                click.echo(f"  {warn} No trust scores recorded yet (defaults active)")

            click.echo(f"  {ok} Routing decisions: {routing_count}  |  Outcomes recorded: {outcomes_count}")
            threshold_src = "learned" if routing_count >= 10 else "defaults"
            icon = ok if threshold_src == "learned" else warn
            click.echo(f"  {icon} Threshold source: {threshold_src}")
        except Exception as exc:
            click.echo(f"  {err} Cannot query cap.db: {exc}")

    # ── 5. Circuit breaker status ──────────────────────────────────────────────
    click.echo(click.style("\n5. Circuit breaker status", bold=True))
    if not cap_db_path.exists():
        click.echo(f"  {warn} cap.db missing — no circuit breaker state")
    else:
        try:
            from cap.reliability.circuit_breaker import CircuitBreaker

            db_cb = _get_db()
            cb_conn = sqlite3.connect(str(cap_db_path))
            cb_conn.execute("PRAGMA busy_timeout=2000")
            agent_types = ["dev", "devops", "security", "sre", "code-review", "test", "docs", "explore"]
            any_open = False
            for at in agent_types:
                cb = CircuitBreaker(at, db_cb)
                state = cb.get_state()
                failure_row = cb_conn.execute(
                    "SELECT failure_count FROM circuit_breaker_state WHERE agent_type = ?", (at,)
                ).fetchone()
                failures = failure_row[0] if failure_row else 0
                if state == "CLOSED":
                    icon = ok
                elif state == "HALF_OPEN":
                    icon = warn
                    any_open = True
                else:
                    icon = err
                    any_open = True
                state_str = click.style(state, fg={"CLOSED": "green", "OPEN": "red", "HALF_OPEN": "yellow"}.get(state, "white"))
                click.echo(f"  {icon} {at}: {state_str}  (failures={failures})")
            cb_conn.close()
            if not any_open:
                click.echo(f"\n  {ok} All circuit breakers CLOSED")
        except Exception as exc:
            click.echo(f"  {err} Circuit breaker check failed: {exc}")

    # ── 6. Harness section ─────────────────────────────────────────────────────
    click.echo(click.style("\n6. Harness", bold=True))
    try:
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        _platform_db = _Path.home() / ".claude-platform" / "data" / "platform.db"
        _claude_json = _Path.home() / ".claude.json"

        # Harness server registered?
        _harness_registered = False
        if _claude_json.exists():
            try:
                _cj = json.loads(_claude_json.read_text())
                _harness_registered = "cap-harness" in _cj.get("mcpServers", {})
            except Exception:
                pass
        _reg_icon = ok if _harness_registered else warn
        click.echo(f"  {_reg_icon} Harness server registered: {'yes' if _harness_registered else 'no'}")

        # Executor available?
        _executor_ok = False
        try:
            from cap.harness.executor import AgentExecutor  # noqa: F401
            _executor_ok = True
        except Exception:
            pass
        _exec_icon = ok if _executor_ok else warn
        click.echo(f"  {_exec_icon} Executor available: {'yes' if _executor_ok else 'no'}")

        if _platform_db.exists():
            _conn = _sqlite3.connect(str(_platform_db), timeout=2)
            _conn.execute("PRAGMA busy_timeout=2000")

            # Active agents count
            try:
                _active = _conn.execute(
                    "SELECT COUNT(*) FROM agents WHERE status = 'active'"
                ).fetchone()[0]
                click.echo(f"  {ok} Active agents: {_active}")
            except Exception:
                click.echo(f"  {warn} Active agents: (table unavailable)")

            # Today cost from budget_remaining
            try:
                from cap.harness.cost_meter import budget_remaining
                _remaining = budget_remaining(db=_sqlite3.connect(str(_platform_db), timeout=2))
                _daily = 5.0
                _spent = max(0.0, _daily - _remaining)
                click.echo(f"  {ok} Today cost: ${_spent:.4f} (remaining: ${_remaining:.4f})")
            except Exception:
                click.echo(f"  {warn} Today cost: (unavailable)")

            # Governance policy loaded?
            try:
                from cap.harness.governance import load_policy
                _policy = load_policy()
                click.echo(f"  {ok} Governance policy loaded: yes (daily_budget=${_policy.daily_budget_usd:.2f})")
            except Exception as _pe:
                click.echo(f"  {warn} Governance policy loaded: no ({_pe})")

            # Audit entries today
            try:
                import time as _time
                _today_start = _time.time() - 86400
                _audit_count = _conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE timestamp >= ?",
                    (_today_start,),
                ).fetchone()[0]
                click.echo(f"  {ok} Audit entries today: {_audit_count}")
            except Exception:
                click.echo(f"  {warn} Audit entries today: (audit_log unavailable)")

            _conn.close()
        else:
            click.echo(f"  {warn} platform.db not found — run `cap init` to initialize")
    except Exception as _harness_exc:
        click.echo(f"  {warn} Harness check skipped: {_harness_exc}")

    click.echo("")


@click.command("orchestrator-status")
def orchestrator_status():
    """Overall orchestration system status."""
    from cap.reliability.dlq import list_dlq
    from cap.health.monitor import AgentHealthMonitor
    from cap.reliability.circuit_breaker import CircuitBreaker

    db = _get_db()

    # Checkpoints summary (checkpoint module removed — show zeros)
    running = []
    planned = []
    completed = []
    failed = []

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
